#!/usr/bin/env python3
"""
vlm.py — the recognition pass for the stolen-bike watch.

Why this exists
---------------
`visual.py` answers "what GENRE of bike is this" with CLIP. That was the right
first step and it is still the free pre-rank, but it cannot answer the question
the owner actually asks of each photo, which is "is that MY bike". Measured on 120
archived sweep listings on 2026-10-06, the whole top of the CLIP ranking — the
photos leading every digest, `_visual` 74-77, all gate PASS — is modern carbon
race bikes with sloping top tubes. CLIP ranks them first because they are road
bikes. They are not, and could never be, a navy steel <MODEL>.

A vision LLM can make that distinction, and the distinction is cheap:

    120 real sweep listings      119 scored <=2, one scored 4
    the 3 reference photos       9 / 8 / 8   (leave-one-out: the photo being
                                 scored is removed from the reference set)
    reference 1, hue-rotated 90  9   (survives a repaint)
    reference 1, cropped to the
      bare frame, no kit         9   (survives being stripped)
    reference 1 at 240 px        9   (survives a marketplace thumbnail)
    reference 1 at 45% light     9   (survives a dim garage shot)
    real <BRAND>s in FR listings   0-7, correctly graded: a dark steel
                                 level-top-tube drop-bar <BRAND> scores 7, a
                                 pink one 2, a flat-bar one 2, a frame-only 0

So the gate has the recall to survive the ways a thief changes a bike, and
enough precision that an ordinary day shows nothing at all.

Model choice is measured, not assumed. Claude Haiku 4.5 was tested on the same
set and is DISQUALIFIED: it scored reference photos 1 and 2 — the bike loaded
with its own child seat and panniers, which is the single most likely way it
gets photographed — a 2, reporting the navy frame as "lighter/grey". It also
cost MORE per image than Sonnet ($0.0017 vs $0.0014), because its minimum
cacheable prefix is larger than our reference block, so nothing cached and
every request paid full input price. Do not "optimise" this back to Haiku.

Cost, measured
--------------
$0.00113 per listing photo on claude-sonnet-5 at 384 px. The sweep banks ~906
listings/day (NOT the ~530 it used to SEND -- that confusion put the first
published figure at $20/month when it was really $31). A free per-ring CLIP
pre-gate in front of this call -- see pre_gate() -- cuts that to ~408 paid
photos/day, about **$0.46/day, $14/month**.

Two cheaper designs were tried and REJECTED, both for quality:
  - batching 12 photos per request (-43%) gave 5x the false positives, because
    the model starts comparing listings to each other instead of to the refs;
  - dropping the `reason` field (-29%) cost recall -- the best near-miss probe
    fell from 7 to 3. The reason field is doing reasoning, not decoration.
The remaining 50% is the Batch API, unbuilt because it costs the 20-minute
instant push.

`vlm_daily_budget_usd` is a hard stop: when the day's ledger passes it the
pass degrades to the CLIP ranking and says so, rather than overspending.

NOTE: this bills the same plaintext API key the observer crons use, so it WILL
show up in the owner's Anthropic top-ups. Set `vlm_enabled` false in bike.json to
turn the whole stage off.

Usage
  ./vlm.py score                 score data/pending.json in place (idempotent)
  ./vlm.py score --file X.json   score an arbitrary bank
  ./vlm.py refs                  leave-one-out sanity check on the references
  ./vlm.py spend                 what this has cost, by day

Read docs/RECOGNITION.md before changing any threshold -- especially the seven-probe table
behind the pre-gate, where a repainted bike survives on the `kid` arm ALONE
and a stripped one on `navy`/`dropbar` ALONE.
"""

import argparse
import base64
import datetime as dt
import io
import json
import os
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
PHOTOS = os.path.join(DATA, "photos")
REFDIR = os.path.join(DATA, "reference-photos")
PENDING_FILE = os.path.join(DATA, "pending.json")
MIL_PENDING = os.path.join(DATA, "mil-pending.json")
CACHE_FILE = os.path.join(DATA, "vlm-cache.json")
SPEND_FILE = os.path.join(DATA, "vlm-spend.jsonl")
BIKE_FILE = os.path.join(DATA, "bike.json")
LOG = "[vlm]"

def _norm(x):
    x = unicodedata.normalize("NFKD", (x or "").lower())
    return "".join(c for c in x if not unicodedata.combining(c))


def _brand_tokens(prof):
    """Brand name, its misspellings and the model names — the same set the
    HITS tripwire uses. extra_keywords ("yepp", "portabebe") are deliberately
    NOT here: they match accessories all day."""
    out = []
    for t in _norm(prof.get("brand", "")).split():
        if len(t) > 2:
            out.append(t)
    for kw in list(prof.get("brand_aliases", [])) + list(prof.get("model_aliases", [])):
        kw = _norm(kw).strip()
        if len(kw) > 2:
            out.append(kw)
    return sorted(set(out))


DEFAULTS = {
    "vlm_enabled": True,
    "vlm_model": "claude-sonnet-5",
    "vlm_px": 384,
    "vlm_ref_px": 448,
    "vlm_show_threshold": 4,
    "vlm_hit_threshold": 7,
    "vlm_daily_budget_usd": 0.80,
    "vlm_workers": 8,
    "vlm_pregate": True,
    "vlm_pregate_dropbar": 0.10,
    "vlm_pregate_navy": 0.05,
    "vlm_pregate_kid": 0.40,
    # The far ring keeps the original visual.py gate; see pre_gate().
    "vlm_pregate_far_dropbar": 0.35,
    "vlm_pregate_far_navy": 0.06,
}

# $/1M: input, output, cache read, cache write
PRICES = {
    "claude-sonnet-5": (2.00, 10.00, 0.20, 2.50),
    "claude-opus-5": (5.00, 25.00, 0.50, 6.25),
    "claude-haiku-4-5": (1.00, 5.00, 0.10, 1.25),
}

# The prompt is the product here. Two things in it are load-bearing and were
# each arrived at by a failed run:
#   - The band descriptions have to span the range. A first version ended with
#     "Be strict. Most photos should be 0-3." and collapsed 116 of 120 scores
#     to exactly 0 — including all three reference photos.
#   - The blue-wall warning has to be there. Both reference photos of the
#     loaded bike are shot against a blue painted garage wall, which is also
#     why visual.py's `navy` signal reads 0.18 on them; without the warning the
#     model credits the background.
PROMPT = """You are helping find ONE specific stolen bicycle in marketplace photos.
Reference photos of it are attached first — study them before judging.

THE BIKE: a <BRAND> <MODEL>, stolen in Barcelona on 2026-09-05.

Frame and fork — the part a thief cannot easily change:
- Dark navy / midnight blue gloss paint.
- Steel: thin, round, constant-diameter tubes. Not a fat aluminium frame and
  not a shaped/aero carbon frame.
- A level, horizontal top tube running straight from the head tube to the
  seat tube. Most modern road and gravel bikes slope the top tube down toward
  the back; this one does not.
- Drop handlebars. Disc brakes. Wide-tyre clearance with rack/mudguard eyelets.

Wheels: wide (~40 mm) tyres with tan / brown gumwall sidewalls.

The kit — often stripped, so its absence proves nothing, but its presence is
strong evidence: a grey-and-black Thule Yepp child seat on the rear rack,
black tubular racks front AND rear, a black VAUDE pannier, black mudguards.

One trap: the reference photos happen to be shot against a blue painted wall.
Background colour is not evidence — judge the paint on the frame tubes.

Score how plausibly the photo could be THIS bike, 0-10:
 0-1  wrong genre entirely — mtb, e-bike, city/hybrid, kids, BMX, folding,
      a spare part, a frame only, or not a bicycle
 2-3  a drop-bar bike, but clearly a different one (modern sloping-top-tube
      carbon race bike, or plainly the wrong colour)
 4-5  right genre — a steel or steel-looking drop-bar gravel/touring/cross
      bike — but nothing specific lines up
 6-7  several specific features line up: e.g. dark blue AND drop bars, or a
      level top tube on a steel drop-bar bike, or gumwall tyres plus racks
 8-10 could really be it: frame shape and colour both match, or the kit
      (child seat, twin racks, gumwalls) is recognisable

Use the whole range. Judge what you can actually see; if the photo is too
small, dark or cropped to tell, say so in the reason and score the middle."""

SCHEMA = {
    "type": "object",
    "properties": {
        # No `minimum`/`maximum`: the API rejects them on an integer property
        # ("For 'integer' type, properties maximum, minimum are not supported").
        "score": {"type": "integer", "description": "0 to 10 inclusive"},
        "reason": {"type": "string", "description": "at most 12 words"},
    },
    "required": ["score", "reason"],
    "additionalProperties": False,
}


def log(msg):
    print(f"{LOG} {msg}", flush=True)


def profile():
    p = dict(DEFAULTS)
    try:
        p.update(json.load(open(BIKE_FILE)))
    except Exception:
        pass
    return p


# ---------------------------------------------------------------- the ledger

def spend_today():
    today = dt.date.today().isoformat()
    total = 0.0
    if os.path.exists(SPEND_FILE):
        for line in open(SPEND_FILE):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("day") == today:
                total += r.get("usd", 0.0)
    return total


def record_spend(model, usage, n):
    ci, co, cr, cw = PRICES.get(model, PRICES["claude-sonnet-5"])
    usd = (usage["in"] * ci + usage["out"] * co +
           usage["cr"] * cr + usage["cw"] * cw) / 1e6
    with open(SPEND_FILE, "a") as f:
        f.write(json.dumps({"day": dt.date.today().isoformat(),
                            "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                            "model": model, "images": n, "usd": round(usd, 5),
                            **usage}) + "\n")
    return usd


# ---------------------------------------------------------------- the images

def _b64(path, px):
    from PIL import Image
    im = Image.open(path).convert("RGB")
    im.thumbnail((px, px))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=80)
    return base64.standard_b64encode(buf.getvalue()).decode()


def _img(path, px):
    return {"type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg",
                       "data": _b64(path, px)}}


def refs():
    return sorted(os.path.join(REFDIR, f) for f in os.listdir(REFDIR)
                  if f.lower().endswith((".jpg", ".jpeg", ".png")))


def ref_prefix(prof, exclude=None):
    """Numbered, individually labelled reference images, cached.

    The labels are not decoration. With the references as a bare run of image
    blocks, 4 of 12 calls answered "no bicycle image provided to evaluate" —
    the model had folded the listing photo into the reference set.
    """
    out = [{"type": "text", "text":
            "=== REFERENCE PHOTOS of the stolen bike (for comparison only, "
            "never the photo you are scoring) ==="}]
    n = 0
    for p in refs():
        if exclude and os.path.abspath(p) == os.path.abspath(exclude):
            continue
        n += 1
        out.append({"type": "text", "text": f"Reference {n}:"})
        out.append(_img(p, prof["vlm_ref_px"]))
    out[-1] = dict(out[-1], cache_control={"type": "ephemeral"})
    return out


def listing_blocks(path, prof):
    return [{"type": "text", "text":
             "=== END OF REFERENCES ===\n\nNow score THIS marketplace listing "
             "photo, a different image from every reference above:"},
            _img(path, prof["vlm_px"]),
            {"type": "text", "text":
             "Score the listing photo immediately above."}]


# ---------------------------------------------------------------- the scorer

class Scorer:
    def __init__(self, prof):
        import anthropic
        self.prof = prof
        self.model = prof["vlm_model"]
        self.client = anthropic.Anthropic()
        self.prefix = ref_prefix(prof)
        self.usage = {"in": 0, "out": 0, "cr": 0, "cw": 0}

    def one(self, path, prefix=None):
        for attempt in range(3):
            try:
                r = self.client.messages.create(
                    model=self.model, max_tokens=200,
                    system=[{"type": "text", "text": PROMPT,
                             "cache_control": {"type": "ephemeral"}}],
                    output_config={"format": {"type": "json_schema",
                                              "schema": SCHEMA}},
                    messages=[{"role": "user",
                               "content": (prefix or self.prefix)
                               + listing_blocks(path, self.prof)}])
                u = r.usage
                self.usage["in"] += u.input_tokens
                self.usage["out"] += u.output_tokens
                self.usage["cr"] += u.cache_read_input_tokens or 0
                self.usage["cw"] += u.cache_creation_input_tokens or 0
                raw = "".join(b.text for b in r.content if b.type == "text")
                v = json.loads(raw)
                v["score"] = max(0, min(10, int(v.get("score", 0))))
                return v
            except Exception as e:
                if attempt == 2:
                    # An error must never read as "not your bike". -1 is
                    # distinct from 0 and the digest shows it rather than
                    # hiding it.
                    return {"score": -1, "reason": f"scoring failed: {type(e).__name__}"}
                time.sleep(2 * (attempt + 1))


# ---------------------------------------------------------------- the bank

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            return json.load(open(CACHE_FILE))
        except Exception:
            pass
    return {}


def save_cache(c):
    tmp = CACHE_FILE + ".tmp"
    json.dump(c, open(tmp, "w"), ensure_ascii=False)
    os.replace(tmp, CACHE_FILE)


def photo_path(row):
    """Reuse the thumbnail visual.py already downloaded; fetch only if absent."""
    iid = str(row.get("id"))
    p = os.path.join(PHOTOS, f"{iid}_0.jpg")
    if os.path.exists(p) and os.path.getsize(p) > 1000:
        return p
    urls = []
    for im in (row.get("images") or [])[:1]:
        if isinstance(im, str):
            urls.append(im if im.startswith("http") else "https:" + im)
        elif isinstance(im, dict):
            u = im.get("urls") or {}
            urls.append(u.get("medium") or u.get("big") or u.get("small") or "")
    urls = [u for u in urls if u]
    if not urls:
        return None
    try:
        import requests
        r = requests.get(urls[0], timeout=20)
        if r.status_code == 200 and len(r.content) > 1000:
            os.makedirs(PHOTOS, exist_ok=True)
            open(p, "wb").write(r.content)
            return p
    except Exception:
        pass
    return None


def named(row, prof):
    """Does the TITLE contain the brand or a model name?

    A listing that says "<BRAND>" is harder evidence than any photo score, so it
    bypasses both the pre-gate and the digest cut. Whole-word match, so
    "<brand>x" does not count.
    """
    title = _norm(row.get("title", ""))
    for t in _brand_tokens(prof):
        if re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", title):
            return True
    return False


def pre_gate(row, prof):
    """The free CLIP pre-filter, in front of the paid pass.

    `visual.py` has already attached `_vattrs` by the time this runs. Rows that
    clear none of the three arms are not sent to the API at all.

    The thresholds are calibrated against SEVEN probes, not guessed — the three
    reference photos plus four degradations of reference 1 (hue-rotated 90 deg
    = repainted, cropped to the bare frame = stripped, 240 px = a marketplace
    thumbnail, 45% brightness = a dim garage). Measured 2026-10-06:

        probe            dropbar   navy    kid     saved by
        ref1 loaded        0.009   0.179   0.857   kid
        ref2 rear seat     0.013   0.158   0.516   kid
        ref3 cockpit       0.444   0.070   0.225   dropbar
        dark               0.025   0.168   0.899   kid
        lowres             0.011   0.192   0.927   kid
        repainted          0.002   0.001   0.904   kid        <-- ONLY the kid arm
        stripped           0.193   0.398   0.166   navy       <-- kid arm is NO help

    Three arms because there are three failure modes and each one is covered by
    a different arm. **A repainted bike survives on `kid` alone and a stripped
    one on `navy`/`dropbar` alone, so never collapse this to fewer arms and
    never raise a threshold without re-running the probe table.**

    At the shipped values the worst-case margin across all seven probes is
    2.3x, and the gate passes 54.3% of the 26,279-listing archive — so it
    roughly halves the bill. Checked against the 120-listing benchmark: of the
    30 it would have killed, **none** had been scored >= 3 by the VLM.

    The gate is PER RING. Inside `sweep_near_km` the recall-tuned 3-arm gate
    above is used (55.7% pass), because that is where the bike actually is.
    Outside it, the far ring keeps the ORIGINAL `visual.py` gate it has had
    since 2026-09-07 (29.7% pass) — not a new restriction, just a refusal to
    pay to recognise listings the far ring was never going to show: it is
    capped at `sweep_far_max` (12) per digest no matter what they score.
    That one split is the difference between $31/month and $14/month.
    """
    if not prof.get("vlm_pregate", True):
        return True
    a = row.get("_vattrs")
    if not a:
        # No CLIP score yet (visual.py has not run, or the photo is missing).
        # Fail OPEN: pay for the call rather than silently skip a listing.
        return True
    if row.get("_ring") == "far":
        return (a.get("dropbar", 0) >= prof.get("vlm_pregate_far_dropbar", 0.35)
                or a.get("navy", 0) >= prof.get("vlm_pregate_far_navy", 0.06))
    return (a.get("dropbar", 0) >= prof.get("vlm_pregate_dropbar", 0.10)
            or a.get("navy", 0) >= prof.get("vlm_pregate_navy", 0.05)
            or a.get("kid", 0) >= prof.get("vlm_pregate_kid", 0.40))


def score_rows(rows, prof=None, cache=None):
    """Attach _vlm / _vlm_reason to rows in place. Returns a status string."""
    prof = prof or profile()
    if not prof.get("vlm_enabled", True):
        return "disabled"
    cache = cache if cache is not None else load_cache()

    todo, gated = [], 0
    for r in rows:
        iid = str(r.get("id"))
        if iid in cache:
            continue
        p = photo_path(r)
        if p is None:
            cache[iid] = {"score": -1, "reason": "no photo"}
            continue
        if not named(r, prof) and not pre_gate(r, prof):
            gated += 1
            cache[iid] = {"score": 0, "reason": "not a drop-bar, navy or "
                                                "loaded bike (CLIP pre-gate)",
                          "_pregated": True}
            continue
        todo.append((iid, p))
    if gated:
        log(f"CLIP pre-gate: {gated} photos not sent to the API "
            f"(saved ~${gated * 0.00113:.3f})")

    status = "ok"
    if todo:
        budget = prof.get("vlm_daily_budget_usd", 1.0)
        already = spend_today()
        # Price the batch before buying it, at the measured per-image rate.
        est = len(todo) * 0.00113
        if already + est > budget:
            afford = max(0, int((budget - already) / 0.00113))
            if afford == 0:
                log(f"daily budget ${budget:.2f} reached (${already:.3f} spent)"
                    f" — {len(todo)} photos left unscored, falling back to CLIP")
                for r in rows:
                    a = cache.get(str(r.get("id")))
                    if a:
                        r["_vlm"], r["_vlm_reason"] = a["score"], a["reason"]
                return "budget"
            log(f"daily budget ${budget:.2f}: scoring {afford} of {len(todo)}")
            todo = todo[:afford]
            status = "partial"

        s = Scorer(prof)
        t0 = time.time()
        # One serial call first so the reference block lands in the cache
        # before the rest fan out and all pay the write price.
        cache[todo[0][0]] = s.one(todo[0][1])
        if len(todo) > 1:
            with ThreadPoolExecutor(max_workers=prof.get("vlm_workers", 8)) as ex:
                for (iid, _), v in zip(todo[1:], ex.map(lambda t: s.one(t[1]), todo[1:])):
                    cache[iid] = v
        usd = record_spend(s.model, s.usage, len(todo))
        log(f"scored {len(todo)} photos in {time.time() - t0:.0f}s "
            f"for ${usd:.4f} (${spend_today():.3f} today)")

    for r in rows:
        a = cache.get(str(r.get("id")), {})
        if a:
            r["_vlm"] = a.get("score", -1)
            r["_vlm_reason"] = a.get("reason", "")
    return status


# ---------------------------------------------------------------- commands

def cmd_score(args):
    prof = profile()
    if not prof.get("vlm_enabled", True):
        log("vlm_enabled is false in bike.json — nothing to do")
        return
    cache = load_cache()
    paths = [args.file] if args.file else [PENDING_FILE, MIL_PENDING]
    for path in paths:
        if not os.path.exists(path):
            continue
        rows = json.load(open(path))
        if not rows:
            continue
        status = score_rows(rows, prof, cache)
        tmp = path + ".tmp"
        json.dump(rows, open(tmp, "w"), ensure_ascii=False)
        os.replace(tmp, path)
        th = prof["vlm_show_threshold"]
        shown = sum(1 for r in rows if (r.get("_vlm") or -1) >= th)
        log(f"{os.path.basename(path)}: {len(rows)} banked, {shown} at >= {th} "
            f"({status})")
    save_cache(cache)


def cmd_refs(args):
    """Leave-one-out: the reference being scored is removed from the prefix,
    otherwise the model is marking its own homework and says so."""
    prof = profile()
    s = Scorer(prof)
    for p in refs():
        v = s.one(p, prefix=ref_prefix(prof, exclude=p))
        print(f"  {v['score']:3d}  {os.path.basename(p):28s} {v['reason']}")
    print(f"\n  cost ${record_spend(s.model, s.usage, len(refs())):.4f}")


def cmd_spend(args):
    days = {}
    if os.path.exists(SPEND_FILE):
        for line in open(SPEND_FILE):
            try:
                r = json.loads(line)
            except Exception:
                continue
            d = days.setdefault(r["day"], {"usd": 0.0, "images": 0})
            d["usd"] += r.get("usd", 0.0)
            d["images"] += r.get("images", 0)
    if not days:
        print("no spend recorded yet")
        return
    tot = 0.0
    for day in sorted(days):
        d = days[day]
        tot += d["usd"]
        print(f"  {day}  {d['images']:5d} photos  ${d['usd']:.4f}")
    print(f"  {'total':10s} {'':5s}          ${tot:.4f}"
          f"   (budget ${profile()['vlm_daily_budget_usd']:.2f}/day)")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("score")
    s.add_argument("--file")
    s.set_defaults(fn=cmd_score)
    s = sub.add_parser("refs")
    s.set_defaults(fn=cmd_refs)
    s = sub.add_parser("spend")
    s.set_defaults(fn=cmd_spend)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
