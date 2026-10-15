#!/bin/bash
# milanuncios channel of the stolen-bike watch. Sibling of run.sh.
#
#   ./run-milanuncios.sh --test              one pass, print only
#   ./run-milanuncios.sh --send              scan: push hits, bank sweep finds
#   ./run-milanuncios.sh --digest            flush the bank as a photo album
#   ./run-milanuncios.sh --report            what has been flagged so far
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

# Its own lock: this must not queue behind the Wallapop pass.
exec flock -n "$SCRIPT_DIR/data/.mil.lock" \
  "$PY" "$SCRIPT_DIR/milanuncios.py" "$@"
