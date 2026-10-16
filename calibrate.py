#!/usr/bin/env python3
"""calibrate.py — derive the pre-gate thresholds from YOUR photos.

The thresholds in `profiles/bicycle.json` are not defaults, they are
measurements: seven probes, one bike, one afternoon. They do not transfer to
your object, and a guessed threshold fails silently — you do not see the
listing it cut.

So this does the measuring. It builds the same four degradations the bicycle
pack was tuned against (repainted, cropped, thumbnailed, dimmed), scores every
arm on every probe, scores a sample of real listings as the background, and
then searches for the thresholds that keep every probe alive with margin while
letting through as little of the background as possible.

    ./calibrate.py probes          build the degradations, print the arm table
    ./calibrate.py colour          read the colour band off your photos
    ./calibrate.py arms            propose thresholds (add --write to apply)
    ./calibrate.py prompt          print the assembled vision-model prompt

Runs under .venv-vision, like visual.py — it re-execs itself there if needed.

The probes are not decoration. Each one is a way a thief disposes of a thing:
a repaint, a strip, a bad phone photo in a dark garage. An arm that only
survives the clean studio shot is an arm that will miss the listing.
"""

import argparse
import copy
import itertools
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
VISION_PY = os.path.join(HERE, ".venv-vision", "bin", "python")

# fastembed lives in .venv-vision and has no business in the cron venv.
try:
    import numpy as np
    from PIL import Image, ImageEnhance
    import fastembed  # noqa: F401
except ImportError:
    if os.path.exists(VISION_PY) and os.environ.get("_CAL_REEXEC") != "1":
        os.environ["_CAL_REEXEC"] = "1"
        os.execv(VISION_PY, [VISION_PY, os.path.abspath(__file__)] + sys.argv[1:])
    raise SystemExit("[calibrate] needs .venv-vision:\n"
                     "  python3 -m venv .venv-vision && "
                     ".venv-vision/bin/pip install fastembed pillow numpy requests")

sys.path.insert(0, HERE)
import objectpack                       # noqa: E402
import visual                           # noqa: E402

PROFILE_FILE = os.path.join(DATA, "bike.json")
PROBE_DIR = os.path.join(DATA, "probes")
MARGIN = 2.0          # near ring: a probe must clear an arm by this much
FAR_MARGIN = 1.2      # far ring: alive is enough -- see cmd_arms()


def log(m):
    print(f"[calibrate] {m}", flush=True)


def profile():
    return objectpack.read_json(PROFILE_FILE)


# ------------------------------------------------------------------- probes

def hue_rotate(im, deg=90):
    h, s, v = im.convert("HSV").split()
    h = h.point(lambda p: (p + int(deg / 360 * 255)) % 256)
    return Image.merge("HSV", (h, s, v)).convert("RGB")


def build_probes():
    """The reference photos, plus four degradations of the first one.

    Repaint, strip, thumbnail, dark. If your object has a fifth plausible
    disposal — a respray into a different colour family, a part swap, a
    sticker bomb — add it here and re-run; the search below will account for
    it automatically.
    """
    refs = visual.ref_paths() if hasattr(visual, "ref_paths") else sorted(
        os.path.join(visual.REFDIR, f) for f in os.listdir(visual.REFDIR)
        if f.lower().endswith((".jpg", ".jpeg", ".png")))
    if not refs:
        raise SystemExit(f"[calibrate] no photos in {visual.REFDIR} — put 3 in "
                         "(a full side, the back, a close detail) and re-run.")
    os.makedirs(PROBE_DIR, exist_ok=True)
    out = []
    for i, r in enumerate(refs, 1):
        out.append((f"reference {i} — {os.path.basename(r)}", r, True))
    base = Image.open(refs[0]).convert("RGB")
    w, h = base.size
    variants = {
        "repainted (hue +90°)": hue_rotate(base, 90),
        "cropped to the body": base.crop((int(w * .04), int(h * .34),
                                          int(w * .62), int(h * .97))),
        "240 px thumbnail": _thumb(base, 240),
        "dimmed to 45%": ImageEnhance.Brightness(base).enhance(0.45),
    }
    for name, im in variants.items():
        p = os.path.join(PROBE_DIR, name.split(" (")[0].replace(" ", "_") + ".jpg")
        im.save(p, quality=92)
        out.append((name, p, False))
    return out


def _thumb(im, px):
    s = im.copy()
    s.thumbnail((px, px))
    return s


def probe_table(pack):
    probes = build_probes()
    scores = visual.attrs_for([p for _, p, _ in probes])
    rows = [(name, s, clean) for (name, _, clean), s in zip(probes, scores)]
    arms = list(objectpack.arms(pack))
    w = max(len(n) for n, _, _ in rows) + 2
    print(f"\n{'probe':{w}}" + "".join(f"{a:>12}" for a in arms))
    for name, s, _ in rows:
        print(f"{name:{w}}" + "".join(f"{s.get(a, 0):>12.3f}" for a in arms))
    return rows, arms


# --------------------------------------------------------------- background

def background(limit=1500):
    """Real listings, for the pass-rate half of the trade-off.

    Reads the attribute cache first — every listing the sweep has already seen
    is free. A brand-new install has none, which is the one case where you have
    to run a sweep before you can calibrate.
    """
    cache = visual.load_cache()
    rows = [v["attrs"] if isinstance(v, dict) and "attrs" in v else v
            for v in cache.values()]
    rows = [r for r in rows if isinstance(r, dict) and r]
    if not rows:
        raise SystemExit("[calibrate] no cached listing attributes yet. Run a "
                         "sweep first (./run.sh --send, then ./visual.py "
                         "score) so there is a background to measure against.")
    return rows[-limit:]


def pass_rate(rows, gate):
    if not rows:
        return 1.0
    n = sum(1 for r in rows if any(r.get(a, 0) >= t for a, t in gate.items()))
    return n / len(rows)


# ------------------------------------------------------------------- search

def candidates(rows, arm, margin=MARGIN):
    """Thresholds worth trying for one arm: just under each probe value, so
    every candidate is a threshold that actually changes which probes pass."""
    vals = sorted({round(s.get(arm, 0) / margin, 4) for _, s, _ in rows})
    vals = [v for v in vals if v > 0.0005]
    return (vals or [0.05])[:14]


def covered(rows, gate, clean_only=False, margin=MARGIN):
    """-> {probe: [arms that keep it alive with margin]}"""
    out = {}
    for name, s, clean in rows:
        if clean_only and not clean:
            continue
        out[name] = [a for a, t in gate.items() if s.get(a, 0) >= t * margin]
    return out


def search(rows, arms, bg, clean_only=False, margin=MARGIN):
    """Cheapest gate (lowest background pass rate) that keeps every probe
    alive with MARGIN to spare. Brute force: a handful of arms, a dozen
    candidates each, and the objective is a single number."""
    grids = [candidates(rows, a, margin) for a in arms]
    best, best_rate = None, 2.0
    for combo in itertools.product(*grids):
        gate = dict(zip(arms, combo))
        cov = covered(rows, gate, clean_only, margin)
        if any(not v for v in cov.values()):
            continue
        r = pass_rate(bg, gate)
        if r < best_rate:
            best, best_rate = gate, r
    return best, best_rate


def cmd_arms(args):
    prof = profile()
    pack = objectpack.load(prof)
    rows, _all_arms = probe_table(pack)
    arms = objectpack.gate_arms(pack)
    skipped = [a for a in _all_arms if a not in arms]
    if skipped:
        log(f"ranking-only arms, not gated on: {', '.join(skipped)}")
    bg = background()
    log(f"background: {len(bg)} cached listings")

    cur_near = objectpack.gate(pack, prof, "near")
    cur_far = objectpack.gate(pack, prof, "far")
    print(f"\ncurrent near {cur_near}  -> {pass_rate(bg, cur_near):.1%} of listings paid for")
    print(f"current far  {cur_far}  -> {pass_rate(bg, cur_far):.1%}")
    cov = covered(rows, cur_near)
    dead = [n for n, v in cov.items() if not v]
    if dead:
        print("\n!! the CURRENT near gate does not cover, with margin:")
        for n in dead:
            print(f"   - {n}")

    near, nr = search(rows, arms, bg)
    # The far ring is capped per digest no matter what it scores, so paying
    # full recall price for it buys almost nothing. It gets the thinner gate:
    # the undegraded references have to survive, and that is all.
    far, fr = search(rows, arms, bg, clean_only=True, margin=FAR_MARGIN)
    if not near:
        raise SystemExit("[calibrate] no gate covers every probe. Your arms do "
                         "not describe this object — add or change an arm in "
                         "the pack (see AGENTS.md) and re-run.")
    print(f"\nproposed near {near}  -> {nr:.1%}")
    print(f"proposed far  {far}  -> {fr:.1%}")

    print("\nwhich arm keeps each probe alive (near gate):")
    for name, as_ in covered(rows, near).items():
        flag = "   <-- ONLY arm" if len(as_) == 1 else ""
        print(f"  {name:38} {', '.join(as_)}{flag}")
    only = sorted({a for v in covered(rows, near).values() if len(v) == 1 for a in v})
    if only:
        print(f"\nEach of {', '.join(only)} is, for at least one probe, the only "
              f"thing between you\nand missing it. That is what the arms are for. "
              f"Do not collapse them, and do not\nraise one without re-running "
              f"this.")

    if not args.write:
        print("\n(nothing written — re-run with --write to apply)")
        return
    new = copy.deepcopy(prof)
    for a, t in near.items():
        new[f"vlm_pregate_{a}"] = round(t, 4)
    for a, t in far.items():
        new[f"vlm_pregate_far_{a}"] = round(t, 4)
    with open(PROFILE_FILE, "w") as f:
        json.dump(new, f, indent=2, ensure_ascii=False)
    log(f"wrote {len(near) + len(far)} thresholds into {PROFILE_FILE}")
    log("the attribute cache is keyed by the ARM definitions, not the "
        "thresholds, so nothing is re-scored — the next digest just cuts "
        "differently.")


# ------------------------------------------------------------------- colour

def cmd_colour(args):
    """Read the dominant hue band off the reference photos.

    Takes the centre half of each photo (the object, usually) and reports the
    band that covers most of its saturated pixels. It is a starting point, not
    an answer: check it against a photo shot somewhere else, because a blue
    wall behind a blue bike will hand you the wall.
    """
    refs = sorted(os.path.join(visual.REFDIR, f) for f in os.listdir(visual.REFDIR)
                  if f.lower().endswith((".jpg", ".jpeg", ".png")))
    hs = []
    for r in refs:
        im = Image.open(r).convert("RGB")
        w, h = im.size
        im = im.crop((w // 4, h // 4, w * 3 // 4, h * 3 // 4))
        im.thumbnail((200, 200))
        a = np.asarray(im.convert("HSV")).astype(np.float32)
        hue, sat, val = a[..., 0] * 360 / 255, a[..., 1] / 255, a[..., 2] / 255
        m = (sat > 0.25) & (val > 0.08) & (val < 0.92)
        hs.append(hue[m])
    if not hs or not len(np.concatenate(hs)):
        raise SystemExit("[calibrate] no saturated pixels — a black, white or "
                         "silver object has no colour arm. Drop it from the "
                         "pack and lean on the CLIP arms.")
    allh = np.concatenate(hs)
    hist, edges = np.histogram(allh, bins=36, range=(0, 360))
    peak = int(np.argmax(hist))
    lo, hi = edges[max(0, peak - 3)], edges[min(36, peak + 4)]
    print(f"\ndominant hue across {len(refs)} photos: {edges[peak]:.0f}-{edges[peak+1]:.0f}°")
    print(f"suggested arm:\n")
    print(json.dumps({"type": "colour", "hue": [round(float(lo)), round(float(hi))],
                      "sat_min": 0.25, "val": [0.08, 0.62]}, indent=2))
    print("\nPaste that into the arm in profiles/<object>.json, then run "
          "./calibrate.py arms.\nIf the photos share a coloured background, "
          "this is measuring the background.")


def cmd_probes(args):
    probe_table(objectpack.load(profile()))
    print(f"\nprobe images written to {PROBE_DIR}")


def cmd_prompt(args):
    sys.path.insert(0, HERE)
    import vlm
    print(vlm.build_prompt(vlm.profile()))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probes").set_defaults(fn=cmd_probes)
    sub.add_parser("colour").set_defaults(fn=cmd_colour)
    sub.add_parser("prompt").set_defaults(fn=cmd_prompt)
    a = sub.add_parser("arms")
    a.add_argument("--write", action="store_true",
                   help="apply the proposed thresholds to data/bike.json")
    a.set_defaults(fn=cmd_arms)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
