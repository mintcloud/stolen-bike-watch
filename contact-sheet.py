#!/usr/bin/env python3
"""Render every swept listing as one scrollable dark contact sheet.

Telegram is a bad surface for 250 photos — you scroll past them once and they
are gone up the chat. This reads data/sweep-archive.jsonl + data/pending.json
(the same rows the digest sends) and writes a single HTML page: a photo grid,
newest first, price/distance/age under each, click through to the listing.

  ./contact-sheet.py [out.html]
"""
import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")


def rows():
    out = {}
    arch = os.path.join(DATA, "sweep-archive.jsonl")
    if os.path.exists(arch):
        for line in open(arch):
            line = line.strip()
            if line:
                try:
                    r = json.loads(line)
                    out[r["id"]] = r
                except Exception:
                    pass
    try:
        for r in json.load(open(os.path.join(DATA, "pending.json"))):
            out[r["id"]] = r
    except Exception:
        pass
    return list(out.values())


def img(r):
    imgs = r.get("images") or []
    if not imgs:
        return ""
    u = imgs[0].get("urls") or {}
    return u.get("big") or u.get("medium") or u.get("small") or ""


def age(r):
    c = r.get("created_at")
    if not c:
        return ""
    d = datetime.now(timezone.utc) - datetime.fromtimestamp(c / 1000, timezone.utc)
    h = d.total_seconds() / 3600
    return f"{h:.0f}h ago" if h < 48 else f"{d.days}d ago"


def main():
    prof = json.load(open(os.path.join(DATA, "bike.json")))
    items = [r for r in rows() if img(r)]
    items.sort(key=lambda r: -(r.get("created_at") or 0))

    cards = []
    for r in items:
        price = (r.get("price") or {})
        amt = price.get("amount") if isinstance(price, dict) else price
        loc = r.get("location") or {}
        url = "https://es.wallapop.com/item/" + (r.get("web_slug") or r.get("id", ""))
        title = (r.get("title") or "").replace("<", "&lt;")
        cards.append(
            f'<a class="c" href="{url}" target="_blank" rel="noopener">'
            f'<img loading="lazy" src="{img(r)}" alt="">'
            f'<div class="t">{title}</div>'
            f'<div class="m"><b>€{amt or 0:.0f}</b> · {loc.get("city") or "?"}'
            f' · {age(r)}</div></a>')

    html = f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Wallapop sweep — {len(items)} bikes</title><style>
:root{{--bg:#1a1a2e;--fg:#e0e0e0;--blue:#64b5f6;--dim:#9aa0b5;--card:#22223d;--line:#33334f}}
*{{box-sizing:border-box}}
body{{background:var(--bg);color:var(--fg);margin:0;padding:24px 16px 80px;
 font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Helvetica,sans-serif}}
header{{max-width:1400px;margin:0 auto 24px}}
h1{{margin:0 0 .2em;font-size:1.5rem;color:#fff;letter-spacing:-.02em}}
.sub{{color:var(--dim);font-size:.92rem}}
.sub b{{color:var(--blue)}}
.grid{{max-width:1400px;margin:0 auto;display:grid;gap:14px;
 grid-template-columns:repeat(auto-fill,minmax(190px,1fr))}}
.c{{background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden;
 text-decoration:none;color:inherit;display:flex;flex-direction:column;transition:.12s}}
.c:hover{{border-color:var(--blue);transform:translateY(-2px)}}
.c img{{width:100%;aspect-ratio:1;object-fit:cover;background:#111124;display:block}}
.t{{padding:8px 10px 2px;font-size:.85rem;line-height:1.35;color:#eef1f8;
 display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}}
.m{{padding:2px 10px 10px;font-size:.78rem;color:var(--dim);margin-top:auto}}
.m b{{color:var(--blue);font-size:.9rem}}
</style></head><body>
<header><h1>Wallapop sweep — {len(items)} bikes</h1>
<div class="sub">Every listing within <b>{prof['sweep_radius_km']} km</b> of
{prof['centre']['label']}, &euro;{prof['sweep_min_price']}&ndash;{prof['sweep_max_price']},
posted on or after <b>{prof['stolen_on']}</b>. Newest first. Click any card to open
the listing. Looking for a <b>navy <BRAND> <MODEL></b> — steel frame, child seat,
front and rear racks.</div>
<div class="sub" style="margin-top:6px">Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} ·
do not contact a seller, send the URL to the the police.</div></header>
<div class="grid">{''.join(cards)}</div></body></html>"""

    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(DATA, "contact-sheet.html")
    open(out, "w").write(html)
    print(f"{len(items)} listings -> {out}")


if __name__ == "__main__":
    main()
