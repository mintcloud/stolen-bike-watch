#!/usr/bin/env python3
"""Re-send archived sweep listings through the fixed bytes-upload path.

Every photo in the Bike topic sent before 2026-09-16 was fetched by Telegram
from the marketplace URL, which produces a photo record Telegram's own CDN then
refuses to serve to the desktop client -- it stalls at 0% forever. Those records
are permanently poisoned; the only way to see those listings on desktop is to
send the images again as uploaded bytes.

Spot-checked 2026-09-16: 80/80 sampled archived images from 6-15 Sept still
fetch at HTTP 200 from this box, so the whole backlog is recoverable.

  ./recover-backlog.py --top 40              best 40 by visual score, dry run
  ./recover-backlog.py --top 40 --send       actually send them
  ./recover-backlog.py --since 2026-09-14 --send
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from wallapop_monitor import DATA, first_image, send_batch, send_telegram  # noqa: E402

SWEEP = os.path.join(DATA, "sweep-archive.jsonl")
MIL = os.path.join(DATA, "mil-archive.jsonl")


def load(path, wallapop=True):
    out = []
    if not os.path.exists(path):
        return out
    for line in open(path):
        r = json.loads(line)
        u = first_image(r) if wallapop else (r["images"][0] if r.get("images") else None)
        if not u:
            continue
        out.append({
            "url": u,
            "archived": r.get("_archived", ""),
            "visual": r.get("_visual") if r.get("_visual") is not None else -1,
            "price": (r.get("price") or {}).get("amount", 0) if wallapop else r.get("price", 0),
            "title": r.get("title", "")[:60],
            "link": (f"https://es.wallapop.com/item/{r['web_slug']}" if wallapop and r.get("web_slug")
                     else r.get("url", "")),
            "src": "Wallapop" if wallapop else "Milanuncios",
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=40,
                    help="how many, ranked by visual score (default 40)")
    ap.add_argument("--since", default="", help="only listings archived on/after YYYY-MM-DD")
    ap.add_argument("--send", action="store_true", help="actually send; default is a dry run")
    a = ap.parse_args()

    rows = load(SWEEP, True) + load(MIL, False)
    if a.since:
        rows = [r for r in rows if r["archived"][:10] >= a.since]
    # Highest visual score first -- these are the ones that most look like the bike.
    rows.sort(key=lambda r: (-r["visual"], r["archived"]))
    rows = rows[:a.top]

    print(f"{len(rows)} listings, visual score {rows[-1]['visual']}-{rows[0]['visual']}, "
          f"archived {min(r['archived'][:10] for r in rows)} to {max(r['archived'][:10] for r in rows)}")
    if not a.send:
        for r in rows[:10]:
            print(f"  [{r['visual']:>3}] EUR{r['price']:.0f} {r['src']:<11} {r['title']}")
        print("dry run -- pass --send to push these to the Bike topic")
        return

    send_telegram(f"*Backlog recovery* — the {len(rows)} best-matching sweep listings since "
                  f"{min(r['archived'][:10] for r in rows)}, re-sent as uploaded bytes so they "
                  f"actually render on desktop. Ranked by photo score, highest first.")
    batch, sent = [], 0
    for r in rows:
        batch.append({"type": "photo", "media": r["url"],
                      "caption": f"€{r['price']:.0f} · photo {r['visual']} · {r['src']}\n"
                                 f"{r['title']}\n{r['link']}"})
        if len(batch) == 10:
            sent += send_batch(batch)
            batch = []
    if batch:
        sent += send_batch(batch)
    print(f"recovered: {sent} photos sent")


if __name__ == "__main__":
    main()
