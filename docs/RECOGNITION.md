# The recognition channel — what was measured, what shipped

Written 2026-10-06, after the owner said the sweep digest had become too long to
read and that he was close to stopping. Sibling of `VISION.md`, which
documents the CLIP channel this one sits on top of. **Read both before
touching `vlm.py` or any `vlm_*` key in `bike.json`.**

---

## 1. The diagnosis: the digest was long because the near ring was ungated

Measured on `data/sweep-archive.jsonl` (25,626 rows) on 2026-10-06:

| | |
|---|---|
| photos sent to Telegram per day | **~530** (min 446, max 706 over 24 days) |
| sent from the near ring (≤150 km) | 13,093 |
| ...of which **failed** the CLIP gate | **9,234 — 70%** |
| sent from the far ring (150–400 km) | 2,431, all gate passes |

The far ring was gated and capped from day one. The near ring never was —
`sweep_near_km` meant "show everything, your eye is the filter". That was the
right call when the alternative was a keyword alert, and it is what made the
digest 500 photos long: seven in ten of them were not even drop-bar bikes.

## 2. Why the obvious fix — gate the near ring with CLIP — is not enough

It would have cut the digest ~70% for free. It was rejected because CLIP's
ranking is measurably ranking the wrong thing, and its recall on *this bike*
depends on an accident.

**The ranking.** Of 120 archived listings scored by both systems, the entire
top of the CLIP ranking — `_visual` 74–77, every one a gate PASS, the photos
that led every digest the owner opened — is modern carbon race bikes:

```
clip 77  Cube Attain Race GTC Ultegra      carbon, sloping top tube
clip 77  Bicicleta de Carretera Cannondale carbon, sloping top tube
clip 76  Orbea Onix Carbono T:54           carbon, sloping top tube
clip 76  Bicicleta Giant TCR Carbono Di2   carbon, sloping top tube
```

CLIP ranks them first because they are road bikes, which is the only question
it was asked. None of them could ever be a navy steel <MODEL>.

**The recall.** The reference photos score, from `./visual.py refs`:

| reference | dropbar | navy | passes on |
|---|---|---|---|
| 01 drive side, loaded | 0.01 | **0.18** | navy only |
| 02 rear, child seat | 0.01 | **0.16** | navy only |
| 03 cockpit, front rack | 0.44 | 0.07 | dropbar |

Two of the three pass the gate **only** on `navy`, and both of those are shot
against a blue painted garage wall. The bike photographed loaded, against a
non-blue background — the single most likely way a lazy thief lists it — may
score under both thresholds. CLIP cannot be the thing standing in front of the
recognition pass.

## 2a. The pre-gate: where CLIP earns its place after all

Section 2 says CLIP cannot stand in front of the recognition pass. That was
right about the gate *as it existed*, and wrong as a general claim — once the
failure modes are measured, a cheap gate with real margin is constructible.
The measurement that was missing is what the CLIP attributes do to a
**degraded** reference photo:

| probe | dropbar | navy | kid | saved by |
|---|---|---|---|---|
| ref1 loaded | 0.009 | 0.179 | 0.857 | kid |
| ref2 rear seat | 0.013 | 0.158 | 0.516 | kid |
| ref3 cockpit | **0.444** | 0.070 | 0.225 | dropbar |
| dark (45% brightness) | 0.025 | 0.168 | 0.899 | kid |
| lowres (240 px) | 0.011 | 0.192 | 0.927 | kid |
| **repainted** (hue +90°) | 0.002 | **0.001** | **0.904** | **kid alone** |
| **stripped** (bare frame) | 0.193 | **0.398** | 0.166 | **navy/dropbar alone** |

Three failure modes, and **each is covered by a different arm**. A repainted
bike survives on `kid` and nothing else; a stripped one survives on
`navy`/`dropbar` and nothing else. This is why the gate has three arms and why
collapsing it to fewer, or raising a single threshold, silently destroys
recall for one specific disposal strategy.

Shipped, for the **near** ring:

```
dropbar >= 0.10  OR  navy >= 0.05  OR  kid >= 0.40
```

- Worst-case margin across all seven probes: **2.3×**.
- Passes **55.7%** of the 13,141 archived near-ring listings.
- Checked against the 120-listing benchmark: of the 30 it would have killed,
  **none** had been scored ≥3 by the VLM.

The **far** ring (150–400 km) keeps the *original* `visual.py` gate,
`dropbar >= 0.35 OR navy >= 0.06`, which has governed it since 2026-09-07.
That is not a new restriction — it is the gate the far ring has always had.
It passes 29.7%, and it does **not** cover the repaint case. The trade is
deliberate and worth stating plainly: **a repainted bike listed 150–400 km
away can now be missed.** It is accepted because the far ring is capped at
`sweep_far_max` (12) per digest regardless of score, so paying full recall
price for it buys at most 12 slots — and that one split is the difference
between $31/month and $14/month.

Two bypasses, both unconditional:

- A listing whose **title** matches the brand or a model name is never
  pre-gated and never cut, at any score. The name is harder evidence than the
  picture. Whole-word match, so `<brand>x` does not count.
- A listing with **no `_vattrs`** (CLIP has not run, or the photo is missing)
  **fails open** — it is paid for rather than silently skipped.

To re-tune: change a threshold, then re-run the probe table above and the
pass-rate measurement. Never one without the other.

## 3. What shipped: a vision-LLM recognition pass

`vlm.py` sends each listing photo to Claude with the three reference photos
cached in the prefix, and asks for a 0–10 plausibility score plus a one-line
reason. Everything below `vlm_show_threshold` is archived with `_held`, never
shown, never dropped.

### Precision — 120 archived sweep listings, stratified

Strata: the 40 highest-CLIP listings, 40 random CLIP passes, 40 random CLIP
fails.

| score | 0 | 1 | 2 | 4 |
|---|---|---|---|---|
| listings | 64 | 49 | 6 | 1 |

**119 of 120 score ≤2.** At threshold 4, one of 120 would be shown; at 5, none.
The reasons are the right ones — "sloping top tube, carbon", "wrong colour",
"flat bars" — which is exactly the set of distinctions CLIP cannot make.

### Recall — the bike itself, and the ways a thief changes it

Leave-one-out: the reference photo being scored is removed from the prefix, so
the model is never marking its own homework.

| probe | score |
|---|---|
| reference 01, leave-one-out | **9** |
| reference 02, leave-one-out | **8** |
| reference 03, leave-one-out | **7** |
| reference 01, hue rotated 90° (*repainted*) | **9** |
| reference 01, cropped to the bare frame, no kit (*stripped*) | **9** |
| reference 01 at 240 px (*marketplace thumbnail*) | **9** |
| reference 01 at 45% brightness (*dim garage*) | **9** |

The gate survives a repaint, a strip, a thumbnail and the dark. That is the
property that matters, because the archive analysis already
established that stripping is the economically correct disposal.

### Recall — other real <BRAND>s in the wild

Hardest possible negatives: actual <BRAND> listings pulled from leboncoin.fr and
Troc-Vélo on 2026-10-06. The gradient is correct and continuous.

| listing | score |
|---|---|
| steel, level top tube, drop bars, gumwalls, dark | **7** |
| black <BRAND>, level top tube, drop bars | 6 |
| navy <BRAND>, level top tube, **flat** bars | 6 |
| flat-bar <BRAND>s, black / tan / teal | 1–3 |
| pink and mint <BRAND>s, frame-only | 0–2 |

### The model choice is measured, not assumed

**Claude Haiku 4.5 is disqualified. Do not "optimise" back to it.** Same
prompt, same images, same day:

| | Sonnet 5 | Haiku 4.5 |
|---|---|---|
| reference 01 (loaded), leave-one-out | **9** | **2** |
| reference 02 (loaded), leave-one-out | **8** | **2** |
| reference 01 repainted | **9** | **2** |
| the 120 negatives | 119 ≤2, no false positive | one at 6 |
| spread across the wild <BRAND>s | 0–7, graded | flat 1–2 |
| **cost per image** | **$0.0014** | **$0.0017** |

Haiku scored the bike loaded with its own child seat and panniers a 2,
reporting the navy frame as "lighter/grey". It is also *dearer*: its minimum
cacheable prefix is larger than our reference block, so nothing cached and
every request paid full input price (`cache_read = 0` across the whole run).

## 4. Cost — and the correction

**The first number published for this was wrong.** It said ~$20/month, derived
from ~530 photos/day. 530 was the count *sent to Telegram*; the pass scores
everything *banked*, which is ~906/day. The real figure as first shipped was
**$31/month**, and at a $1.00/day cap it would have hit the budget on most
days and silently degraded to the CLIP ranking.

Measured volume, 16 full days to 2026-10-05:

| | median/day | max |
|---|---|---|
| listings banked | **906** | 1,179 |
| near ring (≤150 km) | 509 | |
| far ring (150–400 km) | 418 — **46% of the bill** | |

Per image, on `claude-sonnet-5` at 384 px, from the live ledger
(**$0.00113/photo**), the spend splits almost evenly three ways:

| component | tok/image | $/image | share |
|---|---|---|---|
| listing photo (uncached input) | 204 | 0.000409 | 36% |
| output tokens | 38 | 0.000382 | 34% |
| cached prefix (3 refs + system) | 1,663 | 0.000333 | 29% |

### What was tried and rejected

| lever | saving | why rejected |
|---|---|---|
| **Batch 12 photos per request** | −43% | **5× the false positives** (5/120 at threshold 4, vs 1/120). The model starts comparing listings to each other instead of to the references. |
| **Drop the `reason` field** (score-only) | −29% | **Costs recall.** The best near-miss probe — a dark steel level-top-tube drop-bar <BRAND> — fell from **7 to 3**. The reason field is doing real reasoning work; it is not decoration. |
| Narrow the price band | −19% | A thief prices unpredictably. Not worth the recall risk for 19%. |

### What shipped: the per-ring CLIP pre-gate (section 2a)

| configuration | photos/day | $/month |
|---|---|---|
| as first shipped, no pre-gate | 927 | **$31** |
| 3-arm pre-gate everywhere | 512 | $17 |
| **3-arm near + original gate far** ← shipped | **408** | **$14** |
| near ring only, far ring off | 284 | $10 |

`vlm_daily_budget_usd` is **0.80** — roughly 1.7× the expected $0.46/day, so a
busy day does not trip it. It remains a hard stop: past it the pass degrades
to the CLIP ranking and says so. `./vlm.py spend` prints the ledger.

**This bills the same Anthropic key the observer crons use**, so it appears in
the owner's top-ups. `vlm_enabled: false` in `bike.json` turns it off.

**The next 50% is the Batch API** — the digest is not latency-sensitive, and
batch submission halves the price with zero quality change. It is not built,
because it would cost the 20-minute instant push on a strong match.

## 5. Two prompt findings that each cost a run

- **Do not tell it to be strict.** A first prompt ended with *"Be strict. Most
  photos should be 0-3."* It collapsed 116 of 120 scores to exactly 0 —
  including all three reference photos — and still produced two false
  positives at 8. The band descriptions have to span the range and then stop.
- **Label every reference image.** With the references as a bare run of image
  blocks, 4 of 12 calls answered *"no bicycle image provided to evaluate"*: the
  model had folded the listing photo into the reference set. Numbered labels
  and an explicit `=== END OF REFERENCES ===` fixed it. A listing that silently
  scores "no image" is a listing that silently never reaches the owner.
- The blue-wall warning is load-bearing for the same reason it is in
  `VISION.md`: without it the model credits the background.

## 6. What this still cannot do

It cannot tell you a listing **is** the bike — it says a photo is plausibly a
navy steel level-top-tube drop-bar bike, and on an ordinary day in Spain that
is nobody. The recognition is still the owner's; the model's job is to make sure the
handful of photos he looks at are the handful worth looking at. If something
looks right it goes to the the police on the existing police report. **Do not message
the seller.**

The brand tripwire in `run_hits` is untouched and still fires on the name alone,
at any score, out to `hit_radius_km` 1200 — the owner asked explicitly to keep that.

## 7. Known gap: `--catchup` is still unfiltered

`./run.sh --catchup` deliberately re-sends every bike listed since the theft,
including ones already shown, and it does **not** apply the VLM cut. That is
its documented contract ("`seen.json` deliberately ignored") and silently
filtering it would break the one escape hatch that shows everything. But it
means a catchup run is still a 250-photo flood.

Scoring the whole 26,207-listing archive to fix this would cost ~$33 in one
go, which is why it was not done unasked. If it is ever wanted, the cheap
route is `./vlm.py score --file <a json dump of the archive>` overnight with
`vlm_daily_budget_usd` raised for one night; the cache is keyed by listing id,
so every later digest then reads it for free.
