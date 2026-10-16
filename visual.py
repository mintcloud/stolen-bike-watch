#!/usr/bin/env python3
"""
visual.py — the photo channel for the stolen-bike watch.

What it is NOT
--------------
It is not "find my bike by image match". That was tried and measured on the
777 archived listing photos (see docs/VISION.md): CLIP ViT-B/32 and Unicom ViT-B/32
global embeddings both rank random Decathlon mountain bikes ABOVE the owner's own
bike photographed from a different angle. Instance-level retrieval off a
marketplace thumbnail does not work with an off-the-shelf embedding.

What it IS
----------
An *attribute* pre-filter. CLIP is bad at "is this the same object" and good at
"what kind of object is this", so it is used zero-shot to answer three
questions about every listing photo:

  dropbar   drop handlebars (road/gravel) vs mtb/city/e-bike/folding/kid/part
            -> validated: the top 25 of 777 by this score are all road bikes
  navy      fraction of pixels in the dark-navy band, measured on the raw
            pixels rather than asked of CLIP, because CLIP's colour and
            tyre-sidewall answers were noise (median 0.37 on "tan sidewalls")
  steel     slim steel touring tube vs carbon aero vs fat aluminium (weak,
            used only to break ties)

The composite `look` score (0-100) re-ranks the digest, and the gate
(`dropbar >= 0.35 or navy >= 0.06`) is what makes the enlarged search radius
readable: it passes 27% of the archive while passing all three reference
photos of the actual bike.

Usage
  ./visual.py score              score data/pending.json in place  (idempotent)
  ./visual.py score --archive    score every archived listing -> data/visual.json
  ./visual.py sheet              ranked HTML contact sheet of the archive
  ./visual.py refs               print the scores of the reference photos
"""

import argparse
import hashlib
import json
import os
import sys
import time

import numpy as np
import requests
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
PHOTOS = os.path.join(DATA, "photos")
MODELS = os.path.join(DATA, ".models")
CACHE_FILE = os.path.join(DATA, "visual-cache.json")
PENDING_FILE = os.path.join(DATA, "pending.json")
MIL_PENDING = os.path.join(DATA, "mil-pending.json")
ARCHIVE_FILES = (os.path.join(DATA, "sweep-archive.jsonl"),
                 os.path.join(DATA, "mil-archive.jsonl"))
REFDIR = os.path.join(DATA, "reference-photos")
LOG = "[visual]"

VISION_MODEL = "Qdrant/clip-ViT-B-32-vision"
TEXT_MODEL = "Qdrant/clip-ViT-B-32-text"

# Contrastive prompt groups, the colour arm and the gate all come from the
# object pack — `profiles/<object>.json`, selected by `"object"` in the item
# profile. `profiles/bicycle.json` holds exactly the values this ran on for
# its first six weeks; see objectpack.py and AGENTS.md.
#
# Each CLIP arm is (positives, negatives); the score is the softmax mass on
# the positives, so the negatives are load-bearing — a bare cosine to one
# prompt is not comparable across images.
import objectpack  # noqa: E402

PROFILE_FILE = os.path.join(DATA, "bike.json")


def profile():
    try:
        with open(PROFILE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


PACK = objectpack.load(profile())
GROUPS = objectpack.clip_groups(PACK)
COLOUR_ARMS = objectpack.colour_arms(PACK)
# The gate for the outer ring of the sweep. For the bicycle pack this is the
# original 2026-09-07 gate: it passes 27-30% of listings and all three
# reference photos. Recall is the point — the object may well be photographed
# loaded, in which case `dropbar` reads 0.01 (measured on ref 1) and only the
# colour arm saves it.
FAR_GATE = objectpack.gate(PACK, profile(), "far")
RANK = PACK.get("rank", {})
ARM_LABELS = list(objectpack.arms(PACK))

# Every cached attribute was computed under one set of arms. Change the arms
# and the cache is not stale, it is WRONG — so it is keyed by their hash and
# dropped wholesale when that moves.
ARM_HASH = hashlib.sha1(
    json.dumps(objectpack.arms(PACK), sort_keys=True).encode()).hexdigest()[:12]

_models = {}


def log(msg):
    print(f"{LOG} {msg}", flush=True)


# ------------------------------------------------------------------ features

def colour_fraction(path, spec):
    """Fraction of pixels inside a hue/sat/value box, straight off the pixels.

    CLIP was asked this ("a photo of a dark navy blue bicycle") and answered
    noise, so it is measured instead. Backgrounds do pollute it — two of the
    three reference shots are against a blue wall — which is why it is an OR
    arm and never a filter on its own.

    `spec` is a colour arm from the object pack:
        {"hue": [195, 260], "sat_min": 0.25, "val": [0.08, 0.62]}
    Hue in degrees, 0-360, red at 0. `./calibrate.py colour` reads the band
    off your own reference photos rather than making you guess it.
    """
    try:
        im = Image.open(path).convert("RGB")
    except Exception:
        return 0.0
    im.thumbnail((256, 256))
    a = np.asarray(im).astype(np.float32) / 255.0
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    mx, mn = a.max(-1), a.min(-1)
    d = mx - mn
    v = mx
    s = np.where(mx > 0, d / np.maximum(mx, 1e-6), 0)
    h = np.zeros_like(mx)
    m = d > 1e-6
    i = (mx == r) & m
    h[i] = ((g - b)[i] / d[i]) % 6
    i = (mx == g) & m
    h[i] = ((b - r)[i] / d[i]) + 2
    i = (mx == b) & m
    h[i] = ((r - g)[i] / d[i]) + 4
    h *= 60
    h0, h1 = spec.get("hue", [0, 360])
    smin = spec.get("sat_min", 0.25)
    v0, v1 = spec.get("val", [0.0, 1.0])
    inband = (h > h1) | (h < h0) if h0 > h1 else (h > h0) & (h < h1)   # wrap at red
    return float((inband & (s > smin) & (v > v0) & (v < v1)).mean())


def _text_matrix():
    """Prompt vectors, cached on disk — the text tower is only needed once."""
    cache = os.path.join(MODELS, "prompts.npz")
    key = hashlib.sha1(json.dumps(GROUPS, sort_keys=True).encode()).hexdigest()[:12]
    if os.path.exists(cache):
        z = np.load(cache, allow_pickle=True)
        if str(z["key"]) == key:
            return {g: (z[f"T_{g}"], int(z[f"n_{g}"])) for g in GROUPS}
    from fastembed import TextEmbedding
    t = TextEmbedding(model_name=TEXT_MODEL, cache_dir=MODELS)
    out, blob = {}, {"key": key}
    for g, (pos, neg) in GROUPS.items():
        T = np.array(list(t.embed(pos + neg)))
        T = T / np.linalg.norm(T, axis=1, keepdims=True)
        out[g] = (T, len(pos))
        blob[f"T_{g}"], blob[f"n_{g}"] = T, len(pos)
    os.makedirs(MODELS, exist_ok=True)
    np.savez(cache, **blob)
    return out


def embed_images(paths):
    if not paths:
        return np.zeros((0, 512), dtype=np.float32)
    if "img" not in _models:
        from fastembed import ImageEmbedding
        _models["img"] = ImageEmbedding(model_name=VISION_MODEL, cache_dir=MODELS)
    V = np.array(list(_models["img"].embed(paths)))
    return V / np.linalg.norm(V, axis=1, keepdims=True)


def attrs_for(paths):
    """-> list of {arm: score} in the order given, one key per pack arm."""
    V = embed_images(paths)
    T = _text_matrix()
    out = [{} for _ in paths]
    for g, (M, npos) in T.items():
        s = (V @ M.T) * 100.0
        e = np.exp(s - s.max(1, keepdims=True))
        p = (e / e.sum(1, keepdims=True))[:, :npos].sum(1)
        for i, val in enumerate(p):
            out[i][g] = float(val)
    for i, path in enumerate(paths):
        for arm, spec in COLOUR_ARMS.items():
            out[i][arm] = colour_fraction(path, spec)
    return out


def look_score(a):
    """0-100, for ranking the digest. Weights come from the pack's `rank`
    block; a colour arm is normalised by `norm` first because its raw value is
    a pixel fraction and saturates low (0.15 on the bicycle pack)."""
    w = RANK.get("weights") or {k: 1 for k in GROUPS}
    norms = RANK.get("norm", {})
    total = sum(w.values()) or 1
    acc = 0.0
    for arm, weight in w.items():
        v = a.get(arm, 0.0)
        n = norms.get(arm)
        if n is None and arm in COLOUR_ARMS:
            n = 0.15
        if n:
            v = min(v / n, 1.0)
        acc += weight * v
    return int(round(100 * acc / total))


def passes_gate(a):
    """The far-ring gate: any arm over its threshold."""
    return any(a.get(arm, 0) >= thr for arm, thr in FAR_GATE.items())


# ------------------------------------------------------------------ listings

def image_urls(it, n=1):
    out = []
    for im in (it.get("images") or [])[:n]:
        if isinstance(im, str):
            out.append(im if im.startswith("http") else "https:" + im)
        elif isinstance(im, dict):
            u = im.get("urls") or {}
            out.append(u.get("medium") or u.get("big") or u.get("small") or "")
    return [u for u in out if u]


def fetch_photo(iid, url):
    os.makedirs(PHOTOS, exist_ok=True)
    p = os.path.join(PHOTOS, f"{iid}_0.jpg")
    if os.path.exists(p) and os.path.getsize(p) > 1000:
        return p
    try:
        r = requests.get(url, timeout=20)
        if r.status_code == 200 and len(r.content) > 1000:
            with open(p, "wb") as f:
                f.write(r.content)
            return p
    except Exception:
        pass
    return None


def score_rows(rows, cache=None):
    """Attach _visual / _vattrs / _vpass to rows in place. Cached by listing id
    so a re-run over the same bank costs nothing."""
    cache = cache if cache is not None else load_cache()
    todo, paths = [], []
    for r in rows:
        iid = str(r.get("id"))
        if iid in cache:
            continue
        urls = image_urls(r)
        if not urls:
            cache[iid] = {"dropbar": 0.0, "steel": 0.0, "kid": 0.0, "navy": 0.0,
                          "_nophoto": True}
            continue
        p = fetch_photo(iid, urls[0])
        if p:
            todo.append(iid)
            paths.append(p)
    if paths:
        t0 = time.time()
        for iid, a in zip(todo, attrs_for(paths)):
            cache[iid] = a
        log(f"scored {len(paths)} photos in {time.time() - t0:.0f}s")
    for r in rows:
        a = cache.get(str(r.get("id")), {})
        r["_vattrs"] = {k: round(v, 3) for k, v in a.items()
                        if isinstance(v, float)}
        r["_visual"] = look_score(a)
        r["_vpass"] = bool(passes_gate(a)) if not a.get("_nophoto") else False
    return cache


def load_cache():
    """Cached attributes, dropped wholesale if the arms have been retuned.

    Every value in here was produced by one set of prompts and one colour
    band. Edit the pack and the old numbers are not stale, they are answers to
    a different question — so they go, and the next sweep re-scores."""
    if os.path.exists(CACHE_FILE):
        try:
            c = json.load(open(CACHE_FILE))
        except Exception:
            return {}
        if c.pop("_armhash", None) == ARM_HASH:
            return c
        log(f"arms changed — dropping {len(c)} cached attribute rows")
    return {}


def save_cache(cache):
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(dict(cache, _armhash=ARM_HASH), f)
    os.replace(tmp, CACHE_FILE)


def load_archive():
    out, seen = [], set()
    for f in ARCHIVE_FILES:
        if not os.path.exists(f):
            continue
        for line in open(f):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if str(d.get("id")) in seen:
                continue
            seen.add(str(d.get("id")))
            out.append(d)
    return out


# ------------------------------------------------------------------ commands

def cmd_score(args):
    cache = load_cache()
    if args.archive:
        rows = load_archive()
        log(f"scoring {len(rows)} archived listings")
        score_rows(rows, cache)
        save_cache(cache)
        rows.sort(key=lambda r: -r["_visual"])
        json.dump([{"id": r["id"], "title": r.get("title"),
                    "visual": r["_visual"], "pass": r["_vpass"],
                    "attrs": r["_vattrs"]} for r in rows],
                  open(os.path.join(DATA, "visual.json"), "w"),
                  ensure_ascii=False, indent=1)
        npass = sum(1 for r in rows if r["_vpass"])
        log(f"{npass}/{len(rows)} pass the gate ({100*npass/max(len(rows),1):.0f}%)"
            f" -> data/visual.json")
        for r in rows[:15]:
            print(f"  {r['_visual']:3d} {'PASS' if r['_vpass'] else '    '} "
                  f"{str(r.get('title'))[:60]}")
        return
    for path in (PENDING_FILE, MIL_PENDING):
        if not os.path.exists(path):
            continue
        rows = json.load(open(path))
        if not rows:
            continue
        score_rows(rows, cache)
        tmp = path + ".tmp"
        json.dump(rows, open(tmp, "w"), ensure_ascii=False)
        os.replace(tmp, path)
        npass = sum(1 for r in rows if r.get("_vpass"))
        log(f"{os.path.basename(path)}: {len(rows)} banked, {npass} pass the gate")
    save_cache(cache)


def cmd_refs(args):
    paths = sorted(os.path.join(REFDIR, f) for f in os.listdir(REFDIR)
                   if f.lower().endswith((".jpg", ".jpeg", ".png")))
    for p, a in zip(paths, attrs_for(paths)):
        print(f"{os.path.basename(p):28s} look={look_score(a):3d} "
              f"{'PASS' if passes_gate(a) else 'FAIL'}  "
              + "  ".join(f"{k}={v:.2f}" for k, v in a.items()))


def cmd_sheet(args):
    rows = load_archive()
    cache = load_cache()
    score_rows(rows, cache)
    save_cache(cache)
    rows = [r for r in rows if image_urls(r)]
    rows.sort(key=lambda r: (-r["_visual"], -(r.get("created_at") or 0)))
    if args.limit:
        rows = rows[: args.limit]
    cells = []
    for r in rows:
        url = ("https://es.wallapop.com/item/" + (r.get("web_slug") or r.get("id"))
               if r.get("web_slug") or r.get("category_id") else r.get("id"))
        img = image_urls(r)[0]
        price = (r.get("price") or {}).get("amount") if isinstance(r.get("price"), dict) \
            else r.get("price")
        city = (r.get("location") or {}).get("city") or ""
        a = r["_vattrs"]
        cells.append(
            f'<a class="c{" pass" if r["_vpass"] else ""}" href="{url}" target="_blank">'
            f'<img loading="lazy" src="{img}">'
            f'<div class="m"><b>{r["_visual"]}</b> '
            f'<span>{str(r.get("title") or "")[:44]}</span><br>'
            f'<span class="d">€{price or 0:.0f} · {city} · '
            + " · ".join(f"{k[:4]} {a.get(k, 0):.2f}" for k in ARM_LABELS)
            + '</span>'
            f'</div></a>')
    html = """<!doctype html><meta charset=utf-8>
<title>__TITLE__ — visual ranking</title>
<style>
body{background:#1a1a2e;color:#e0e0e0;font:14px/1.4 -apple-system,system-ui,sans-serif;margin:0;padding:24px}
h1{font-size:20px;font-weight:600;margin:0 0 4px}p.sub{color:#8a8aa3;margin:0 0 20px}
.g{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:14px}
a.c{background:#22223a;border-radius:10px;overflow:hidden;text-decoration:none;color:inherit;display:block;border:2px solid transparent}
a.c.pass{border-color:#64b5f6}
img{width:100%;height:190px;object-fit:cover;display:block;background:#111}
.m{padding:8px 10px}.m b{color:#64b5f6}.d{color:#8a8aa3;font-size:12px}
</style>
<h1>Visual ranking — __N__ listings</h1>
<p class="sub">Ranked by the attribute score (__ARMS__).
Blue border = passes the gate. This is a genre filter, not an identification —
scroll it with your own eyes.</p>
<div class="g">__CELLS__</div>"""
    # .replace, not %: the CSS is full of literal % signs
    html = (html.replace("__N__", str(len(rows)))
                .replace("__TITLE__", objectpack.noun(PACK).title())
                .replace("__ARMS__", " · ".join(ARM_LABELS))
                .replace("__CELLS__", "\n".join(cells)))
    out = os.path.join(DATA, "visual-sheet.html")
    open(out, "w").write(html)
    log(f"wrote {out} ({len(rows)} cells)")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("score")
    s.add_argument("--archive", action="store_true")
    s.set_defaults(fn=cmd_score)
    s = sub.add_parser("sheet")
    s.add_argument("--limit", type=int, default=0)
    s.set_defaults(fn=cmd_sheet)
    s = sub.add_parser("refs")
    s.set_defaults(fn=cmd_refs)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
