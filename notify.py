#!/usr/bin/env python3
"""Where a push goes.

`NOTIFY_BACKEND` picks the sink:

    telegram  (default)  upload photo bytes to a chat    needs TELEGRAM_TOKEN + TELEGRAM_USER_ID
    stdout               print the push, fetch nothing   needs nothing
    webhook              POST one JSON object per push    needs NOTIFY_WEBHOOK_URL

The Telegram path is the one with mileage on it (6 digests/day since
2026-09-05) and is left exactly as it was. `stdout` is what you want for
`--test`, for cron-to-email, or to see what the thing would say before you
give it a bot token.
"""

import json
import os
import sys

BACKEND = os.environ.get("NOTIFY_BACKEND", "telegram").strip().lower()


def emit(kind, text="", photos=()):
    """Non-telegram sinks. Returns a truthy/int result in the shape the
    caller expects: send_telegram/send_photo want a bool, send_batch wants
    the number of photos that landed."""
    photos = [p for p in photos if p]
    if BACKEND == "webhook":
        return _webhook(kind, text, photos)
    return _stdout(kind, text, photos)


def _stdout(kind, text, photos):
    if text:
        print(f"\n--- {kind} ---\n{text}", flush=True)
    for p in photos:
        print(f"    photo: {p}", flush=True)
    return len(photos) if kind == "album" else True


def _webhook(kind, text, photos):
    url = os.environ.get("NOTIFY_WEBHOOK_URL")
    if not url:
        print("[notify] NOTIFY_WEBHOOK_URL missing", file=sys.stderr)
        return 0 if kind == "album" else False
    import requests
    try:
        r = requests.post(url, json={"kind": kind, "text": text,
                                     "photos": photos}, timeout=30)
        ok = r.ok
    except requests.RequestException as e:
        print(f"[notify] webhook failed: {e}", file=sys.stderr)
        ok = False
    if not ok:
        return 0 if kind == "album" else False
    return len(photos) if kind == "album" else True
