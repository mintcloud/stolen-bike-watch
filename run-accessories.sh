#!/bin/bash
# accessories.py — the stripped-bike channel. Sibling of run.sh.
#
#   ./run-accessories.sh --test      one pass, print only, no Telegram
#   ./run-accessories.sh --scan      bank kit + push lock tripwires (cron, hourly)
#   ./run-accessories.sh --digest    seller-clustered digest (cron, daily)
#   ./run-accessories.sh --report    what is banked and which sellers cluster
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$SCRIPT_DIR/.venv/bin/python3"
[ -x "$PY" ] || PY="$(command -v python3)"

if [ -f "$SCRIPT_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$SCRIPT_DIR/.env"
  set +a
fi
mkdir -p "$SCRIPT_DIR/data"

# Its own lock: this must not contend with the 20-min bike sweep.
#
# -w, not -n. The scan is hourly at :35 and the digest was originally also at
# :35, so every day at 09:35 the two collided on this lock and `flock -n` made
# the digest exit 1 SILENTLY — 174 listings sat banked and nothing was pushed.
# The digest now runs at :40, and waiting rather than bailing means a future
# overlap costs a delay instead of a missed send. A real timeout is logged.
if ! flock -w 300 "$SCRIPT_DIR/data/.lock-accessories" \
     "$PY" "$SCRIPT_DIR/accessories.py" "$@"; then
  rc=$?
  echo "[accessories] run failed or lock timed out after 300s (rc=$rc): $*" >&2
  exit $rc
fi
