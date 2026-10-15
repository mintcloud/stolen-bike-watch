#!/bin/bash
# wallapop-monitor — runner. 
#
# Usage:
#   ./run.sh --test            one pass, print only, no Telegram, no state written
#   ./run.sh --send            HITS pass  (cron: every 30 min)
#   ./run.sh --send --digest   HITS + SWEEP digest (cron: daily 09:15 Madrid)
#   ./run.sh --seed            mark everything currently listed as seen
#   ./run.sh --report          what has been flagged so far
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

# One at a time: this box has no swap and stacked runs waste the API budget.
exec flock -n "$SCRIPT_DIR/data/.lock" \
  "$PY" "$SCRIPT_DIR/wallapop_monitor.py" "$@"
