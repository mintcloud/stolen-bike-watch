#!/usr/bin/env python3
"""
milanuncios — second marketplace channel for the stolen-bike watch.

Wallapop is where a Barcelona thief lists first, but it is not the only place:
Milanuncios is the other big Spanish second-hand board and it skews older /
non-app sellers, which is exactly the population a fence sells into. Two days
of Wallapop-only sweeps turned up nothing, so this widens the net.

Same two channels as wallapop_monitor, same bike.json profile, same Telegram
chat — only the source differs:

  HITS   brand/model text match -> pushed the moment it appears.
  SWEEP  every whole bike in the bike categories inside the radius and price
         band, listed after the theft -> banked, flushed as a photo digest.

The public site is behind an Imperva bot wall ("Pardon Our Interruption"),
including under a real headless Chromium. The SPA's own search backend,
searchapi.gw.milanuncios.com/v1/listing, is not: it answers a plain
unauthenticated GET. That is the endpoint used here — read-only, no account,
nothing behind a login.

Two API quirks worth keeping in the code:
  * pagination is `offset`, NOT the `nextToken` the response advertises.
    Feeding nextToken back returns page 1 again, forever — five "pages" of the
    same thirty ads, which looks like full coverage and is not.
  * there are no working price / category query params, so the band and the
    bike-category filter are applied client-side on the returned ads.

Usage (via run-milanuncios.sh):
  --test     one pass, print only, no Telegram, no state written
  --send     scan: push hits, bank sweep finds
  --digest   flush the banked sweep as a photo album
  --report   what has been flagged so far
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
from datetime import datetime, timezone, timedelta

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from wallapop_monitor import (           # noqa: E402  (path set above)
    DATA, PACK, SUSPICIOUS, bike_tokens, brand_token_set, build_queries, has_token,
    is_whole_bike, load_json, load_profile, norm, run_visual, run_vlm,
    save_json,
    send_telegram, _tg, send_batch, send_photo as _wm_send_photo,
)

API = "https://searchapi.gw.milanuncios.com/v1/listing"
SITE = "https://www.milanuncios.com"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-ES,es;q=0.9",
    "Origin": SITE,
    "Referer": SITE + "/",
}
TIMEOUT = 25
PAGE = 30                                # the API's fixed page size

SEEN_FILE = os.path.join(DATA, "mil-seen.json")
PENDING_FILE = os.path.join(DATA, "mil-pending.json")
HITS_FILE = os.path.join(DATA, "mil-hits.jsonl")
ARCHIVE_FILE = os.path.join(DATA, "mil-archive.jsonl")

LOG = "[milanuncios]"

# Broad sweep terms, (query, pages). Milanuncios carries ~10-20 new bikes a day
# inside 150 km, so two pages per term already reach back well past a day —
# nothing like the 5-page-per-20-minutes race the Wallapop national feed needs.
SWEEP_QUERIES = tuple(
    (q, int(n)) for q, n in PACK.get("sweep_queries_secondary", [])) or (
        ("bicicleta", 3), ("bici", 3), ("bicicleta gravel", 1),
        ("bicicleta carretera", 2), ("mountain bike", 1))


def log(msg):
    print(f"{LOG} {msg}", flush=True)


# ---------------------------------------------------------------- api

def fetch(text, pages=2, lat=41.3874, lon=2.1686, distance_km=150):
    """Date-ordered ads for one query. Returns normalised items."""
    out, photos = {}, {}
    for page in range(pages):
        params = {"text": text, "order": "date", "latitude": lat,
                  "longitude": lon, "distance": int(distance_km * 1000)}
        if page:
            params["offset"] = page * PAGE
        url = API + "?" + urllib.parse.urlencode(params)
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            d = r.json()
        except Exception as e:
            log(f"search failed ({text!r} p{page}): {e}")
            break
        for ph in d.get("photos") or []:
            photos[ph.get("adId")] = ph.get("imageUrls") or []
        ads = d.get("ads") or []
        for a in ads:
            out[a["id"]] = a
        if len(ads) < PAGE:
            break
        time.sleep(0.7)
    return [to_item(a, photos.get(a["id"]) or []) for a in out.values()]


def img_url(raw):
    """Photo urls come back without a scheme, and the two media hosts want
    different things: images-re serves the bare url, while the ma-ad-media-pro
    service 404s ("Invalid Rule") on anything but a whitelisted ?rule=. Of the
    rule names probed, only hw396_70 is accepted — a 396px thumbnail, which is
    all a digest album shows anyway."""
    if not raw:
        return None
    u = raw if raw.startswith("http") else "https://" + raw
    return u + "?rule=hw396_70" if "/ma-ad-media" in u else u


def to_item(ad, image_urls):
    loc = ad.get("location") or {}
    city = (loc.get("city") or {}).get("name")
    province = (loc.get("province") or {}).get("name")
    price = ((ad.get("price") or {}).get("cash") or {}).get("value")
    cats = [c.get("name") or "" for c in (ad.get("categories") or [])]
    return {
        "id": str(ad.get("id")),
        "title": ad.get("title") or "",
        "description": ad.get("description") or "",
        "price": float(price) if price is not None else 0.0,
        "url": SITE + (ad.get("url") or ""),
        "city": city or province or "?",
        "province": province,
        "categories": cats,
        "created": ad.get("publicationDate") or ad.get("sortDate"),
        "images": [img_url(u) for u in image_urls[:1] if u],
        "author": ad.get("authorName"),
    }


def created_dt(it):
    s = it.get("created")
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


# ---------------------------------------------------------------- filtering

def in_bike_category(it):
    """Milanuncios files bikes under 'Bicicletas ...' leaves. Trust the
    category when it is there, fall back to the title when it is not."""
    if any("icicleta" in norm(c) for c in it["categories"]):
        return True
    t = norm(it["title"])
    return any(has_token(t, w) for w in ("bicicleta", "bici", "mtb", "btt", "bike"))


def is_accessory_category(it):
    return any("recambios" in norm(c) or "accesorio" in norm(c)
               for c in it["categories"])


def score(it, prof, strong, mid, weak):
    """Same shape as wallapop_monitor.score_item: context breaks ties, it is
    never the whole case. `matched` gates the HITS channel."""
    text = norm(it["title"] + " " + it["description"])
    reasons, s = [], 0

    hit_strong = [t for t in strong if has_token(text, t)]
    if hit_strong:
        s += 3 * len(hit_strong)
        reasons.append("matches " + "/".join(hit_strong))
    hit_mid = [t for t in mid if has_token(text, t)]
    if hit_mid:
        s += 2 * len(hit_mid)
        reasons.append("has " + "/".join(hit_mid))
    hit_weak = [t for t in weak if has_token(text, t)]
    if hit_weak:
        s += len(hit_weak)
        reasons.append("also " + "/".join(hit_weak))

    paid = prof.get("paid_eur") or 0
    if paid and it["price"] and it["price"] < paid * 0.45:
        s += 2
        reasons.append(f"€{it['price']:.0f} vs €{paid:.0f} — under half")

    t = created_dt(it)
    stolen = prof.get("stolen_on")
    if t and stolen:
        st = datetime.strptime(stolen, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if t >= st:
            s += 1
            reasons.append(f"listed {(t - st).days}d after the theft")
        else:
            s -= 3
            reasons.append("listed BEFORE the theft — not it")

    flags = [x for x in SUSPICIOUS if norm(x) in text]
    if flags:
        s += 1
        reasons.append("wording: " + ", ".join(flags[:2]))

    return s, reasons, bool(hit_strong or hit_mid)


def listed_after_theft(it, prof):
    t, stolen = created_dt(it), prof.get("stolen_on")
    if not t or not stolen:
        return True
    st = datetime.strptime(stolen, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return t >= st


# ---------------------------------------------------------------- output

def line(it, extra=""):
    t = created_dt(it)
    when = ""
    if t:
        age = datetime.now(timezone.utc) - t
        when = (f"{int(age.total_seconds() // 3600)}h ago" if age < timedelta(days=1)
                else f"{age.days}d ago")
    meta = " · ".join(x for x in (it["city"], when, "Milanuncios") if x)
    head = f"*{it['title'][:70]}* — €{it['price']:.0f}"
    return f"{head}\n{meta}\n{it['url']}" + (f"\n_{extra}_" if extra else "")


def send_photo(it, caption):
    # Bytes, not a URL: Telegram's own fetcher produces photo records its CDN
    # then refuses to serve to the desktop client (stalls at 0% forever), and
    # 400s outright on URLs this box reads at HTTP 200. Same fix as the
    # Wallapop side -- _wm_send_photo downloads here and uploads multipart.
    if not it["images"]:
        return send_telegram(caption)
    return _wm_send_photo(it["images"][0], caption)


def send_album(items):
    batch, sent = [], 0
    for it in items:
        if not it["images"]:
            continue
        tag = ""
        if it.get("_vlm_named"):
            tag += " · ⚠ NAME MATCH"
        if it.get("_vlm") is not None and it["_vlm"] >= 0:
            tag += f" · match {it['_vlm']}/10"
        if it.get("_visual") is not None:
            tag += f" · photo {it['_visual']}"
        why = it.get("_vlm_reason")
        batch.append({"type": "photo", "media": it["images"][0],
                      "caption": f"€{it['price']:.0f}{tag} — "
                                 f"{it['title'][:60]}"
                                 + (f"\n_{why}_" if why else "")
                                 + f"\n{it['url']}"})
        if len(batch) == 10:
            # send_batch uploads the bytes and degrades per-image: one dead
            # URL costs one photo, where the old all-or-nothing sendMediaGroup
            # silently dropped the whole ten and still reported them as shown.
            sent += send_batch(batch)
            batch = []
            time.sleep(3.0)
    if batch:
        sent += send_batch(batch)
    log(f"album: {sent} photos sent")
    return sent


def archive(rows):
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(ARCHIVE_FILE, "a") as f:
        for r in rows:
            f.write(json.dumps(dict(r, _archived=stamp), ensure_ascii=False) + "\n")


def record(rows):
    with open(HITS_FILE, "a") as f:
        for it, s, reasons in rows:
            f.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "score": s, "id": it["id"], "title": it["title"],
                "price": it["price"], "url": it["url"], "reasons": reasons,
            }, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- channels

def run_hits(prof, seen, threshold=6, send=True):
    strong, mid, weak = bike_tokens(prof)
    brand_tokens = brand_token_set(prof)
    queries = build_queries(prof)
    if not queries:
        log("no brand/model in data/bike.json — HITS idle")
        return []
    log(f"hits: {len(queries)} queries {queries}")
    found, by_id = [], {}
    for q in queries:
        for it in fetch(q, pages=2, distance_km=prof.get("hit_radius_km", 400)):
            by_id[it["id"]] = it
    for it in by_id.values():
        if it["id"] in seen:
            continue
        # Milanuncios search is even fuzzier than Wallapop's: "midnight
        # special" returns Jimmy Smith CDs. The category gate is what keeps the
        # widened alias query set from becoming a record-shop feed.
        if not in_bike_category(it) or is_accessory_category(it):
            continue
        s, reasons, matched = score(it, prof, strong, mid, weak)
        # Brand tripwire, same rule as wallapop_monitor.run_hits: a <BRAND> or a
        # <BRAND> model name anywhere in the text is pushed on its own, without
        # having to earn the context points. The brand is rare enough that this
        # costs nothing.
        text = norm(it.get("title", "") + " " + (it.get("description") or ""))
        brand_hit = [t for t in brand_tokens if has_token(text, t)]
        if brand_hit and listed_after_theft(it, prof):
            if s < threshold:
                reasons = reasons + [f"brand tripwire ({'/'.join(brand_hit)})"]
            found.append((it, max(s, threshold), reasons))
            continue
        if matched and s >= threshold:
            found.append((it, s, reasons))
    found.sort(key=lambda x: -x[1])
    for it, s, reasons in found:
        seen[it["id"]] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        text = "🚲 *Milanuncios hit*\n\n" + line(it, "; ".join(reasons))
        if send:
            send_photo(it, text)
        else:
            print(text + "\n")
    if found and send:
        record(found)
    return found


def run_sweep(prof, seen):
    """Every whole bike in the band, brand or no brand."""
    strong, mid, weak = bike_tokens(prof)
    by_id = {}
    for q, pages in SWEEP_QUERIES:
        # sweep_near_km, not sweep_radius_km. The outer ring added on
        # 2026-09-07 is a Wallapop-only thing so far: there the far listings are
        # filtered by visual.py's gate before you ever see them, and until the
        # same gate is wired into this digest, widening here would just be more
        # photos.
        for it in fetch(q, pages=pages,
                        distance_km=prof.get("sweep_near_km")
                        or prof.get("sweep_radius_km", 150)):
            by_id[it["id"]] = it
    log(f"sweep scan: {len(by_id)} raw listings")
    out = []
    for it in by_id.values():
        if it["id"] in seen:
            continue
        if not in_bike_category(it) or is_accessory_category(it):
            continue
        if not is_whole_bike(it):
            continue
        if not (prof["sweep_min_price"] <= it["price"] <= prof["sweep_max_price"]):
            continue
        if not listed_after_theft(it, prof):
            continue
        s, reasons, _ = score(it, prof, strong, mid, weak)
        out.append(dict(it, _score=s, _reasons=reasons))
    out.sort(key=lambda x: -x["_score"])
    return out


def digest(prof, send=True):
    pending = load_json(PENDING_FILE, [])
    if not pending:
        log("digest: nothing banked")
        return 0
    # Photo pass — same scorer as the Wallapop digest; it writes mil-pending
    # back with _visual attached, so re-read it.
    if run_visual():
        pending = load_json(PENDING_FILE, pending)
    # Recognition pass — same cut as the Wallapop digest, same never-drop
    # rule: what is below the threshold is archived with _held, and a scoring
    # error (-1) is shown rather than hidden.
    cut = 0
    if run_vlm():
        pending = load_json(PENDING_FILE, pending)
        th = prof.get("vlm_show_threshold", 4)
        keep, drop = [], []
        for x in pending:
            v = x.get("_vlm")
            (keep if (v is None or v < 0 or v >= th) else drop).append(x)
        if drop:
            cut = len(drop)
            archive([dict(x, _held=f"below the VLM cut (<{th})") for x in drop])
            pending = keep
            log(f"digest: VLM cut {cut} of {cut + len(keep)} (threshold {th})")
    pending.sort(key=lambda x: (-(x.get("_vlm") if x.get("_vlm") is not None else -1),
                                -(x.get("_visual") or 0), -(x.get("_score") or 0)))
    if cut and not pending:
        if send:
            send_telegram(f"🚲 *Milanuncios sweep* — nothing worth "
                          f"your eyes. {cut} new bike{'s' if cut != 1 else ''} "
                          f"listed, not one a navy steel drop-bar bike. "
                          f"_Archived, not shown._")
        else:
            print(f"Milanuncios: 0 shown, {cut} ruled out by the VLM")
        save_json(PENDING_FILE, [])
        log(f"digest sent: 0 shown, {cut} ruled out by the VLM")
        return 0
    header = (f"🚲 *Milanuncios sweep* — {len(pending)} new bike"
              f"{'s' if len(pending) != 1 else ''} worth a look"
              + (f", out of {cut + len(pending)}," if cut else "")
              + f" within "
              f"{prof.get('sweep_near_km') or prof.get('sweep_radius_km')} km, "
              f"€{prof['sweep_min_price']}–{prof['sweep_max_price']}, "
              f"listed since the theft. Ranked by how plausibly each photo "
              f"could be *your* bike, best first."
              + (f"\n_{cut} other{'s' if cut != 1 else ''} looked at and ruled "
                 f"out — archived, not shown._" if cut else ""))
    if not send:
        print(header)
        for it in pending:
            print("  " + line(it).replace("\n", " | "))
        return len(pending)
    send_telegram(header)
    shown = send_album(pending)
    archive(pending)
    save_json(PENDING_FILE, [])
    # Count what was actually delivered -- see the note on the Wallapop side.
    log(f"digest sent: {shown} shown of {len(pending)} banked"
        + (f" -- {len(pending) - shown} NOT SENT (image unfetchable)"
           if shown < len(pending) else ""))
    return shown


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--digest", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--seed", action="store_true",
                    help="mark everything currently listed as seen")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    prof = load_profile()
    seen = load_json(SEEN_FILE, {})

    if args.report:
        pending = load_json(PENDING_FILE, [])
        hits = [json.loads(l) for l in open(HITS_FILE)] if os.path.exists(HITS_FILE) else []
        print(f"seen {len(seen)} · banked {len(pending)} · hits {len(hits)}")
        for h in hits[-10:]:
            print(f"  {h['ts']} [{h['score']}] {h['title'][:50]} — {h['url']}")
        return

    if args.test:
        run_hits(prof, dict(seen), send=False)
        found = run_sweep(prof, seen)
        print(f"{LOG} sweep would bank {len(found)}")
        for it in found[:20]:
            print("  " + line(it).replace("\n", " | "))
        return

    if args.seed:
        found = run_sweep(prof, seen)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for it in found:
            seen[it["id"]] = now
        save_json(SEEN_FILE, seen)
        log(f"seeded {len(found)} listings as seen")
        return

    if args.send:
        hits = run_hits(prof, seen)
        found = run_sweep(prof, seen)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        pending = load_json(PENDING_FILE, [])
        known = {p["id"] for p in pending}
        for it in found:
            seen[it["id"]] = now
            if it["id"] not in known:
                pending.append(it)
        save_json(SEEN_FILE, seen)
        save_json(PENDING_FILE, pending)
        log(f"{len(hits)} hits pushed, {len(found)} banked "
            f"({len(pending)} awaiting digest), {len(seen)} seen total")

    if args.digest:
        digest(prof)


if __name__ == "__main__":
    main()
