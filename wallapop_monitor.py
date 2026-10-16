#!/usr/bin/env python3
"""
wallapop-monitor — watch Wallapop for a stolen bike.

Two channels, deliberately different in noise tolerance:

  HITS   listings whose text matches the bike's brand/model tokens. Rare, so
         they are pushed the moment they appear (every 30 min).
  SWEEP  every bike listed near the theft site inside the price band, brand or
         no brand. Thieves usually strip the brand out of the title, so this is
         the channel that actually catches them — but it is 20-80 listings a
         day, which is a digest, not an alert.

Wallapop has no public API. This uses the same endpoint the web app calls
(api.wallapop.com/api/v3/search). It is unauthenticated and read-only: no
account, no messaging, no scraping of anything behind a login.

Usage:
  ./run.sh --test               one pass, print only, no Telegram, no state write
  ./run.sh --send               hits pass (cron, every 30 min)
  ./run.sh --send --digest      hits + sweep digest (cron, daily)
  ./run.sh --seed               mark everything currently listed as seen
  ./run.sh --report             what has been flagged so far
"""

import argparse
import json
import math
import os
import random
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone, timedelta

import requests

import objectpack

# Optional: drop a tg_routes.py next to this file to route pushes into a
# Telegram forum topic instead of the DM. Absent, this is a no-op.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import tg_routes
except ImportError:
    tg_routes = None


def _route(payload):
    return tg_routes.apply(payload, "bike") if tg_routes else payload


HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
BIKE_FILE = os.path.join(DATA, "bike.json")
SEEN_FILE = os.path.join(DATA, "seen.json")
PENDING_FILE = os.path.join(DATA, "pending.json")
STATE_FILE = os.path.join(DATA, "state.json")
HITS_FILE = os.path.join(DATA, "hits.jsonl")

API = "https://api.wallapop.com/api/v3/search"
TIMEOUT = 25
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0.0.0 Safari/537.36")
# The CloudFront WAF in front of api.wallapop.com 403s a bare curl. Origin +
# Referer + X-DeviceOS are what make it answer; do not trim these.
HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Origin": "https://es.wallapop.com",
    "Referer": "https://es.wallapop.com/",
    "X-DeviceOS": "0",
}
# The marketplace category for whole objects of this class, the words that
# name one, and the words that name a PART of one, all come from the object
# pack -- profiles/<object>.json, picked by "object" in data/bike.json.
# profiles/bicycle.json holds exactly what this ran on for its first six
# weeks; profiles/motorbike.json is the same shape for category 14000.
PACK = objectpack.load(objectpack.read_json(
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "data", "bike.json")))
BIKE_CATEGORY_IDS = objectpack.category_ids(PACK) or {17000}
MAX_PAGES = 5                        # 40 items/page; 5 pages of "bicicleta"
                                     # spans ~35 min of national listings
LOG = "[wallapop-monitor]"


# ---------------------------------------------------------------- utilities

def log(msg):
    print(f"{LOG} {msg}", flush=True)


def norm(s):
    """lowercase, strip accents — 'Bicicleta Eléctrica' -> 'bicicleta electrica'"""
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ------------------------------------------------------------------ seen ids
# seen.json is the only thing standing between a still-listed item and a
# re-notification on the next 20-minute pass. It used to be a flat list
# pruned with `sorted(seen)[-30000:]` — which evicts by ALPHABET, not by age.
# Wallapop ids are random base-36-ish strings, so every id starting with a low
# character ("0j285mr4xdjy") was dropped on every single save and re-announced
# forever, while ids starting with "x" were kept for good. Now the file is a
# map id -> first-seen date and pruning is by age.
SEEN_TTL_DAYS = 120                  # ~850 new ids/day -> ~100k entries
SEEN_MAX = 150000                    # backstop, newest-first


def load_seen():
    """-> (set of ids, {id: 'YYYY-MM-DD'}). Accepts the legacy list format."""
    raw = load_json(SEEN_FILE, {})
    today = datetime.now(timezone.utc).date().isoformat()
    if isinstance(raw, list):
        # No dates on disk for the legacy entries. Stamp them today rather
        # than at the epoch: guessing old would prune them all at once and
        # re-announce thousands of listings.
        raw = {i: today for i in raw if isinstance(i, str)}
    elif not isinstance(raw, dict):
        raw = {}
    return set(raw), raw


def save_seen(seen, stamps):
    """Stamp anything new with today, drop anything past the TTL, write."""
    today = datetime.now(timezone.utc).date().isoformat()
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=SEEN_TTL_DAYS)).date().isoformat()
    for i in seen:
        stamps.setdefault(i, today)
    kept = {i: t for i, t in stamps.items() if i in seen and t >= cutoff}
    if len(kept) > SEEN_MAX:
        newest = sorted(kept.items(), key=lambda kv: kv[1])[-SEEN_MAX:]
        kept = dict(newest)
    save_json(SEEN_FILE, kept)
    return kept


# ---------------------------------------------------------------- bike profile

DEFAULT_PROFILE = {
    "_comment": "Edit with ./set-bike.sh or by hand. Everything except centre "
                "is optional; the more you fill in, the fewer false alerts.",
    "brand": "",
    "model": "",
    "type": "",
    "colour": "",
    "size": "",
    "year": "",
    "extra_keywords": [],
    "paid_eur": 0,
    "stolen_on": "2026-09-05",
    "centre": {"lat": 41.3874, "lon": 2.1686, "label": "city centre"},
    "hit_radius_km": 150,
    "sweep_radius_km": 25,
    "sweep_min_price": 80,
    "sweep_max_price": 2500,
    "sweep_max_items": 25,
}


def load_profile():
    p = dict(DEFAULT_PROFILE)
    p.update(load_json(BIKE_FILE, {}))
    return p


def bike_tokens(p):
    """Three tiers: brand/model (strong), distinguishing phrases (mid),
    colour/type/size (weak).

    extra_keywords are matched as WHOLE PHRASES. Splitting them was a bug:
    "silla infantil" became "silla" + "infantil" and then matched every child
    seat on Wallapop.
    """
    strong, mid, weak = [], [], []
    for field in ("brand", "model"):
        for t in norm(p.get(field, "")).split():
            if len(t) > 2:
                strong.append(t)
    # Aliases are strong too. A reseller who does not know the model puts the
    # wrong <BRAND> on it ("cross check", "disc trucker") or spells the brand the
    # way it sounds ("surley"), and either is still a <BRAND>. Matched as whole
    # phrases, same as extra_keywords — "cross check" must not become "cross".
    for kw in list(p.get("brand_aliases", [])) + list(p.get("model_aliases", [])):
        kw = norm(kw).strip()
        if len(kw) > 2 and kw not in strong:
            strong.append(kw)
    for kw in p.get("extra_keywords", []):
        kw = norm(kw).strip()
        if len(kw) > 2 and kw not in strong:
            mid.append(kw)
    for field in ("type", "colour", "year"):
        for t in norm(str(p.get(field, ""))).split():
            if len(t) > 2:
                weak.append(t)
    size = norm(str(p.get("size", ""))).strip()
    if size:
        # a bare "m" matches every listing on the site — only "talla m" counts
        weak.append(size if len(size) > 2 else f"talla {size}")
    return sorted(set(strong)), sorted(set(mid)), sorted(set(weak))


def build_queries(p):
    """Search strings for the HITS channel."""
    brand, model = p.get("brand", "").strip(), p.get("model", "").strip()
    typ = p.get("type", "").strip()
    qs = []
    if brand and model:
        qs.append(f"{brand} {model}")
    if brand:
        qs.append(brand)
        # "<local noun> <brand>" — "bicicleta <BRAND>", "moto Honda". The word
        # comes from the object pack, in the marketplace's language.
        qs.append(f"{objectpack.noun(PACK, 'local')} {brand}")
    if model and not brand:
        qs.append(model)
    # The whole of Wallapop Spain held exactly TWO <BRAND>-titled bikes when this
    # was measured (2026-09-07: a Pugsley and a Bridge Club), so asking for
    # every model name and every misspelling costs almost nothing in noise and
    # is the cheapest way to widen the net.
    for kw in list(p.get("brand_aliases", [])) + list(p.get("model_aliases", [])):
        qs.append(kw)
    for kw in p.get("extra_keywords", []):
        qs.append(kw)
    if not qs and typ:
        qs.append(f"bicicleta {typ}")
    out, seen = [], set()
    for q in qs:
        k = norm(q)
        if k and k not in seen:
            seen.add(k)
            out.append(q)
    return out


# ---------------------------------------------------------------- api

def search(keywords=None, next_page=None, lat=41.3874, lon=2.1686, order="newest"):
    if next_page:
        url = f"{API}?next_page={next_page}"
    else:
        params = {
            "keywords": keywords, "latitude": lat, "longitude": lon,
            "order_by": order, "source": "search_box",
            # cache-buster: CloudFront caches per full URL and we want fresh
            "_ts": int(time.time()),
        }
        url = API + "?" + "&".join(
            f"{k}={requests.utils.quote(str(v))}" for k, v in params.items())
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    if r.status_code == 403:
        raise RuntimeError("403 from Wallapop WAF — headers or IP blocked")
    r.raise_for_status()
    d = r.json()
    try:
        items = d["data"]["section"]["payload"]["items"]
    except (KeyError, TypeError):
        items = []
    return items, (d.get("meta") or {}).get("next_page")


def fetch(keywords, pages=MAX_PAGES, lat=41.3874, lon=2.1686, order="newest"):
    out, np_, page = [], None, 0
    while page < pages:
        try:
            items, np_ = search(keywords=keywords if page == 0 else None,
                                next_page=np_, lat=lat, lon=lon, order=order)
        except Exception as e:
            log(f"search failed ({keywords!r} p{page}): {e}")
            break
        out.extend(items)
        page += 1
        if not np_ or not items:
            break
        time.sleep(0.6 + random.random() * 0.6)
    return out


# ---------------------------------------------------------------- scoring

GENERIC_BIKE_WORDS = objectpack.words(PACK, "generic")
# Phrasing that recurs in stolen-goods listings: no papers, cash, urgent, no
# questions. Individually meaningless, jointly a tell.
SUSPICIOUS = ("sin factura", "no factura", "sin papeles", "urge", "urgente",
              "vendo rapido", "vendo rápido", "efectivo", "solo hoy",
              "no preguntas", "regalada", "por mudanza", "no tengo la llave",
              "sin llave", "no se la marca", "no conozco la marca")


def item_geo(it, prof):
    loc = it.get("location") or {}
    lat, lon = loc.get("latitude"), loc.get("longitude")
    if lat is None or lon is None:
        return None, loc
    c = prof["centre"]
    return haversine_km(c["lat"], c["lon"], lat, lon), loc


def has_token(text, token):
    """Word-boundary match. Substring matching made 'gain' hit 'bargain'."""
    return re.search(r"(?<![a-z0-9])" + re.escape(token) + r"(?![a-z0-9])", text) is not None


def score_item(it, prof, strong, mid, weak):
    """Returns (score, reasons, matched).

    `matched` is True only if a brand/model token or a distinguishing phrase
    hit. Context alone (near, cheap, listed after the theft) is worth up to 5
    points, so without this a Rolex returned by Wallapop's fuzzy search for
    "<BRAND>" would clear any sane threshold. Context breaks ties; it must never
    be the whole case.
    """
    text = norm(it.get("title", "") + " " + (it.get("description") or ""))
    reasons, score = [], 0

    hit_strong = [t for t in strong if has_token(text, t)]
    if hit_strong:
        score += 3 * len(hit_strong)
        reasons.append("matches " + "/".join(hit_strong))

    hit_mid = [t for t in mid if has_token(text, t)]
    if hit_mid:
        score += 2 * len(hit_mid)
        reasons.append("has " + "/".join(hit_mid))

    hit_weak = [t for t in weak if has_token(text, t)]
    if hit_weak:
        score += len(hit_weak)
        reasons.append("also " + "/".join(hit_weak))

    dist, loc = item_geo(it, prof)
    if dist is not None and dist <= 15:
        score += 2
        reasons.append(f"{dist:.0f} km from the theft site")
    elif dist is not None and dist <= 40:
        score += 1
        reasons.append(f"{dist:.0f} km away")

    price = (it.get("price") or {}).get("amount") or 0
    paid = prof.get("paid_eur") or 0
    if paid and price and price < paid * 0.45:
        score += 2
        reasons.append(f"€{price:.0f} vs €{paid:.0f} paid — under half")

    created = it.get("created_at")
    stolen = prof.get("stolen_on")
    if created and stolen:
        try:
            t = datetime.fromtimestamp(created / 1000, timezone.utc)
            s = datetime.strptime(stolen, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if t >= s:
                score += 1
                reasons.append(f"listed {(t - s).days}d after the theft")
            else:
                score -= 3
                reasons.append("listed BEFORE the theft — not it")
        except Exception:
            pass

    flags = [s for s in SUSPICIOUS if norm(s) in text]
    if flags:
        score += 1
        reasons.append("wording: " + ", ".join(flags[:2]))

    return score, reasons, bool(hit_strong or hit_mid)


def is_bike(it):
    if it.get("category_id") in BIKE_CATEGORY_IDS:
        return True
    text = norm(it.get("title", ""))
    return any(w in text for w in GENERIC_BIKE_WORDS)


def item_url(it):
    return "https://es.wallapop.com/item/" + (it.get("web_slug") or it.get("id", ""))


def item_line(it, prof, extra=""):
    price = (it.get("price") or {}).get("amount") or 0
    dist, loc = item_geo(it, prof)
    city = (loc or {}).get("city") or "?"
    created = it.get("created_at")
    when = ""
    if created:
        age = datetime.now(timezone.utc) - datetime.fromtimestamp(created / 1000, timezone.utc)
        when = f"{int(age.total_seconds() // 3600)}h ago" if age < timedelta(days=1) \
            else f"{age.days}d ago"
    where = f"{city}" + (f", {dist:.0f} km" if dist is not None else "")
    head = f"*{it.get('title','')[:70]}* — €{price:.0f}"
    meta = " · ".join(x for x in (where, when) if x)
    return f"{head}\n{meta}\n{item_url(it)}" + (f"\n_{extra}_" if extra else "")


# ---------------------------------------------------------------- channels

def brand_token_set(prof):
    """Only the tokens that identify the BRAND — brand name, its misspellings
    and the model names. These are the tripwire; extra_keywords ("yepp",
    "portabebe") are not, because they match accessories all day."""
    out = []
    for t in norm(prof.get("brand", "")).split():
        if len(t) > 2:
            out.append(t)
    for kw in list(prof.get("brand_aliases", [])) + list(prof.get("model_aliases", [])):
        kw = norm(kw).strip()
        if len(kw) > 2:
            out.append(kw)
    return sorted(set(out))


def run_hits(prof, seen, threshold=6):
    strong, mid, weak = bike_tokens(prof)
    brand_tokens = brand_token_set(prof)
    queries = build_queries(prof)
    if not queries:
        log("no brand/model/keywords in data/bike.json — HITS channel idle "
            "(sweep still runs). Set one with ./set-bike.sh")
        return []
    log(f"hits: {len(queries)} queries {queries}")
    found, by_id = [], {}
    # 2 pages, not MAX_PAGES: a brand query exhausts in one page, and the
    # keyword queries ("thule", "portabebe") are mostly junk past page 2.
    # With ~10 queries the page budget is what decides how long a scan takes.
    for q in queries:
        for it in fetch(q, pages=2):
            by_id[it["id"]] = it
    for it in by_id.values():
        if not is_bike(it) or not is_whole_bike(it):
            continue
        dist, _ = item_geo(it, prof)
        if dist is not None and dist > prof["hit_radius_km"]:
            continue
        score, reasons, matched = score_item(it, prof, strong, mid, weak)
        # The brand tripwire. "<BRAND>" and its model names are rare enough
        # nationally (2 listings in the whole of Spain on 2026-09-07) that a
        # brand match is worth pushing on its own — waiting for the context
        # points to reach 6 would silently swallow a <BRAND> listed in Sevilla at
        # full price, which is exactly the listing worth seeing.
        text = norm(it.get("title", "") + " " + (it.get("description") or ""))
        brand_hit = [t for t in brand_tokens if has_token(text, t)]
        # ...but never for something listed before the bike was taken. That is
        # the one signal that settles it, and score_item already docks 3 points
        # for it — the tripwire must not step over that.
        pre_theft = any("BEFORE the theft" in r for r in reasons)
        if brand_hit and not pre_theft and it["id"] not in seen:
            if score < threshold:
                reasons = reasons + [f"pushed on the brand tripwire "
                                     f"({'/'.join(brand_hit)}) below threshold"]
            found.append((max(score, threshold), it, reasons))
            continue
        if matched and score >= threshold and it["id"] not in seen:
            found.append((score, it, reasons))
    found.sort(key=lambda x: -x[0])
    return found


# Listings in the bike category that are not a rideable bike. Only applied when
# the title has no bike word, so "cuadro de bicicleta" survives — a stripped
# frame is exactly what a thief sells.
# A parts word in the first three words of a title means the listing is about
# the part, not the object. For bicycles "cuadro" (frame) is deliberately
# absent from this list -- a stripped frame is exactly what a thief sells.
PARTS_WORDS = objectpack.words(PACK, "parts")
BIKE_WORDS = objectpack.words(PACK, "head")


def is_whole_bike(it):
    """"Silla portabebé para bicicleta" contains "bicicleta" and is not a bike.
    The head noun decides: a parts word in the first three words of the title
    means the listing is about the part. "cuadro" is deliberately absent —
    a stripped frame is exactly what a thief sells.
    """
    t = norm(it.get("title", ""))
    head = " ".join(t.split()[:3])
    if any(has_token(head, w) for w in PARTS_WORDS):
        return False
    if any(has_token(t, w) for w in BIKE_WORDS):
        return True
    return not any(has_token(t, w) for w in PARTS_WORDS)


# (keyword, pages), from the pack. The broad terms fill 5 pages in ~40 min of
# national listings so they need the depth; the narrow ones reach back a full
# day on 2.
SWEEP_QUERIES = objectpack.sweep_queries(PACK)


def sweep_queries(prof):
    """SWEEP_QUERIES plus the genre terms from the profile.

    The broad terms are what the sweep runs on, but the national newest-first
    feed is shallow and a drop-bar steel bike listed as "gravel" or "bici
    carretera" can fall off the back of "bicicleta" between two scans. These
    reach the right genre directly and are cheap — each exhausts in a page or
    two.
    """
    qs = list(SWEEP_QUERIES)
    have = {norm(q) for q, _ in qs}
    for q in prof.get("genre_queries", []):
        if norm(q) not in have:
            have.add(norm(q))
            qs.append((q, 2))
    return tuple(qs)


def sweep_scan(prof, seen, exclude=(), order="newest", pages=None,
               queries=None):
    """Every bike near the theft site in the price band, brand-agnostic.

    The national newest-first feed only spans ~27 min for 4 pages, so this has
    to run on a short cron and bank what it finds; the digest reads the bank.
    """
    strong, mid, weak = bike_tokens(prof)
    queries = queries or sweep_queries(prof)
    c = prof["centre"]
    by_id = {}
    q_oldest = []          # oldest item each query reached back to
    for q, q_pages in queries:
        items = fetch(q, pages=pages or q_pages, lat=c["lat"], lon=c["lon"],
                      order=order)
        stamps = [i["created_at"] for i in items if i.get("created_at")]
        if stamps:
            q_oldest.append(min(stamps))
        for it in items:
            by_id[it["id"]] = it
    log(f"sweep scan ({order}): {len(by_id)} raw listings")

    # Coverage check. The newest-first feed is national and only a few pages
    # deep, so if the cron interval is longer than the window it covers, local
    # listings vanish between runs and nothing complains. This complains.
    if order == "newest" and q_oldest:
        if True:
            # the binding constraint is the BROADEST query — the one whose feed
            # runs out soonest. Taking min() across all queries would hide it
            # behind a narrow query that reaches back a day.
            window_start = max(q_oldest) / 1000
            state = load_json(STATE_FILE, {})
            last = state.get("last_scan_ts")
            if last and window_start > last + 60:
                log(f"WARNING: coverage gap of "
                    f"{(window_start - last) / 60:.0f} min — the feed no longer "
                    f"reaches back to the previous scan. Raise MAX_PAGES or "
                    f"shorten the cron interval.")
            state["last_scan_ts"] = time.time()
            state["last_window_start"] = window_start
            state["last_window_min"] = round((time.time() - window_start) / 60)
            save_json(STATE_FILE, state)
    out = []
    stolen = prof.get("stolen_on")
    for it in by_id.values():
        if it["id"] in seen or it["id"] in exclude or not is_bike(it):
            continue
        if not is_whole_bike(it):
            continue
        dist, _ = item_geo(it, prof)
        if dist is None or dist > prof["sweep_radius_km"]:
            continue
        # Two rings. Inside sweep_near_km nothing changes — every bike is
        # banked and shown, because at that range your eye is the filter and it
        # is a better one than any model. Outside it the volume is several
        # times larger and unreadable, so those listings are banked with
        # _ring="far" and the digest only shows the ones that clear the visual
        # gate in visual.py.
        near = prof.get("sweep_near_km") or prof["sweep_radius_km"]
        it["_ring"] = "near" if dist <= near else "far"
        it["_dist_km"] = round(dist)
        price = (it.get("price") or {}).get("amount") or 0
        if not (prof["sweep_min_price"] <= price <= prof["sweep_max_price"]):
            continue
        created = it.get("created_at")
        if created and stolen:
            try:
                t = datetime.fromtimestamp(created / 1000, timezone.utc)
                s = datetime.strptime(stolen, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if t < s:
                    continue          # listed before the bike was taken
            except Exception:
                pass
        score, reasons, _ = score_item(it, prof, strong, mid, weak)
        out.append((score, it, reasons))
    out.sort(key=lambda x: -x[0])
    return out


# ---------------------------------------------------------------- telegram

def send_telegram(text):
    if notify.BACKEND != "telegram":
        return notify.emit("text", text)
    token = os.environ.get("TELEGRAM_TOKEN")
    user = os.environ.get("TELEGRAM_USER_ID")
    if not token or not user:
        print(f"{LOG} TELEGRAM_TOKEN / TELEGRAM_USER_ID missing", file=sys.stderr)
        return False
    payload = _route({"text": text, "parse_mode": "Markdown",
                      "disable_web_page_preview": True})
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json=payload, timeout=TIMEOUT)
    if not r.ok:
        payload.pop("parse_mode")
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json=payload, timeout=TIMEOUT)
    if not r.ok:
        print(f"{LOG} telegram error: {r.text[:200]}", file=sys.stderr)
    return r.ok


def _tg(method, payload):
    token = os.environ.get("TELEGRAM_TOKEN")
    user = os.environ.get("TELEGRAM_USER_ID")
    if not token or not user:
        print(f"{LOG} TELEGRAM_TOKEN / TELEGRAM_USER_ID missing", file=sys.stderr)
        return False
    payload = _route(payload)
    # A full sweep is 150+ photos, which is well past Telegram's ~20 msg/min
    # per-chat limit. Without this the tail of the digest is silently dropped:
    # 429 comes back with the exact wait in retry_after, so wait it out.
    for attempt in range(6):
        try:
            # sendMediaGroup makes Telegram fetch 10 remote images, which
            # regularly runs past the normal timeout. An uncaught Timeout here
            # used to abort the whole digest mid-send.
            r = requests.post(f"https://api.telegram.org/bot{token}/{method}",
                              json=payload, timeout=90)
        except requests.RequestException as e:
            log(f"telegram {method} network error ({e.__class__.__name__}), retrying")
            time.sleep(4)
            continue
        if r.ok:
            return True
        if r.status_code == 429:
            wait = 3
            try:
                wait = int((r.json().get("parameters") or {}).get("retry_after", 3))
            except Exception:
                pass
            log(f"telegram 429 — waiting {wait + 1}s")
            time.sleep(wait + 1)
            continue
        print(f"{LOG} telegram {method} error: {r.text[:200]}", file=sys.stderr)
        return False
    print(f"{LOG} telegram {method}: gave up after 6 attempts", file=sys.stderr)
    return False


def first_image(it):
    imgs = it.get("images") or []
    if not imgs:
        return None
    urls = imgs[0].get("urls") or {}
    return urls.get("big") or urls.get("medium") or urls.get("small")


def _fetch_bytes(url):
    """Pull the image here rather than handing Telegram a URL.

    Telegram's own fetcher returns photos for cdn.wallapop.com that its CDN
    then refuses to serve to the desktop client — the transfer starts and
    stalls at 0% forever (mobile has its own cache, so it looks fine there).
    It also 400s outright with WEBPAGE_CURL_FAILED on URLs this box reads at
    HTTP 200, and sendMediaGroup is all-or-nothing, so one refused image
    silently dropped all ten listings while the digest still printed
    "94 shown of 94 banked".
    """
    try:
        r = requests.get(url, headers=HEADERS, timeout=25)
        if r.status_code != 200 or len(r.content) < 2000:
            raise ValueError(f"{r.status_code}/{len(r.content)}B")
        return r.content
    except Exception as e:
        log(f"image fetch failed, skipping ({e})")
        return None


def _tg_multipart(method, payload, files):
    """_tg, but multipart so attach:// bodies ride along with the JSON."""
    token = os.environ.get("TELEGRAM_TOKEN")
    user = os.environ.get("TELEGRAM_USER_ID")
    if not token or not user:
        print(f"{LOG} TELEGRAM_TOKEN / TELEGRAM_USER_ID missing", file=sys.stderr)
        return False
    payload = _route(payload)
    data = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
            for k, v in payload.items()}
    for attempt in range(6):
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/{method}",
                              data=data, files=files, timeout=180)
        except requests.RequestException as e:
            log(f"telegram {method} network error ({e.__class__.__name__}), retrying")
            time.sleep(4)
            continue
        if r.ok:
            return True
        if r.status_code == 429:
            wait = 3
            try:
                wait = int((r.json().get("parameters") or {}).get("retry_after", 3))
            except Exception:
                pass
            log(f"telegram 429 — waiting {wait + 1}s")
            time.sleep(wait + 1)
            continue
        print(f"{LOG} telegram {method} error: {r.text[:200]}", file=sys.stderr)
        return False
    print(f"{LOG} telegram {method}: gave up after 6 attempts", file=sys.stderr)
    return False


def send_batch(batch):
    """One album, uploaded as bytes. A dead image costs one photo, not ten."""
    if notify.BACKEND != "telegram":
        return notify.emit("album", "", [m.get("media") for m in batch])
    files, media = {}, []
    for m in batch:
        body = _fetch_bytes(m["media"])
        if body is None:
            continue
        key = f"p{len(media)}"
        files[key] = (f"{key}.jpg", body, "image/jpeg")
        media.append(dict(m, media=f"attach://{key}"))
    if not media:
        log("album: whole batch unfetchable, nothing sent")
        return 0
    if len(media) < len(batch):
        log(f"album: {len(batch) - len(media)} of {len(batch)} images unfetchable, sending the rest")
    return len(media) if _tg_multipart("sendMediaGroup", {"media": media}, files) else 0


def send_photo(url, caption):
    """A hit is worth a picture — the owner recognises the bike by sight, not title."""
    if not url:
        return send_telegram(caption)
    if notify.BACKEND != "telegram":
        return notify.emit("photo", caption, [url])
    body = _fetch_bytes(url)
    if body is None:
        return send_telegram(caption)
    return _tg_multipart("sendPhoto",
                         {"caption": caption[:1000], "parse_mode": "Markdown"},
                         {"photo": ("hit.jpg", body, "image/jpeg")})


def send_album(items, prof):
    """Sweep thumbnails, 10 at a time — the point is to eyeball them fast."""
    batch, sent = [], 0
    for _, it, _ in items:
        u = first_image(it)
        if not u:
            continue
        price = (it.get("price") or {}).get("amount") or 0
        # The photo score and the distance go in the caption so you can see WHY
        # a listing from Zaragoza is in your digest — the ranking is auditable
        # rather than a black box.
        tag = ""
        if it.get("_vlm_named"):
            tag += " · ⚠ NAME MATCH"
        if it.get("_vlm") is not None and it["_vlm"] >= 0:
            tag += f" · match {it['_vlm']}/10"
        if it.get("_visual") is not None:
            tag += f" · photo {it['_visual']}"
        if it.get("_ring") == "far" and it.get("_dist_km"):
            tag += f" · {it['_dist_km']} km"
        # The one-line reason rides along so the cut is calibratable from the
        # digest itself: if something is being shown or hidden wrongly, the
        # sentence says which feature the model thought it saw.
        why = it.get("_vlm_reason")
        batch.append({"type": "photo", "media": u,
                      "caption": f"€{price:.0f}{tag} — "
                                 f"{it.get('title','')[:60]}"
                                 + (f"\n_{why}_" if why else "")
                                 + f"\n{item_url(it)}"})
        if len(batch) == 10:
            sent += send_batch(batch)
            batch = []
            time.sleep(3.0)
    if batch:
        sent += send_batch(batch)
    log(f"album: {sent} photos sent")
    return sent


ARCHIVE_FILE = os.path.join(DATA, "sweep-archive.jsonl")


def archive(rows):
    """Append-only record of every sweep listing ever shown.

    The digest used to clear pending.json outright, and since the ids were
    already in seen.json those listings could never be recovered or re-shown.
    Now the flushed bank lands here first, so --catchup can rebuild the full
    history even for listings Wallapop has since deleted.
    """
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(ARCHIVE_FILE, "a") as f:
        for r in rows:
            f.write(json.dumps(dict(r, _archived=stamp), ensure_ascii=False) + "\n")


def load_archive():
    if not os.path.exists(ARCHIVE_FILE):
        return []
    out = []
    for line in open(ARCHIVE_FILE):
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def record(items):
    with open(HITS_FILE, "a") as f:
        for score, it, reasons in items:
            f.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "score": score, "id": it["id"], "title": it.get("title"),
                "price": (it.get("price") or {}).get("amount"),
                "url": item_url(it), "user_id": it.get("user_id"),
                "reasons": reasons,
            }, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- main

VISION_PY = os.path.join(HERE, ".venv-vision", "bin", "python")


def run_visual(paths=("pending",)):
    """Score the bank with visual.py, in its own interpreter.

    It lives in .venv-vision because fastembed/onnxruntime have no business in
    the agent's venv, and it is called with subprocess rather than imported so
    that a broken model download can never take the digest down with it. Best
    effort: if it fails, the digest goes out unranked, which is what it did
    before this existed.
    """
    if not os.path.exists(VISION_PY):
        log("visual.py: .venv-vision missing — digest unranked")
        return False
    import subprocess
    try:
        # nice: the CLIP pass is 4-core-bound for ~15 s and this box has been
        # OOM/CPU-thrashed before. It must never be what makes something else
        # on the box miss its slot.
        r = subprocess.run(["nice", "-n", "10", VISION_PY,
                            os.path.join(HERE, "visual.py"), "score"],
                           capture_output=True, text=True, timeout=900)
        for line in (r.stdout or "").strip().splitlines():
            log(line)
        if r.returncode != 0:
            log(f"visual.py failed rc={r.returncode}: {(r.stderr or '')[-300:]}")
            return False
        return True
    except Exception as e:
        log(f"visual.py error: {e}")
        return False


def run_vlm():
    """Recognition pass — vlm.py. Attaches _vlm (0-10) and _vlm_reason.

    Same contract as run_visual: own subprocess, best effort, a failure
    downgrades the digest to the CLIP ranking rather than taking it down.
    This is what makes the digest four photos long instead of five hundred;
    see the measurements at the top of vlm.py before changing the thresholds.
    """
    import subprocess
    env = dict(os.environ)
    if not env.get("ANTHROPIC_API_KEY"):
        log("vlm.py: no ANTHROPIC_API_KEY in the environment — CLIP ranking only")
        return False
    try:
        r = subprocess.run([sys.executable, os.path.join(HERE, "vlm.py"), "score"],
                           capture_output=True, text=True, timeout=1800, env=env)
        for line in (r.stdout or "").strip().splitlines():
            log(line)
        if r.returncode != 0:
            log(f"vlm.py failed rc={r.returncode}: {(r.stderr or '')[-300:]}")
            return False
        return True
    except Exception as e:
        log(f"vlm.py error: {e}")
        return False


def compact(it, score, reasons):
    return {"id": it["id"], "title": it.get("title"), "user_id": it.get("user_id"),
            "price": it.get("price"),
            "created_at": it.get("created_at"), "web_slug": it.get("web_slug"),
            "location": it.get("location"), "images": (it.get("images") or [])[:1],
            "category_id": it.get("category_id"),
            "_ring": it.get("_ring", "near"), "_dist_km": it.get("_dist_km"),
            "_score": score, "_reasons": reasons}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true",
                    help="scan: push hits now, bank sweep finds for the digest")
    ap.add_argument("--digest", action="store_true",
                    help="flush the banked sweep finds to Telegram")
    ap.add_argument("--backfill", action="store_true",
                    help="deep local sweep (order_by=closest) for everything "
                         "already listed since the theft")
    ap.add_argument("--seed", action="store_true",
                    help="mark everything currently listed as seen, send nothing")
    ap.add_argument("--test", action="store_true", help="print only, write no state")
    ap.add_argument("--report", action="store_true", help="show what has been flagged")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap how many bikes a digest shows (default: all of them)")
    ap.add_argument("--far", action="store_true",
                    help="with --catchup: include the outer ring "
                         "(sweep_near_km..sweep_radius_km) too")
    ap.add_argument("--catchup", action="store_true",
                    help="rebuild and re-send EVERY bike listed since the theft: "
                         "deep local rescan ignoring seen.json, merged with the "
                         "bank and the archive. Sends photos, changes no state.")
    args = ap.parse_args()

    os.makedirs(DATA, exist_ok=True)
    prof = load_profile()

    if args.report:
        rows = [json.loads(l) for l in open(HITS_FILE)] if os.path.exists(HITS_FILE) else []
        pending = load_json(PENDING_FILE, [])
        st = load_json(STATE_FILE, {})
        print(f"{len(pending)} bikes banked for the next digest")
        if st.get("last_scan_ts"):
            when = datetime.fromtimestamp(st["last_scan_ts"], timezone.utc)
            print(f"last scan {when:%Y-%m-%d %H:%M} UTC, feed window "
                  f"{st.get('last_window_min', '?')} min")
        if not rows:
            print("no scored hits yet")
        for r in rows[-40:]:
            print(f"{r['ts']}  score {r['score']:>2}  EUR{r['price'] or 0:.0f}  "
                  f"{r['title'][:55]}\n    {r['url']}\n    {'; '.join(r['reasons'])}")
        return

    seen, seen_stamps = load_seen()
    pending = load_json(PENDING_FILE, [])
    banked = {p["id"] for p in pending}

    # ---- catchup: everything listed since the theft, in one pass
    if args.catchup:
        # Three sources, unioned by id:
        #   1. a deep LOCAL rescan (order_by=closest respects lat/lon, so it
        #      returns Barcelona only and reaches back far past the ~40 min the
        #      national newest feed covers),
        #   2. the bank waiting for the next digest,
        #   3. the archive of everything already shown.
        # seen.json is deliberately NOT consulted — the point is to re-show
        # listings you have already been sent, so nothing is missed by having
        # scrolled past it.
        log("catchup: deep local rescan, ignoring seen.json")
        deep = sweep_scan(prof, set(), order="closest", pages=10,
                          queries=(("bicicleta", 10), ("bici", 10),
                                   ("mountain bike", 6), ("bicicleta electrica", 4),
                                   ("bicicleta gravel", 4), ("bicicleta paseo", 4)))
        merged = {}
        for row in load_archive():
            merged[row["id"]] = row
        for row in pending:
            merged[row["id"]] = row
        for sc, it, rs in deep:
            merged[it["id"]] = compact(it, sc, rs)
        # The archive and the bank were already filtered at scan time; the deep
        # results too. What is left is to drop anything predating the theft,
        # which is the only filter a stale archived row could have escaped.
        stolen = prof.get("stolen_on")
        rows = []
        for row in merged.values():
            created = row.get("created_at")
            if created and stolen:
                try:
                    t = datetime.fromtimestamp(created / 1000, timezone.utc)
                    sd = datetime.strptime(stolen, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    if t < sd:
                        continue
                except Exception:
                    pass
            if not (row.get("images") or []):
                continue          # no photo is no use in a recognition task
            # The outer ring (sweep_near_km..sweep_radius_km) is only readable
            # through visual.py's gate, and catchup has no bank to score, so it
            # stays local unless you ask. Without this, widening the radius to
            # 400 km turned --catchup into a thousand-photo dump.
            if not args.far and row.get("_ring") == "far":
                continue
            rows.append(row)
        rows.sort(key=lambda r: (-(r.get("_score") or 0), -(r.get("created_at") or 0)))
        log(f"catchup: {len(deep)} from rescan, {len(pending)} banked, "
            f"{len(load_archive())} archived -> {len(rows)} unique with photos")

        if args.test:
            for r in rows:
                print(item_line(r, prof, f"score {r.get('_score')}"), "\n")
            print(f"{len(rows)} listings (nothing sent)")
            return

        c = prof["centre"]
        send_telegram(
            f"\U0001f6b2 *Full catch-up* — every bike listed within "
            f"{prof['sweep_radius_km'] if args.far else (prof.get('sweep_near_km') or prof['sweep_radius_km'])}"
            f" km of {c['label']} since "
            f"{prof['stolen_on']}, \u20ac{prof['sweep_min_price']}\u2013"
            f"{prof['sweep_max_price']}: *{len(rows)} listings*, "
            f"including the ones already sent. Ranked by fit to the <BRAND>. "
            f"Price and link are in each caption.")
        sent = send_album([(r.get("_score", 0), r, r.get("_reasons", [])) for r in rows], prof)
        send_telegram(f"\u2705 Catch-up done \u2014 {sent} photos. "
                      f"Nothing held back.")
        # archive the whole merged set, not just the bank: a listing recovered
        # by the deep rescan is exactly the kind that would otherwise be lost
        # when the seller deletes it.
        known = {r["id"] for r in load_archive()}
        archive([r for r in rows if r["id"] not in known])
        save_json(PENDING_FILE, [])
        seen.update(r["id"] for r in rows)
        save_seen(seen, seen_stamps)
        return

    # ---- digest-only: flush the bank, scan nothing
    if args.digest and not (args.send or args.test or args.backfill):
        if not pending:
            log("digest: nothing banked")
            return
        # Photo pass: attaches _visual (0-100) and _vpass to every banked row.
        # Re-reads pending.json afterwards because visual.py writes it back.
        if run_visual():
            pending = load_json(PENDING_FILE, pending)
        # Recognition pass: attaches _vlm (0-10). Both passes write the bank
        # back to disk, so re-read after each.
        vlm_ran = run_vlm()
        if vlm_ran:
            pending = load_json(PENDING_FILE, pending)
        # The VLM cut. This is the brake that actually shortens the digest:
        # the near ring used to be ungated, so ~530 photos a day went to
        # Telegram and 70% of them were not even drop-bar bikes. Measured on
        # 120 archived listings, 119 score <= 2 and the reference photos score
        # 8-9 even repainted, stripped, dark or at thumbnail size — so a
        # threshold of 4 shows roughly four photos a day and still fires on
        # the bike. Same never-drop rule as every other cut here: what is held
        # back is archived with `_held`, and a scoring ERROR (-1) is SHOWN,
        # because "the model fell over" must never read as "not your bike".
        #
        # EXACT NAMES ARE NEVER CUT. The tripwire in run_hits pushes on a
        # brand/model match before any of this runs, so in the normal case a
        # <BRAND> never reaches the sweep at all. But the two channels use
        # different queries — the tripwire searches the names, the sweep
        # searches "bicicleta"/"gravel" — and Wallapop's national newest-first
        # feed can surface a listing in the sweep that the brand query missed.
        # If that ever happens, a listing with "<BRAND>" or "<MODEL>" in the
        # title must not be thrown away because a model scored the photo a 2.
        # The name is harder evidence than the picture.
        vlm_cut = 0
        if vlm_ran:
            th = prof.get("vlm_show_threshold", 4)
            brand_tokens = brand_token_set(prof)
            keep, drop = [], []
            for p in pending:
                v = p.get("_vlm")
                title = norm(p.get("title", ""))
                named = any(has_token(title, t) for t in brand_tokens)
                if named:
                    p["_vlm_named"] = True
                (keep if (named or v is None or v < 0 or v >= th)
                 else drop).append(p)
            if drop:
                pending, vlm_cut = keep, len(drop)
                archive([dict(p, _held=f"below the VLM cut (<{th})") for p in drop])
                log(f"digest: VLM cut {vlm_cut} of {vlm_cut + len(keep)} "
                    f"(threshold {th}) — archived, not shown")
        # The far-ring cap, applied to WHAT SURVIVED the VLM, and ordered by
        # the VLM score. It used to run first and rank by CLIP, which meant
        # the weaker filter got to throw away listings the stronger one had
        # already judged — 20 of them on the first live run. The cap stays
        # because it is calibration-independent: however the gate drifts, at
        # most `sweep_far_max` listings from 150-400 km ride along. Held-back
        # rows go to the archive, not to /dev/null.
        far_max = prof.get("sweep_far_max", 12)
        far = sorted([p for p in pending if p.get("_ring") == "far"],
                     key=lambda p: (-(p.get("_vlm") if p.get("_vlm") is not None else -1),
                                    -(p.get("_visual") or 0),
                                    -(p.get("created_at") or 0)))
        keep_far = {p["id"] for p in far[:far_max]}
        held = [p for p in far if p["id"] not in keep_far]
        if held:
            held_ids = {p["id"] for p in held}
            pending = [p for p in pending if p["id"] not in held_ids]
            archive([dict(p, _held="over the far-ring cap") for p in held])
            log(f"digest: far ring {len(far)} -> {len(keep_far)} shown, "
                f"{len(held)} over the cap (archived, not shown)")
        # Ranked by recognition first, then by the CLIP genre score. For a
        # sweep listing the text score is almost all context — distance,
        # price, date — and context was never the case; the picture is.
        pending.sort(key=lambda p: (-(p.get("_vlm") if p.get("_vlm") is not None else -1),
                                    -(p.get("_visual") or 0),
                                    -(p.get("_score") or 0),
                                    -(p.get("created_at") or 0)))
        # Every banked bike is shown. The old top-N cap threw the remainder
        # away — the ids were already in seen.json so they never came back,
        # and 26 listings were lost that way on 2026-09-05. Cap only if
        # --limit is passed explicitly.
        top = pending[: args.limit] if args.limit else pending
        rest = len(pending) - len(top)
        near = prof.get("sweep_near_km") or prof["sweep_radius_km"]
        nfar = sum(1 for p in top if p.get("_ring") == "far")
        scanned = len(top) + vlm_cut
        if vlm_ran and not top:
            # The common case now, and it deserves one line rather than
            # silence: silence is indistinguishable from a dead cron, and this
            # monitor has already lost 107 listings to exactly that ambiguity.
            send_telegram(
                f"\U0001f50d *Wallapop sweep* \u2014 nothing worth your eyes. "
                f"{vlm_cut} bike{'s' if vlm_cut != 1 else ''} listed near "
                f"{prof['centre']['label']} since the last digest; not one is a "
                f"navy steel drop-bar bike. _Archived, not shown._")
            archive(pending)
            save_json(PENDING_FILE, [])
            log(f"digest sent: 0 shown, {vlm_cut} ruled out by the VLM")
            return
        head = (f"\U0001f50d *Wallapop sweep* \u2014 {len(top)} bike"
                f"{'s' if len(top) != 1 else ''} worth a look"
                + (f", out of {scanned} listed near {prof['centre']['label']}"
                   if vlm_cut else f" listed near {prof['centre']['label']}")
                + f" (\u2264{near} km, \u20ac{prof['sweep_min_price']}"
                  f"\u2013{prof['sweep_max_price']}) since the last digest"
                + (f", plus {nfar} from {near}\u2013{prof['sweep_radius_km']} km"
                   if nfar else "")
                + (". Ranked by how plausibly each photo could be *your* bike "
                   "(navy \u00b7 steel \u00b7 level top tube \u00b7 drop bars "
                   "\u00b7 gumwalls \u00b7 the kit), best first."
                   if vlm_ran else
                   ". Every one has a photo below \u2014 look for yours. "
                   "Ranked by the *photo* (drop bars \u00b7 navy \u00b7 steel), "
                   "best first.")
                + (f"\n_{vlm_cut} other{'s' if vlm_cut != 1 else ''} looked at "
                   f"and ruled out \u2014 archived, not shown._" if vlm_cut else "")
                + (f"\n_{len(held)} further listings from the outer ring "
                   f"scored lower on the photo \u2014 archived, not shown._"
                   if held else "")
                + (f"\n_{rest} more held back by --limit._" if rest > 0 else ""))
        send_telegram(head)
        shown = send_album([(p["_score"], p, p["_reasons"]) for p in top], prof)
        archive(top)
        save_json(PENDING_FILE, pending[len(top):])
        # Report what actually went out, not what we meant to send. The old
        # summary counted intent, so when sendMediaGroup silently dropped a
        # batch it still printed "94 shown of 94 banked" -- which is how 107
        # listings went missing for eleven days without a single error line.
        log(f"digest sent: {shown} shown of {len(pending)} banked"
            + (f" -- {len(top) - shown} NOT SENT (image unfetchable)"
               if shown < len(top) else ""))
        return

    # ---- scan
    hits = run_hits(prof, seen)
    hit_ids = {it["id"] for _, it, _ in hits}

    if args.backfill:
        sweep = sweep_scan(prof, seen, exclude=hit_ids | banked, order="closest",
                           pages=8, queries=(("bicicleta", 8), ("bici", 8)))
    else:
        sweep = sweep_scan(prof, seen, exclude=hit_ids | banked)

    if args.seed:
        for _, it, _ in hits + sweep:
            seen.add(it["id"])
        save_seen(seen, seen_stamps)
        log(f"seeded {len(seen)} listings, sent nothing")
        return

    if args.test:
        print(f"\n{len(hits)} hits, {len(sweep)} sweep candidates "
              f"(would be banked, not sent)\n")
        for score, it, reasons in hits[:8]:
            print(item_line(it, prof, f"score {score} \u00b7 " + "; ".join(reasons)), "\n")
        for score, it, reasons in sweep[:10]:
            print(item_line(it, prof), "\n")
        return

    if args.send:
        for score, it, reasons in hits[:8]:
            send_photo(first_image(it),
                       item_line(it, prof, f"score {score} \u00b7 " + "; ".join(reasons)))
            time.sleep(1.0)
    elif hits:
        print("\n\n".join(item_line(it, prof, f"score {sc}") for sc, it, _ in hits[:8]))

    record(hits)
    pending.extend(compact(it, sc, rs) for sc, it, rs in sweep)
    save_json(PENDING_FILE, pending)
    # Score at SCAN time, not at digest time. It costs exactly the same — the
    # same photos, scored once and cached by listing id — but it buys the
    # thing a 3-hourly digest cannot: a listing that really looks like the
    # bike gets pushed within 20 minutes instead of waiting up to three hours.
    # The brand tripwire above is untouched and still fires on the name alone.
    # CLIP first, and at scan time too. It is free, local and ~4 s for a
    # batch, and vlm.py's pre-gate reads the `_vattrs` it writes — without
    # this the pre-gate fails open and every photo is paid for.
    if sweep and args.send and run_visual():
        pending = load_json(PENDING_FILE, pending)
    if sweep and args.send and run_vlm():
        pending = load_json(PENDING_FILE, pending)
        hit_th = prof.get("vlm_hit_threshold", 7)
        strong = [p for p in pending
                  if (p.get("_vlm") or -1) >= hit_th and not p.get("_vlm_pushed")]
        for p in strong[:4]:
            send_photo(first_image(p), item_line(
                p, prof, f"\U0001f6b2 *looks like your bike* — "
                         f"{p['_vlm']}/10: {p.get('_vlm_reason','')}"))
            p["_vlm_pushed"] = True
            time.sleep(1.0)
        if strong:
            log(f"{len(strong)} listing(s) at >= {hit_th} pushed immediately")
            save_json(PENDING_FILE, pending)
    for _, it, _ in hits + sweep:
        seen.add(it["id"])
    # ~100 local listings a day; keep the id set from growing without bound
    save_seen(seen, seen_stamps)
    log(f"{len(hits)} hits pushed, {len(sweep)} banked "
        f"({len(pending)} awaiting digest), {len(seen)} seen total")


if __name__ == "__main__":
    main()
