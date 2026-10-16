# AGENTS.md — setting this up for someone whose thing was stolen

You are probably reading this because somebody pasted this repo at you and
said "set this up for me, they took my bike." This file is the whole job. It
is written for an AI agent; a human can read it too.

**Start here: the person you are helping is upset and in a hurry.** Get a
working sweep running on default settings first, then calibrate. A digest that
is slightly too noisy today beats a perfect one on Friday.

---

## 0. The three rules you do not get to relax

1. **Never contact a seller.** Not a message, not a question, not a "is this
   still available". This repo has no code that can — keep it that way. A
   match goes to the police report, and nowhere else. If the user asks you to
   draft a message to a seller, say no and explain why: it tips off the
   thief, it destroys the only evidence, and people get hurt at meet-ups.
2. **Nothing identifying goes in git.** `data/` is gitignored for a reason: it
   holds their address-adjacent coordinates, their photos, and every listing
   the sweep has seen. Never commit it, never paste its contents into an issue.
3. **Do not guess a threshold.** Every number in `profiles/bicycle.json` is a
   measurement, not a preference. For any other object they are *placeholders*
   and must be derived with `./calibrate.py arms`. A guessed threshold fails
   silently — the user never sees the listing it cut.

## 1. What this does, in four lines

Pull every local listing of the right class → filter out parts and accessories
→ score each photo on a handful of cheap local attributes (CLIP + a colour
band) → send only what survives to a vision model, which scores it 0–10
against the owner's reference photos → push the handful that score high.

The thing it is NOT: image-match retrieval. That was tried and measured
(`docs/VISION.md`) and it does not work off a marketplace thumbnail.

## 2. The setup interview

Ask these, in this order. Do not proceed on assumptions — a wrong centre point
silently empties the sweep.

| ask | goes to | notes |
|---|---|---|
| What was taken? | `object` in `data/bike.json` | pick a pack, §3 |
| Make and model, and how people misspell it | `brand`, `model`, `brand_aliases`, `model_aliases` | empty is fine — then only the photo sweep runs |
| Where was it taken, as precisely as they will say | `centre.lat/lon/label` | everything geo is measured from here |
| When | `stolen_on` | listings older than this are scored down hard |
| What did they pay | `paid_eur` | drives the "suspiciously cheap" signal |
| **Three photos** | `data/reference-photos/` | §4 — this is the one that decides whether any of it works |
| What is bolted to it that others do not have | `description`, and the accessory arm | a child seat, a top box, a sticker, a scratch |
| Where do they want the alerts | `.env` | `NOTIFY_BACKEND=stdout` works with no accounts at all |

Then write `data/bike.json` from `data/bike.example.json` and run
`./run.sh --test`. It prints, sends nothing, writes no state. If the listings
coming back are not the right kind of object, fix the pack before anything else.

## 3. Choosing or building the object pack

`profiles/<object>.json` holds everything that is about *the kind of thing*
rather than about *finding a stolen thing*: the marketplace category, the
search terms, the words that mean "a part of one", the attribute arms, and the
vision-model prompt. Shipped:

- **`bicycle`** — the real one. Every threshold measured over six weeks.
- **`motorbike`** — right shape, right category (Wallapop 14000, verified
  against the live search), **placeholder thresholds**.
- **`generic`** — a cello, a camera, a pram. One colour arm and nothing else.

To add an object class, copy the closest pack and change five things:

1. `category_ids` — find them empirically, do not guess:
   ```python
   import wallapop_monitor as W, collections
   collections.Counter(i["category_id"] for i in W.fetch("violonchelo", pages=1))
   ```
2. `sweep_queries` — `[term, pages]`. Broad terms need depth, narrow ones
   don't. In the local language of the marketplace, not English.
3. `words.head` / `words.parts` — `parts` is how "child seat FOR a bicycle"
   stops being read as a bicycle. The head-noun rule only looks at the first
   three words of a title. Leave out the word for the *stripped core* of the
   object (for bicycles, `cuadro`/frame) — that is exactly what a thief sells.
4. `arms` — **the part that matters.** Two kinds:
   - `"type": "clip"` — contrastive prompts, `positives` vs `negatives`. The
     negatives are load-bearing: the score is softmax mass over both, so a
     bare cosine to one prompt is not comparable between images. Write
     negatives that are the *nearest wrong thing*, not random objects.
   - `"type": "colour"` — a hue/saturation/value box measured straight off the
     pixels. CLIP answers colour questions with noise; this does not.
     `./calibrate.py colour` reads the band off their photos.
5. `prompt` — intro, label line, the 0–10 bands, closing. **The bands must
   span the range.** A version that ended "Be strict. Most photos should be
   0-3." collapsed 116 of 120 scores to exactly zero, including all three
   reference photos, and *still* produced two false positives at 8.

Aim for **three arms covering three different failure modes**. On the bicycle
pack: a repainted bike survives on the child-seat arm alone; a stripped one on
the navy/drop-bar arms alone. One arm is not a gate, it is a coin flip.

Arms listed in `gate_arms` decide what gets paid for. Everything else still
scores and still ranks the digest — `steel` on the bicycle pack is a
tie-breaker, far too noisy to gate on.

## 4. The reference photos

Three is enough. Ask for: **the whole thing from the side, the back or the
distinctive end, and one close detail** (a cockpit, a dashboard, a scratch).

- Shot against a **neutral background**. A navy bike against a blue wall
  teaches the colour arm the wall — it reads 0.18 on the wall and 0.001 on the
  same bike repainted. If their photos have a coloured background, say so in
  `description`, because the vision model will credit it otherwise.
- Phone photos are fine. The pass survives 240 px thumbnails.
- If they only have one photo, use it. One is enough to start.

## 5. The calibration loop — do this, then trust the digest

```bash
./calibrate.py probes     # builds 4 degradations, prints every arm's score
./calibrate.py colour     # reads the colour band off their photos
./calibrate.py arms       # proposes thresholds; --write applies them
```

`arms` scores the probes, scores a sample of real listings as the background,
and searches for the thresholds that keep **every probe alive with 2× margin**
while paying for as few listings as possible. It then prints which arm is the
only thing keeping each probe alive. Read that table out loud to the user —
it is the honest statement of what the system would miss.

It needs a background sample, which means **run one sweep first**
(`./run.sh --send`, then `./visual.py score`). On a fresh install there is
nothing to measure against and it will say so.

Re-run `arms` after any change to an arm. Never edit a `vlm_pregate_*` value
by hand.

## 6. Things that were measured and lost. Do not re-propose them.

| idea | what it saves | what it costs |
|---|---|---|
| Batch 12 photos per API call | −43% | **5× the false positives.** The model starts comparing listings to each other instead of to the references. |
| Drop the `reason` field, score only | −29% | **Recall.** The best near-miss probe fell from 7 to 3. The field is doing the reasoning, not decorating it. |
| A cheaper model | — | Scored the owner's own bike, loaded with its own child seat, **2/10** — and cost *more*, because the reference prefix is under its minimum cacheable size so nothing cached. |
| Collapse the three arms into one | simpler | Silently destroys recall for one specific disposal strategy. §3. |
| Hand the chat API an image URL | less code | Telegram's fetcher makes photo records its CDN then refuses to serve to **desktop** — stalls at 0% forever while mobile looks fine. And one refused image silently drops a whole batch of ten. Download the bytes and upload them. |
| Stretch the cron past ~35 min | fewer runs | The newest-first feed is national and shallow; listings vanish between runs with nothing looking wrong. `sweep_scan()` logs `WARNING: coverage gap of N min` when it starts. |

If the user asks for one of these anyway, tell them the number, then do what
they decided. It is their search.

## 7. Verifying you actually finished

```bash
python3 tests/test_pack_parity.py   # the bicycle pack still matches the docs
./run.sh --test                     # real listings, right object class, no sends
./calibrate.py prompt               # read it. Does it describe THEIR object?
./vlm.py refs                       # their own photos should score 7+
```

`./vlm.py refs` is the one that matters: it scores each reference photo with
that photo removed from the prompt, so the model is not marking its own
homework. **If their photos score below 6, stop and fix the description** —
the digest is worthless until this passes. Usual cause: a `description` that
describes a category instead of an object. "A blue bike" is a category. "A
level top tube, which almost no modern bike has, and tan sidewalls" is an
object.

## 8. Cost, so you can tell them the truth

~$0.0011 per photo scored, ~$14/month at ~900 listings a day with the pre-gate
on, ~$31 without it. `vlm_daily_budget_usd` is a hard stop — past it the pass
degrades to the free CLIP ranking and says so in the log. Set it to something
they are comfortable losing. `vlm_enabled: false` turns the paid pass off
entirely and the thing still runs.
