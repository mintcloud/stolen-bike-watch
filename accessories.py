#!/usr/bin/env python3
"""
accessories.py — the third channel: what is left of the bike after it is
stripped.

The bike sweep watches whole bikes and drops parts on purpose
(`is_whole_bike()` rejects a listing with a parts word in its first three
words; `brand_token_set()` keeps "yepp"/"portabebe" out of the tripwire).
That was the right call for HITS — accessory brands flooded the bike search
with racks — but it closes the most likely door: a <MODEL> loaded with a
child seat, two racks and panniers is worth roughly €430-800 in pieces
against ~€150 as a quick whole-bike cash sale, so stripping is the
economically correct disposal and every piece is invisible to a "blue <BRAND>"
search.

Two channels again, and again they differ in noise tolerance:

  KIT    accessory listings near the theft site, posted after it. ~25/day
         inside 40 km, which is a digest. The unit of interest is NOT the
         item, it is the SELLER: one account posting a child seat AND a rack
         AND a pannier inside a fortnight is a stripped bike. Anything with
         two or more distinct kit categories is promoted to the top of the
         digest with its photos; singletons go out as a compact list.

  LOCKS  "pitlock" / "pinhead" / "hexlox". the owner's wheels and seatpost are on
         coded locking skewers, so these words are the bike's fingerprint.
         Measured nationally on 2026-09-20: 7, 3 and 2 listings in all of
         Spain. Volume that low runs nationally and pushes on sight.

Usage:
  ./run-accessories.sh --test     one pass, print only, no Telegram, no state
  ./run-accessories.sh --scan     bank new listings + push lock tripwires (cron)
  ./run-accessories.sh --digest   flush the bank as a seller-clustered digest
  ./run-accessories.sh --report   what is banked and which sellers cluster
"""

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import wallapop_monitor as W          # API, WAF headers, Telegram, geo, norm

DATA = W.DATA
CONF_FILE = os.path.join(DATA, "accessories.json")
SEEN_FILE = os.path.join(DATA, "acc-seen.json")
PENDING_FILE = os.path.join(DATA, "acc-pending.json")
ARCHIVE_FILE = os.path.join(DATA, "acc-archive.jsonl")
LOCK_HITS_FILE = os.path.join(DATA, "acc-lock-hits.jsonl")

LOG = "[accessories]"
PAGES = 2                             # a narrow accessory query exhausts fast


def log(msg):
    print(f"{LOG} {msg}", flush=True)


def load_conf():
    return W.load_json(CONF_FILE, {})


def theft_ms(conf):
    d = conf.get("theft_date") or "2026-09-05"
    return datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000


def geo(it, conf):
    """km from the theft site, or None if the listing carries no coordinates."""
    loc = it.get("location") or {}
    lat, lon = loc.get("latitude"), loc.get("longitude")
    if lat is None or lon is None:
        return None
    c = conf.get("centre") or {}
    return W.haversine_km(c.get("lat", 41.3874), c.get("lon", 2.1686), lat, lon)


# The sweep's PARTS_WORDS plus the head-nouns this channel actually queries
# for. Singular/plural both, because has_token() is a whole-word match and
# "alforja" does not match the sweep's "alforjas".
ACC_HEAD_WORDS = tuple(set(W.PARTS_WORDS) | {
    "alforja", "parrilla", "portaequipajes", "portaequipaje", "guardabarros",
    "cierre", "cierres", "abrazadera", "abrazaderas", "grupo", "cambio",
    "bielas", "cassette", "casete", "freno", "frenos", "thule", "yepp",
    "vaude", "portabici", "portabicicletas", "transportin", "bolsa",
    "cubierta", "cubiertas", "llanta", "llantas", "buje", "bujes",
})


def is_kit(it, category, conf):
    """Reject the whole bikes that a parts query drags in.

    "cambio trasero shimano" returns complete bikes whose groupset is named in
    the title ("Triban RC 500 Disc Shimano Sora XL", "Cannondale Flash
    Carbono"), and those are the sweep's job — letting them through would
    duplicate hundreds of listings a day into a digest sized for 25. The
    category_id cannot arbitrate: Wallapop files a Yepp seat, a rear
    derailleur and a whole bike alike under 17000, checked 2026-09-20.

    So the head noun decides, and strictly — the part word must be in the
    first three words. "Alforja Massi para bicicleta" is kit; "Bicicleta
    eléctrica estilo clásico" is not.
    """
    head = " ".join(W.norm(it.get("title", "")).split()[:3])
    return any(W.has_token(head, w) for w in ACC_HEAD_WORDS)


def scan_kit(conf, seen, test=False):
    """Every accessory listed near the theft site since the theft."""
    cutoff = theft_ms(conf)
    radius = conf.get("radius_km", 40)
    max_price = conf.get("max_price", 500)
    c = conf.get("centre") or {}
    lat, lon = c.get("lat", 41.3874), c.get("lon", 2.1686)
    found, stats = [], {}
    for category, queries in (conf.get("kit") or {}).items():
        kept = 0
        for q in queries:
            try:
                items = W.fetch(q, pages=PAGES, lat=lat, lon=lon, order="closest")
            except Exception as e:
                log(f"query {q!r} failed: {e}")
                continue
            for it in items:
                iid = it.get("id")
                if not iid or iid in seen:
                    continue
                if (it.get("created_at") or 0) < cutoff:
                    continue
                d = geo(it, conf)
                if d is None or d > radius:
                    continue
                price = (it.get("price") or {}).get("amount") or 0
                if price > max_price:
                    continue
                if not is_kit(it, category, conf):
                    continue
                it["_category"] = category
                it["_query"] = q
                it["_dist_km"] = round(d)
                seen.add(iid)
                found.append(it)
                kept += 1
            time.sleep(0.6)
        stats[category] = kept
    log("kit: " + ", ".join(f"{k} {v}" for k, v in stats.items()) +
        f" — {len(found)} new")
    return found


def scan_locks(conf, seen, test=False):
    """The coded-skewer tripwire. National, because the volume is ~zero.

    A thief who cannot get the wheels or the seatpost off has to grind the
    skewer, sell the bike whole, or go to the manufacturer for a key. If the
    skewers themselves ever surface — or a listing names the system because
    the seller is explaining why the saddle will not move — it is worth an
    instant push. Guarded the same way the <BRAND> tripwire is: a listing dated
    before the theft is somebody else's Pitlock.
    """
    cutoff = theft_ms(conf)
    out = []
    for q, needs_context in (conf.get("locks_national") or {}).items():
        try:
            items = W.fetch(q, pages=1, lat=41.3874, lon=2.1686, order="newest")
        except Exception as e:
            log(f"lock query {q!r} failed: {e}")
            continue
        for it in items:
            iid = it.get("id")
            if not iid or iid in seen:
                continue
            if (it.get("created_at") or 0) < cutoff:
                continue
            title = W.norm(it.get("title", ""))
            if W.norm(q) not in title:
                continue          # Wallapop's search is fuzzy; require the word
            # "pinhead" is also a Hellraiser figure, which is what the first
            # test pass pushed. Ambiguous terms have to land on a bike.
            if needs_context and not any(w in title for w in W.GENERIC_BIKE_WORDS):
                continue
            it["_lock_term"] = q
            d = geo(it, conf)
            it["_dist_km"] = round(d) if d is not None else None
            seen.add(iid)
            out.append(it)
        time.sleep(0.6)
    log(f"locks: {len(out)} new")
    return out


def archive(rows):
    if not rows:
        return
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(ARCHIVE_FILE, "a") as f:
        for it in rows:
            f.write(json.dumps(dict(it, _archived=now), ensure_ascii=False) + "\n")


def load_archive(days=None):
    rows = []
    if not os.path.exists(ARCHIVE_FILE):
        return rows
    floor = None
    if days:
        floor = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with open(ARCHIVE_FILE) as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if floor and (r.get("_archived") or "") < floor:
                continue
            rows.append(r)
    return rows


# A wheel off this bike means a ground-off skewer. A tyre, an inner tube or a
# seat clamp means nothing — they were never locked. Without this the GRINDER
# flag fired on every vintage-parts seller in Sant Martí.
LOCKED_HEADS = ("rueda", "ruedas", "llanta", "llantas", "juego", "sillin",
                "tija", "buje", "bujes")
NOT_LOCKED = ("cubierta", "cubiertas", "camara", "camaras", "abrazadera",
              "abrazaderas", "cierre", "cierres", "funda", "cubre")


def is_locked_part(it, conf):
    """Is this listing one of the parts the owner's coded skewers hold on?"""
    if it.get("_category") not in set(conf.get("locked_categories", [])):
        return False
    head = " ".join(W.norm(it.get("title", "")).split()[:3])
    if any(W.has_token(head, w) for w in NOT_LOCKED):
        return False
    return any(W.has_token(head, w) for w in LOCKED_HEADS)


def clusters(rows, conf):
    """Group the rolling window by seller.

    This is the whole point of the channel. A single Yepp seat in Barcelona is
    a parent; the same account also selling a rack and a pannier is a bike
    that came apart. Ranked by how many distinct kit categories the seller is
    offering, then by how tight the listings are in time.
    """
    by_user = defaultdict(list)
    for r in rows:
        u = r.get("user_id")
        if u:
            by_user[u].append(r)
    signature = set(conf.get("signature_categories", []))
    need = conf.get("cluster_min_categories", 2)
    out = []
    for u, items in by_user.items():
        cats = {i.get("_category") for i in items if i.get("_category")}
        if len(cats) < need:
            continue
        out.append({
            "user_id": u,
            "categories": sorted(cats),
            # the owner's bike carried a child seat, two racks, panniers and
            # mudguards — kit that comes off with an allen key. A seller
            # offering two or more of THOSE is the shape we are looking for;
            # a seller offering a seat clamp and a tyre is a parts hoarder.
            "signature": sorted(cats & signature),
            "has_locked": any(is_locked_part(i, conf) for i in items),
            "items": sorted(items, key=lambda i: i.get("created_at") or 0),
        })
    out.sort(key=lambda c: (len(c["signature"]), len(c["categories"]),
                            c["has_locked"]), reverse=True)
    return out


def seller_url(user_id):
    return f"https://es.wallapop.com/app/user/{user_id}"


def cluster_caption(c):
    cats = "+".join(c["categories"])
    note = ""
    if c["signature"]:
        note += f" · matches kit: {'+'.join(c['signature'])}"
    if c["has_locked"]:
        # The wheels and seatpost are on coded skewers, so a whole wheel or a
        # saddle-and-post from a seller who also has unlocked kit means
        # somebody cut past the lock — a workshop, not an opportunist.
        note += " · LOCKED PART (grinder)"
    span = ""
    ts = [i.get("created_at") for i in c["items"] if i.get("created_at")]
    if len(ts) > 1:
        span = f" · {int((max(ts) - min(ts)) / 86400000)}d apart"
    return (f"*SELLER CLUSTER* — {len(c['categories'])} kit categories: {cats}"
            f"{note}{span}\n{len(c['items'])} listings · {seller_url(c['user_id'])}")


def push_locks(rows):
    for it in rows:
        price = (it.get("price") or {}).get("amount") or 0
        d = it.get("_dist_km")
        where = f" · {d} km" if d is not None else ""
        cap = (f"*LOCK TRIPWIRE — {it['_lock_term']}*\n"
               f"{it.get('title', '')[:70]} — €{price:.0f}{where}\n"
               f"{W.item_url(it)}\n"
               f"_the owner's wheels + seatpost are on coded skewers. "
               f"This word is close to unique on Wallapop._")
        W.send_photo(W.first_image(it), cap)
        with open(LOCK_HITS_FILE, "a") as f:
            f.write(json.dumps(dict(it, _pushed=datetime.now(timezone.utc)
                                    .isoformat(timespec="seconds")),
                               ensure_ascii=False) + "\n")
        time.sleep(1.5)


def digest(conf, pending, prof):
    if not pending:
        log("digest: nothing banked")
        return
    window = load_archive(days=conf.get("cluster_window_days", 45))
    cl = clusters(window, conf)
    pending_ids = {i.get("id") for i in pending}
    clustered_ids = set()

    header = (f"*Accessory sweep* — {len(pending)} new part"
              f"{'s' if len(pending) != 1 else ''} within "
              f"{conf.get('radius_km', 40)} km since the theft")
    W.send_telegram(header)

    # Sellers first: the digest is a seller list, not an item list.
    shown = 0
    for c in cl:
        if not any(i.get("id") in pending_ids for i in c["items"]):
            continue          # nothing new from this seller since last digest
        W.send_telegram(cluster_caption(c))
        W.send_album([(0, i, []) for i in c["items"][:10]], prof)
        clustered_ids.update(i.get("id") for i in c["items"])
        shown += 1
        time.sleep(2.0)
    if shown:
        log(f"digest: {shown} seller clusters")

    # Everything else as one compact list — cheap to skim, no photo budget.
    singles = [i for i in pending if i.get("id") not in clustered_ids]
    if singles:
        lines = []
        for i in sorted(singles, key=lambda x: x.get("_category") or ""):
            price = (i.get("price") or {}).get("amount") or 0
            lines.append(f"· [{i.get('_category')}] {i.get('title','')[:46]} "
                         f"— €{price:.0f} · {i.get('_dist_km')}km\n  {W.item_url(i)}")
        for chunk in range(0, len(lines), 20):
            W.send_telegram("\n".join(lines[chunk:chunk + 20]))
            time.sleep(1.5)
    log(f"digest: {len(singles)} singletons listed")


def report(conf):
    window = load_archive(days=conf.get("cluster_window_days", 45))
    pending = W.load_json(PENDING_FILE, [])
    cl = clusters(window, conf)
    print(f"{LOG} archive window: {len(window)} listings, {len(pending)} banked")
    cats = defaultdict(int)
    for r in window:
        cats[r.get("_category")] += 1
    print("  by category: " + ", ".join(f"{k} {v}" for k, v in sorted(cats.items())))
    print(f"  seller clusters (>= {conf.get('cluster_min_categories', 2)} categories): {len(cl)}")
    for c in cl[:15]:
        flag = (f" [kit:{'+'.join(c['signature'])}]" if c["signature"] else "")
        flag += " GRINDER" if c["has_locked"] else ""
        print(f"   {c['user_id']} {'+'.join(c['categories'])}{flag} "
              f"({len(c['items'])} listings) {seller_url(c['user_id'])}")
        for i in c["items"][:5]:
            price = (i.get("price") or {}).get("amount") or 0
            print(f"      {i.get('_category'):7s} €{price:<6.0f} {i.get('_dist_km')}km  "
                  f"{i.get('title','')[:52]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="one pass, no Telegram, no state")
    ap.add_argument("--scan", action="store_true", help="bank new kit, push lock tripwires")
    ap.add_argument("--digest", action="store_true", help="flush the bank, clustered by seller")
    ap.add_argument("--seed", action="store_true",
                    help="mark everything currently listed as seen, bank nothing")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    conf = load_conf()
    if not conf:
        print(f"{LOG} data/accessories.json missing", file=sys.stderr)
        return 1
    prof = W.load_profile()

    if args.report:
        report(conf)
        return 0

    seen = set(W.load_json(SEEN_FILE, []))
    pending = W.load_json(PENDING_FILE, [])

    if args.seed:
        kit = scan_kit(conf, seen)
        scan_locks(conf, seen)
        W.save_json(SEEN_FILE, sorted(seen))
        log(f"seeded: {len(seen)} ids marked seen, {len(kit)} banked nothing")
        return 0

    if args.scan or args.test:
        before = len(seen)
        kit = scan_kit(conf, seen, test=args.test)
        locks = scan_locks(conf, seen, test=args.test)
        if args.test:
            for it in kit:
                print(f"  [{it['_category']:7s}] €{(it.get('price') or {}).get('amount', 0):<6.0f} "
                      f"{it['_dist_km']}km  {it.get('title', '')[:56]}")
            for it in locks:
                print(f"  [LOCK {it['_lock_term']}] {it.get('title', '')[:60]}")
            cl = clusters(load_archive(conf.get("cluster_window_days", 45)) + kit, conf)
            print(f"{LOG} would bank {len(kit)}, push {len(locks)} lock hits, "
                  f"{len(cl)} seller clusters in window ({len(seen) - before} newly seen)")
            return 0
        if locks:
            push_locks(locks)
        if kit:
            archive(kit)
            pending.extend(kit)
            W.save_json(PENDING_FILE, pending)
        W.save_json(SEEN_FILE, sorted(seen))

    if args.digest:
        digest(conf, pending, prof)
        W.save_json(PENDING_FILE, [])
        W.save_json(SEEN_FILE, sorted(seen))
    return 0


if __name__ == "__main__":
    sys.exit(main())
