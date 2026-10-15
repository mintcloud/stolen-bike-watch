#!/bin/bash
# Describe the stolen bike. Two forms:
#
#   ./set-bike.sh "Orbea Gain M20 gravel negra talla M 2023" 1800
#   ./set-bike.sh --edit          (open data/bike.json directly)
#
# The free-text form guesses brand = first unrecognised word, model = the next
# one or two, and pulls out type / colour / size / year where it knows them.
# Check what it prints; fix data/bike.json by hand if it guessed wrong.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
export SCRIPT_DIR
PY="$SCRIPT_DIR/.venv/bin/python3"
[ -x "$PY" ] || PY="$(command -v python3)"
mkdir -p "$SCRIPT_DIR/data"

if [ "${1:-}" = "--edit" ]; then
  "${EDITOR:-nano}" "$SCRIPT_DIR/data/bike.json"; exit 0
fi
if [ $# -lt 1 ]; then sed -n '2,9p' "$0"; exit 1; fi

DESC="$1" PAID="${2:-0}" "$PY" - <<'PYEOF'
import json, os, re, unicodedata

DATA = os.path.join(os.environ["SCRIPT_DIR"], "data")
f = os.path.join(DATA, "bike.json")
desc = os.environ["DESC"]
paid = float(os.environ.get("PAID") or 0)

def norm(s):
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c))

TYPES = {"gravel": "gravel", "mtb": "mtb", "montana": "mtb", "btt": "mtb",
         "carretera": "carretera", "road": "carretera", "urbana": "urbana",
         "city": "urbana", "plegable": "plegable", "electrica": "electrica",
         "ebike": "electrica", "e-bike": "electrica", "fixie": "fixie",
         "hibrida": "hibrida", "cyclocross": "cyclocross"}
COLOURS = {"negra": "negra", "negro": "negra", "blanca": "blanca", "blanco": "blanca",
           "azul": "azul", "roja": "roja", "rojo": "roja", "verde": "verde",
           "gris": "gris", "amarilla": "amarilla", "naranja": "naranja",
           "black": "negra", "white": "blanca", "blue": "azul", "red": "roja"}

words = desc.split()
n = [norm(w) for w in words]
typ = next((TYPES[w] for w in n if w in TYPES), "")
col = next((COLOURS[w] for w in n if w in COLOURS), "")
year = next((w for w in n if re.fullmatch(r"20\d\d", w)), "")
m = re.search(r"\btalla\s+(\w+)", norm(desc))
size = m.group(1) if m else ""
consumed = set(TYPES) | set(COLOURS) | {"talla", "bicicleta", "bici", size, year}
core = [w for w, nw in zip(words, n) if nw not in consumed]

old = json.load(open(f)) if os.path.exists(f) else {}
prof = {
    "brand": core[0] if core else "",
    "model": " ".join(core[1:3]) if len(core) > 1 else "",
    "type": typ, "colour": col, "size": size, "year": year,
    "extra_keywords": [w for w in core[3:] if len(w) > 3],
    "paid_eur": paid or old.get("paid_eur", 0),
    "stolen_on": old.get("stolen_on", "2026-09-05"),
    "centre": old.get("centre", {"lat": 41.3874, "lon": 2.1686,
                                 "label": "city centre"}),
    "hit_radius_km": old.get("hit_radius_km", 150),
    "sweep_radius_km": old.get("sweep_radius_km", 25),
    "sweep_near_km": old.get("sweep_near_km", 0),
    # carried forward, never guessed from free text: these were hand-tuned
    "brand_aliases": old.get("brand_aliases", []),
    "model_aliases": old.get("model_aliases", []),
    "genre_queries": old.get("genre_queries", []),
    "sweep_min_price": old.get("sweep_min_price", 80),
    "sweep_max_price": old.get("sweep_max_price", 2500),
    "sweep_max_items": old.get("sweep_max_items", 25),
}
json.dump(prof, open(f, "w"), ensure_ascii=False, indent=1)
print(json.dumps(prof, ensure_ascii=False, indent=1))
print(f"\nwritten to {f}")
print("check brand/model above, then:  ./run.sh --test")
PYEOF
