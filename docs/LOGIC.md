# How it actually works

*The working doc, kept as it was written — dated, first-person, and occasionally superseded by the sections below it. `README.md` is the current summary; this is the audit trail.*

Snapshot taken 2026-09-06 14:05 Madrid, read off the running code. Nothing here is aspirational — every
claim below is in `wallapop_monitor.py` or the crontab.

---

## 1. The one-paragraph version

Every 20 minutes the bot hits Wallapop's unauthenticated search endpoint with
two different questions. **HITS** asks "is anyone selling a <BRAND> <MODEL>?"
and pushes anything that matches straight to Telegram with a photo. **SWEEP**
asks "what bikes have been listed near the theft site since the theft?", ignores
the brand entirely, and *banks* the answers into a file. Twice a day
(09:15 / 20:15) the bank is flushed as a photo album so you can recognise the
bike by sight. HITS is the channel you want; SWEEP is the channel that works,
because thieves strip the brand out of the title.

---

## 2. What is scheduled

```
*/20 * * * *   run.sh --send     # scan: push hits, bank sweep finds
15 9,20 * * *  run.sh --digest    # flush the bank as a photo album
```

`run.sh` sources `.env` for `TELEGRAM_TOKEN` /
`TELEGRAM_USER_ID`, then runs under `flock -n data/.lock` — a scan that
overruns 20 min is skipped, not stacked. The box has no swap; stacked runs are
how you OOM it. Log: `/tmp/bike-watch.log`.

---

## 3. The bike profile — `data/bike.json`

This is the only file you edit. Current contents:

| field | value | what it drives |
|---|---|---|
| `brand` / `model` | <BRAND> / <MODEL> | the **strong** tokens, +3 each |
| `extra_keywords` | <model>, yepp, portabebe, cuadro acero | **mid** tokens, +2 each, matched as whole phrases |
| `type` / `colour` | gravel / azul | **weak** tokens, +1 each |
| `paid_eur` | 1700 | *market estimate, not what the owner paid* — the "under half price" test |
| `stolen_on` | 2026-09-05 | listings before this date score **−3** and are dropped from the sweep entirely |
| `centre` | 41.3874, 2.1686 (the theft site) | all distance maths |
| `hit_radius_km` | **400** | ~national — a stolen bike travels |
| `sweep_radius_km` | **60** | greater Barcelona |
| `sweep_min/max_price` | 80 – 2500 | sweep price band |
| `sweep_max_items` | 30 | how many photos a digest shows |

Reference photos of the bike live in `data/reference-photos/` (3 shots:
drive side, rear child seat, cockpit + front rack).

**The rule that cost six false alerts:** do not put accessory brands
(`thule`, `vaude`) in `extra_keywords`. Those brands sell far more racks than
bikes, so every listing they match is a rack. `yepp` and `portabebe` survive
only because the head-noun filter (§6) stops them matching child seats sold
alone.

---

## 4. How it talks to Wallapop

`GET https://api.wallapop.com/api/v3/search` — the same endpoint the web app
calls. Unauthenticated, read-only. No account, no login, no messaging.
~14 requests per scan, ~13 s.

Four things that are not obvious and will bite whoever edits this next:

- **The CloudFront WAF 403s a bare request.** `Origin: https://es.wallapop.com`
  \+ `Referer` \+ `X-DeviceOS: 0` are what make it answer. Do not trim `HEADERS`.
- **`distance` and `category_ids` are silently ignored.** Passing
  `distance=20000` still returns Burgos and Palma. *All* geo filtering is done
  client-side by haversine off `location.latitude/longitude`, and category
  filtering by `category_id == 17000`. More reliable anyway.
- **`order_by=newest` is national and shallow.** Five pages of `bicicleta`
  reaches back roughly 40 minutes of listings, nationwide. That is the entire
  reason SWEEP banks instead of digesting — a twice-daily sweep would see about
  1% of the day.
- **`order_by=closest`** does respect lat/lon and returns local-only results,
  but ranks by distance regardless of age. Used only by `--backfill`.
- Responses are CDN-cached per full URL, hence the `_ts` cache-buster param.

### The coverage-gap alarm

`sweep_scan()` records how far back the feed reached (`data/state.json →
last_window_min`) and compares it against the previous scan. If the feed no
longer reaches back to the last run it logs

```
WARNING: coverage gap of N min — the feed no longer reaches back to the
previous scan. Raise MAX_PAGES or shorten the cron interval.
```

This matters because the failure is **silent**: listings just quietly stop
being seen and nothing looks wrong. Right now the window is 44 min against a
20 min cron — comfortable. One WARNING has fired since launch (during the
first hours, before the interval was tuned). **If you lengthen the cron past
~35 min, the bot goes half-blind and will not tell you unless you read the log.**

---

## 5. Scoring — the HITS channel

A listing is pushed only if **all three** hold:

1. **`matched`** — at least one brand/model token or `extra_keywords` phrase hit;
2. **score ≥ 6**;
3. **`is_whole_bike()`** says it is a bike, not a part.

Condition 1 is the load-bearing one. Context alone (near + cheap + listed after
the theft) is worth up to 5 points, and Wallapop's search is fuzzy enough that
a query for "<BRAND>" returns Rolexes. **Context breaks ties; it must never be
the whole case.** Real matches score 8–12.

| signal | points |
|---|---|
| each brand/model token (word-boundary, title+description) | **+3** |
| each `extra_keywords` phrase, matched whole | **+2** |
| each colour/type/year token, or `talla <size>` | +1 |
| within 15 km of the theft site | +2 |
| within 40 km | +1 |
| priced under 45% of `paid_eur` | +2 |
| listed on/after `stolen_on` | +1 |
| listed **before** `stolen_on` | **−3** |
| stolen-goods phrasing ("sin factura", "urge", "efectivo", "no se la marca"…) | +1 |

Two matching details that were bugs once and are now load-bearing:

- **Word-boundary matching, not substring.** `gain` was matching `bargain`.
- **`extra_keywords` are whole phrases.** Splitting them turned
  `"silla infantil"` into `silla` + `infantil`, which matched every child seat
  on the site.
- A one-letter `size` is expanded to `talla m` — a bare `m` matches everything.

The HITS channel runs 7 queries (`<BRAND> <MODEL>`, `<BRAND>`, `bicicleta <BRAND>`,
`<model>`, `yepp`, `portabebe`, `cuadro acero`) at 2 pages each. Two, not
five: a brand query exhausts in one page and the keyword queries are junk past
page 2.

---

## 6. The parts filter — `is_whole_bike()`

Category 17000 ("Bicicletas") includes wheels, helmets, racks and frames.
The filter is **head-noun based**: a parts word in the *first three words of
the title* means the listing is about the part. So "Silla portabebé para
bicicleta" is dropped despite containing "bicicleta", and "Cuadro de bicicleta
<BRAND>" survives.

`cuadro` (frame) is deliberately **absent** from the parts list — a stripped
frame is exactly what a thief sells.

The accessory head-nouns (`portabicicletas`, `porta`, `transportin`, `cesta`,
`nino`, `bebe`, `soporte`…) were added after "Thule Caravan portabicicletas"
and "Porta niños bicicleta Thule" both got through.

---

## 7. The SWEEP channel

Queries: `bicicleta` (5 pages), `bici` (5), `mountain bike` (2),
`bicicleta electrica` (2) — ~310 raw listings per scan. Then filtered
client-side to: unseen · is a bike · is a *whole* bike · within
`sweep_radius_km` · price in band · **listed on or after the theft date**.
Survivors are scored (for ranking only, no threshold) and appended to
`data/pending.json`.

The digest sorts the bank by score then recency, takes the top
`sweep_max_items` (30), sends them as `sendMediaGroup` albums of 10 with
price + title + link captions, then a text block of the same items.

---

## 8. Files and what they mean

```
data/bike.json          the profile — the only file you edit
data/pending.json       sweep finds banked for the next digest   (147 right now)
data/seen.json          listing ids already reported, capped 30k (421 right now)
data/hits.jsonl         append-only log of every scored hit + reasons (7 rows)
data/state.json         last scan time + measured feed window
data/reference-photos/  3 photos of the actual bike
```

`--report` prints bank size, last scan, feed window and every scored hit.

---

## 10. What this bot cannot do

It reads public listings and matches **text**. It does not look at photos, so a
listing with no brand in the words is only ever caught by SWEEP, by your eye.
It cannot see Wallapop chats, and it will not contact a seller. If something
looks like the bike, that is a the police matter — add it to the existing police report,
do not arrange a meet.

## Milanuncios channel (added 2026-09-06)

`milanuncios.py` + `run-milanuncios.sh` run the same two channels (HITS,
SWEEP-then-digest) against Milanuncios, the other big Spanish board. Same
`data/bike.json` profile, same chat, separate state (`data/mil-*`) and lock.

Source: `searchapi.gw.milanuncios.com/v1/listing` — the SPA's own backend,
plain unauthenticated GET. The public HTML site is behind an Imperva bot wall
("Pardon Our Interruption") that a real headless Chromium does not get past,
so do not go back to scraping pages.

Three things that will bite whoever edits this:
* pagination is `offset`, NOT the `nextToken` the response advertises. Feeding
  nextToken back silently returns page 1 forever.
* there are no working price/category query params — both filters are applied
  client-side on the returned ads.
* image urls arrive scheme-less. `images-re...` serves the bare url; the
  `ma-ad-media-pro` service 404s "Invalid Rule" unless you append a
  whitelisted `?rule=` — `hw396_70` is the only one found to work.

Volume is ~10-20 new bikes a day inside 150 km, an order of magnitude below
the Wallapop sweep, so the cron is generous (3 pages/query, every 20 min).

Cron: scan at :05/:25/:45, digest at :20 on the 2h grid (5 min after Wallapop's).


---

## Widened search + the photo channel (2026-09-07)

A bike shop told the owner to widen the search. Three changes went in together,
because widening without a filter just means more photos you scroll past.

### 1. The brand tripwire — precise <BRAND> model search

`bike.json` gained `brand_aliases` (surley, sourly, surlly, <brand> bikes) and
`model_aliases` (<model>, stragler, cross check, crosscheck, cross-check,
long haul trucker, disc trucker, midnight special, bridge club). They score as
**strong (+3)**, they are added to the HITS query set, and in `run_hits` a
match on any of them is **pushed regardless of score** — the tripwire.

Why this is safe: measured 2026-09-07, the whole of Wallapop Spain held
**two** <BRAND>-titled bikes (a Pugsley and a Bridge Club). The brand is rare
enough nationally that waiting for a listing to earn 6 context points would
only ever lose you a real one — a <BRAND> listed in Sevilla at full price scores
3 and would have been silently dropped.

Two guards, both of which cost a false alert before they existed:
- The tripwire does **not** fire on a listing dated before the theft.
  `score_item` docks 3 points for that and the tripwire must not step over it.
- Milanuncios' `run_hits` now applies the bike-category gate. Its search is
  fuzzier than Wallapop's and "midnight special" was returning Jimmy Smith CDs.

`hit_radius_km` is now 1200 km — effectively "anywhere". For a brand this rare
distance is not evidence of anything.

### 2. Two rings in the sweep

| field | value | meaning |
|---|---|---|
| `sweep_near_km` | 150 | inner ring — unchanged behaviour, everything is shown |
| `sweep_radius_km` | 400 | outer ring — only what clears the photo filter |
| `sweep_far_max` | 12 | how many far listings a single digest may show |

The outer ring is **free in API terms**: `order_by=newest` was always national,
so those listings were already being fetched and thrown away by the radius
filter. Each banked row now carries `_ring` ("near"/"far") and `_dist_km`.

`genre_queries` in the profile add to `SWEEP_QUERIES` — bicicleta gravel,
bicicleta carretera, gravel, bicicleta acero, cicloturismo, bikepacking. The
shallow national newest-first feed can drop a steel drop-bar bike listed as
"gravel" off the back of "bicicleta" between two scans; these reach the genre
directly and each exhausts in a page or two.

Milanuncios still sweeps at `sweep_near_km` only — its digest has no far-ring
gate yet, so widening it there would just be more photos.

### 3. The photo filter — `visual.py`

Full write-up with the measurements in **VISION.md**. Read it before touching
the scorer; in particular, image match in the sense of "find my bike" was
tried and **does not work** (CLIP ranks random Decathlon MTBs above the same
bike shot from another angle), and the thing that shipped instead is a
drop-bar/navy **attribute** filter.

Digest flow now: `run_visual()` shells out to `visual.py score` (own venv,
`nice -n 10`, best effort), which writes `_visual` (0-100) and `_vpass` back
into `pending.json`; the far ring is cut to the best `sweep_far_max` and the
rest archived with `_held`; everything left is sorted by `_visual` first and
`_score` second. Captions carry `· photo NN · NNN km`.

### 4. The recognition filter — `vlm.py` (2026-10-06)

Full write-up with the measurements in **RECOGNITION.md**. Read it before touching any
`vlm_*` key. The short version: the near ring was never gated, so ~530 photos
a day went to Telegram and **70% of them were not even drop-bar bikes** — which
is why the owner was about to stop reading the digest. Worse, CLIP's whole top rank
(`_visual` 74-77, all PASS) is modern sloping-top-tube carbon race bikes, so
the photos leading every digest were the ones least likely to be the bike.

`vlm.py` asks Claude the question CLIP cannot answer — *is this plausibly MY
bike* — against the three reference photos, and returns 0-10 plus a one-line
reason. Measured: **119 of 120 archived listings score ≤2**, while the
reference photos score 7-9 even repainted, stripped of every accessory,
darkened, or shrunk to a 240 px thumbnail.

Digest flow: `run_vlm()` shells out to `vlm.py score` (agent venv, needs
`ANTHROPIC_API_KEY`, which `run.sh` picks up from `../.env`); everything below
`vlm_show_threshold` (4) is archived with `_held` and never shown; what is left
is sorted by `_vlm` first, `_visual` second. **Scoring happens at scan time**,
so a listing at or above `vlm_hit_threshold` (7) is pushed within 20 minutes
instead of waiting for the next digest. Captions carry
`· match N/10 · photo NN` plus the model's one-line reason, so the cut stays
calibratable from the digest itself.

Three invariants, all of them learned the hard way:

- A scoring **error is -1 and is SHOWN**, never hidden. "The model fell over"
  must not read as "not your bike".
- The far-ring cap now runs **after** the VLM cut and ranks by `_vlm`. It used
  to run first and rank by CLIP, which let the weaker filter throw away
  listings the stronger one had already judged — 20 of them on the first live
  run.
- An empty digest **still sends a line** ("nothing worth your eyes, N ruled
  out"). Silence is indistinguishable from a dead cron, and this monitor has
  already lost 107 listings to exactly that ambiguity.

A free **per-ring CLIP pre-gate** runs in front of the paid call (which is why
`run_visual()` now runs at scan time too — the pre-gate reads `_vattrs`). Near
ring: `dropbar>=0.10 | navy>=0.05 | kid>=0.40`, 55.7% pass, 2.3x worst-case
margin over seven recall probes. Far ring: the ORIGINAL `visual.py` gate it has
had since 2026-09-07, 29.7% pass — it does not cover the repaint case, which is
an accepted trade because the far ring is capped at 12 per digest anyway.
**A listing whose title names the brand is never pre-gated and never cut**, at
any score, and a listing with no `_vattrs` fails OPEN.

Cost: **$0.00113/photo measured**, ~**$14/month** after the pre-gate (it was
$31 before it — the first published $20 was wrong, computed from the ~530
photos *sent* rather than the ~906 *banked*). Hard-capped by
`vlm_daily_budget_usd` 0.80; past the cap it degrades to the CLIP ranking and
says so. It bills the same Anthropic key as the observer crons.
`vlm_enabled: false` turns it off. Batching and dropping the `reason` field
were both measured and rejected — see RECOGNITION.md §4. The brand tripwire in
`run_hits` is untouched: it still fires on the name alone, at any score, out to
1200 km.

**Cron unchanged.** The digest got ~35 s slower (the CLIP pass), which is
nothing against a 2 h grid, and the 20-min scan does no vision work at all.

---

## Channel 3 — ACCESSORIES (`accessories.py`, added 2026-09-20)

The first two channels watch **whole bikes**. `is_whole_bike()` rejects any
listing with a parts word in its first three words and `brand_token_set()`
keeps `yepp`/`portabebe` out of the tripwire — both correct for HITS (accessory
brands flooded the bike search with racks), both blind to the likeliest
disposal. Measured against live Barcelona prices on 2026-09-20, this bike is
worth **€430–800 in pieces against ~€150 as a quick whole-bike cash sale**, so
stripping is the economically rational move and every piece is invisible to a
"blue <BRAND>" search.

**The unit of interest is the SELLER, not the item.** A Yepp seat in Barcelona
is a parent. The same account also listing a rack and a pannier inside a
fortnight is a bike that came apart.

Two sub-channels, same noise-tolerance split as the bike monitor:

- **KIT** — `kit` in `data/accessories.json`, seven categories
  (seat/rack/bag/fender/wheel/post/drive), `order_by=closest` around the theft
  site, `radius_km` 40, listings dated after `theft_date` only, price ≤
  `max_price`. Measured backlog on the first pass: **169 listings over 15 days
  (~11/day)** — a digest, not an alert.
- **LOCKS** — `locks_national`, a term → needs-bike-context map. The wheels and
  seatpost are on coded locking skewers (Pinhead/Pitlock class), so `pitlock` /
  `pinhead` / `hexlox` are the bike's fingerprint. On 2026-09-20 all of
  Wallapop Spain held **7 / 3 / 2** listings for those words, so this runs
  nationally and pushes on sight, like the <BRAND> tripwire.

### Things that cost a test pass to learn

- **`category_id` cannot arbitrate parts from bikes.** Wallapop files a Yepp
  seat, a rear derailleur and a complete bike alike under `17000`. The head
  noun is the only discriminator, and it has to be **strict** — a parts word in
  the first three words, no fallback. Without that, "cambio trasero shimano"
  drags in every bike whose groupset is named in the title ("Triban RC 500 Disc
  Shimano Sora XL", "Cannondale Flash Carbono").
- **`ACC_HEAD_WORDS` ≠ the sweep's `PARTS_WORDS`.** `has_token()` is a
  whole-word match, so the sweep's `alforjas` does not match `alforja`.
- **`pinhead` is also a Hellraiser figure** — the first test pass pushed one.
  Ambiguous lock terms need `GENERIC_BIKE_WORDS` in the title.
- **The GRINDER flag has to be narrow.** `locked_categories` (wheel, post) fire
  on tyres, inner tubes and seat clamps, which were never locked; `LOCKED_HEADS`
  minus `NOT_LOCKED` restricts it to an actual wheel or a saddle/seatpost.
  Before that, every vintage-parts seller in Sant Martí was flagged.
- **Descriptions are out of reach.** `SUSPICIOUS` already carries
  "no tengo la llave", but the search endpoint returns titles only;
  `api/v3/items/{id}` is 404 and `es.wallapop.com/item/{slug}` 403s behind the
  WAF. Catching a seller who admits the key is missing would need the Chromium
  path per listing — not worth the fragility.

### Ranking

Clusters sort by `signature_categories` (seat/rack/bag/fender — the kit that
came off **this** bike with an allen key) first, then raw category count, then
the grinder flag. Wheels and the seatpost are behind coded skewers, so their
appearance means somebody cut past the lock: a workshop, not an opportunist.

`--seed` marks everything currently listed as seen without banking it, for when
the backlog is not wanted.

### The cron trap that ate the first digest

The scan runs hourly at `:35` and the digest was first cron'd at `09:35`. Both
go through the same `flock` file, and `flock -n` **exits 1 silently** — so on
21 Sept the scan won the lock and the first digest never sent, leaving 174
listings banked with nothing in the log to say why. The digest now runs at
`09:40`, and `run-accessories.sh` uses `flock -w 300` with an explicit failure
line on stderr, so a future overlap costs a delay instead of a missed send.
**Never cron two modes of the same flock'd runner on the same minute.**
