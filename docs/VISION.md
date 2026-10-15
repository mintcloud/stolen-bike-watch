# The photo channel — what was tried, what was measured, what shipped

Written 2026-09-07 after a bike shop told the owner to widen the search. The
question was whether the widening could be paid for with an **image match**
against the three reference photos of the bike. Short answer: not the way
everyone means it. The long answer is below, with the numbers, because the
next person to have this idea should not have to re-run the experiment.

Test set: the **777 listing photos** already archived by the sweep
(`data/sweep-archive.jsonl` + `data/mil-archive.jsonl`, 833 listings, 777 with
a downloadable photo), plus the 3 reference photos in
`data/reference-photos/`. All numbers are cosine similarity between L2-
normalised embeddings, CPU, no GPU on this box.

---

## 1. What failed: instance-level image match

**CLIP ViT-B/32** (`Qdrant/clip-ViT-B-32-vision`, 512-d):

| | cosine |
|---|---|
| ref 1 ↔ ref 2 (**the same bike**, different angle) | 0.835 |
| ref 1 ↔ ref 3 (**the same bike**, different angle) | 0.801 |
| ref 2 ↔ ref 3 (**the same bike**, different angle) | 0.789 |
| best of 777 random listings vs any ref | **0.889** |
| 99th percentile of the 777 | 0.858 |

The top ten by similarity to the owner's bike were *Bicicleta ST 100 Rockrider*,
*B-Pro ZS2 Roja*, *Rockrider Blanca*, *Rockrider Talla XL*, a Conor city bike,
a Triban, a folding Decathlon, a Thompson, a Cannondale and a Btwin MTB. Not
one drop-bar bike among them. **A random Decathlon mountain bike looks more
like the owner's bike to CLIP than the owner's bike does.**

**Unicom ViT-B/32** (`Qdrant/Unicom-ViT-B-32`, trained for instance retrieval,
which is exactly the task): worse. Same-bike pairs 0.372 / 0.498 / 0.613;
best random listing 0.652. It ranks a stranger's bike above the same bike shot
from the other side.

Why: these are *global scene* embeddings. Two photos of a bike differ mostly in
background, lighting, crop and angle, and all of that dominates a 512-d vector
long before the paint colour and the handlebar shape get a vote. Instance-level
retrieval needs either local-feature matching (SIFT/LoFTR on the head badge and
the decals) or a model fine-tuned on bikes. Neither exists here and a marketplace
thumbnail rarely resolves a head badge anyway.

Model files for Unicom were deleted after the test. The finding is the artefact.

## 2. What shipped: attribute filtering

CLIP is bad at *"is this the same object"* and good at *"what kind of object is
this"*. So it is used zero-shot on the second question, with contrastive prompt
groups (a bare cosine to one prompt is not comparable across images — the
softmax against the negatives is).

**`dropbar`** — "a bicycle with drop handlebars, a road or gravel bike" against
seven negatives (mtb / city / e-bike / folding / kid / BMX / spare part).
Validated: the top 25 of 777 are *Merida Scultura, Sunn carretera, BMC
Teammachine, Scott carbon, SAVA, Specialized Roubaix, Megamo, BMC Roadmachine,
Wilier, Trek Madone…* — every one a road or gravel bike, no misses in the top
25. Only ~10% of the pile scores above 0.575.

**`navy`** — the fraction of pixels in the dark-navy band (H 195–260°, S > 0.25,
V 0.08–0.62), measured **on the raw pixels**, not asked of CLIP. CLIP's colour
and tyre answers were noise: "a bicycle with tan brown gumwall sidewall tires"
had a **median of 0.37 across the whole pile** and fired hardest on BMX and
kids' bikes. Refs score 0.070 / 0.158 / 0.179 against a pile median of 0.011
(90th pct 0.094) — but two of the three reference shots are against a blue
painted wall, so this is a supporting signal, never a filter on its own.

**`steel`** — slim steel touring tube vs carbon aero vs fat aluminium. Weak.
Weighted 0.15 and used only to break ties.

```
look = 100 · (0.55·dropbar + 0.30·min(navy/0.15, 1) + 0.15·steel)     0–100
gate = dropbar ≥ 0.35  OR  navy ≥ 0.06
```

The gate passes **209/833 = 25%** of the archive and **all three** reference
photos. It is deliberately tuned for recall, not precision: reference photo 1
scores `dropbar = 0.01`, because the bike is buried under a child seat, two
racks and a pannier — which is exactly how a thief who cannot be bothered to
strip it would photograph it. In that case only `navy` saves the listing, so
the OR is load-bearing. Do not turn it into an AND.

## 3. Where it is used

- **Ranking.** Both digests now sort by `_visual` before `_score`. For a sweep
  listing the text score is almost entirely context — distance, price, date —
  and context was never the case; the picture is. The score and the distance
  ride in the photo caption (`€450 · photo 54 · 306 km — …`) so the ranking is
  auditable instead of a black box.
- **Paying for the widened radius.** The sweep now runs to
  `sweep_radius_km` = 400 km with an inner ring at `sweep_near_km` = 150 km.
  Inside the inner ring nothing changed: every bike is banked and shown,
  because at that range your eye is the filter and it is a better one than any
  model. Outside it, only listings that clear the gate are eligible, and of
  those only the best `sweep_far_max` (12) per digest are shown. Measured on a
  live scan: 8 near + 59 far banked, 12 far shown, 47 archived unseen.
- **The cap, not the gate, is the brake.** The gate is tuned for recall, so on
  a backlog it still passes dozens. `sweep_far_max` bounds the digest no matter
  how the gate drifts. Everything held back goes to `sweep-archive.jsonl` with
  `_held` set — nothing is dropped, same rule as the old top-N cap that lost 26
  listings on 2026-09-05.

## 4. Operating it

```bash
./visual.py refs                 # what the reference photos score  (sanity check)
./visual.py score                # score the banks in place — the digest calls this
./visual.py score --archive      # score everything ever archived -> data/visual.json
./visual.py sheet                # dark HTML contact sheet of the archive, ranked
```

Runs in its own `.venv-vision` (fastembed + onnxruntime; they have no business
in the agent's venv) and is called by `subprocess`, `nice -n 10`, never
imported — a broken model download must not be able to take the digest down
with it. If it fails the digest goes out unranked, which is what it did before
this existed.

Cost: **777 photos embedded in 38 s** on 4 CPU cores, ~20 photos/s. A digest of
120 new photos is ~15 s of CPU plus the downloads. Models live in
`data/.models` (581 MB, gitignored), thumbnails in `data/photos` (gitignored).

## 5. What this still cannot do

It cannot tell you that a listing **is** your bike. It tells you a listing is a
drop-bar bike that is somewhat blue, which is a genre, and Spain has thousands.
The recognition is still yours — the model's whole job is to make sure the 30
photos you actually look at are the 30 worth looking at, and to make a 400 km
radius survivable. If something looks right, it goes to the the police on the
existing police report. Do not message the seller.
