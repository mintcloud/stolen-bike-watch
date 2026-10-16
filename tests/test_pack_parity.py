#!/usr/bin/env python3
"""The bicycle pack must keep reproducing the constants this repo ran on.

Six weeks of tuning is written down in `profiles/bicycle.json` and nowhere
else now. These are the values that produced the measurements in
docs/RECOGNITION.md; if an edit moves one, that document stops being true and
this test says so.

    python3 tests/test_pack_parity.py        (no pytest needed)
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import objectpack  # noqa: E402

PACK = objectpack.load({"object": "bicycle"})
fails = []


def eq(name, got, want):
    if got != want:
        fails.append(f"{name}\n     got: {got}\n    want: {want}")


eq("category_ids", objectpack.category_ids(PACK), {17000})
eq("sweep_queries", objectpack.sweep_queries(PACK),
   (("bicicleta", 5), ("bici", 5), ("mountain bike", 2), ("bicicleta electrica", 2)))
eq("head words", objectpack.words(PACK, "head"),
   ("bicicleta", "bici", "bike", "mtb", "btt"))
eq("generic words[0:4]", objectpack.words(PACK, "generic")[:4],
   ("bicicleta", "bici", "bike", "mtb"))
eq("parts words count", len(objectpack.words(PACK, "parts")), 49)
eq("'cuadro' is NOT a parts word (a stripped frame is what a thief sells)",
   "cuadro" in objectpack.words(PACK, "parts"), False)

eq("gate arms", objectpack.gate_arms(PACK), ["dropbar", "navy", "kid"])
eq("near gate", objectpack.gate(PACK, {}, "near"),
   {"dropbar": 0.10, "navy": 0.05, "kid": 0.40})
eq("far gate", objectpack.gate(PACK, {}, "far"), {"dropbar": 0.35, "navy": 0.06})
eq("an item profile overrides the pack",
   objectpack.gate(PACK, {"vlm_pregate_kid": 0.5}, "near")["kid"], 0.5)

arms = objectpack.arms(PACK)
eq("three CLIP arms + one colour arm", sorted(arms), ["dropbar", "kid", "navy", "steel"])
eq("navy is measured off pixels, not asked of CLIP", arms["navy"]["type"], "colour")
eq("navy band", (arms["navy"]["hue"], arms["navy"]["sat_min"], arms["navy"]["val"]),
   ([195, 260], 0.25, [0.08, 0.62]))
eq("dropbar negatives still carry the 'not a whole bicycle' arm",
   any("not a whole bicycle" in n for n in arms["dropbar"]["negatives"]), True)

# The prompt must still assemble, and must still span the range -- a prompt
# that tells the model to be strict collapsed 116 of 120 scores to zero.
p = PACK["prompt"]
for band in ("0-1", "2-3", "4-5", "6-7", "8-10"):
    eq(f"prompt keeps the {band} band", band in p["bands"], True)
eq("prompt does not tell the model to be strict",
   "be strict" in (p["bands"] + p["closing"]).lower(), False)
eq("label line takes the item headline", "{headline}" in p["label_line"], True)

# Every shipped pack has to be loadable and internally consistent.
for name in objectpack.available():
    pk = objectpack.load({"object": name})
    for ring in ("near", "far"):
        for arm in objectpack.gate(pk, {}, ring):
            if arm not in objectpack.arms(pk):
                fails.append(f"{name}: {ring} gate names '{arm}', which is not an arm")
    for arm in objectpack.gate_arms(pk):
        if arm not in objectpack.arms(pk):
            fails.append(f"{name}: gate_arms names '{arm}', which is not an arm")
    for arm, spec in objectpack.arms(pk).items():
        kind = spec.get("type", "clip")
        if kind == "clip" and not (spec.get("positives") and spec.get("negatives")):
            fails.append(f"{name}/{arm}: a CLIP arm needs positives AND negatives "
                         f"-- a bare cosine is not comparable across images")
        if kind == "colour" and "hue" not in spec:
            fails.append(f"{name}/{arm}: a colour arm needs a hue band")
    for k in ("intro", "label_line", "scoring", "bands", "closing"):
        if k not in pk.get("prompt", {}):
            fails.append(f"{name}: prompt is missing '{k}'")

if fails:
    print(f"FAILED ({len(fails)}):")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print(f"pack parity OK — {len(objectpack.available())} packs checked "
      f"({', '.join(objectpack.available())})")
