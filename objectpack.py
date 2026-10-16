#!/usr/bin/env python3
"""The object pack — everything in this watcher that is about *bicycles*
rather than about *finding a stolen thing*.

The pipeline is the same whatever was taken: pull local listings, filter to
whole objects of the right class, score the photo on a few cheap attributes,
pay a vision model only for what survives. What changes between a bicycle, a
motorbike and a cello is the vocabulary, the marketplace category, the CLIP
attribute prompts and the description handed to the vision model. All of that
lives in `profiles/<object>.json` and nowhere else.

    data/bike.json  "object": "motorbike"   -> profiles/motorbike.json

`profiles/bicycle.json` reproduces the constants this repo ran on for its
first six weeks, exactly; `tests/test_pack_parity.py` asserts that.

Per-user overrides go in the item profile under `object_overrides`, deep-merged
over the pack, so you can add two queries or retune one arm without forking a
whole file:

    "object_overrides": {"sweep_queries": [["bicicleta gravel", 2]]}
"""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILES = os.path.join(HERE, "profiles")

_cache = {}


def _merge(base, over):
    """Deep merge, dict by dict. Lists replace -- a user who overrides
    `sweep_queries` means *these* queries, not these plus the defaults."""
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def read_json(path, default=None):
    """Small helper so a caller can load its item profile before the rest of
    its own module is defined."""
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {} if default is None else default


def available():
    if not os.path.isdir(PROFILES):
        return []
    return sorted(f[:-5] for f in os.listdir(PROFILES) if f.endswith(".json"))


def load(prof=None):
    """-> the pack for this item profile. Defaults to bicycle, because that is
    what every threshold in this repo was measured on."""
    prof = prof or {}
    name = (prof.get("object") or "bicycle").strip().lower()
    path = os.path.join(PROFILES, f"{name}.json")
    if not os.path.exists(path):
        raise SystemExit(
            f"[objectpack] no profiles/{name}.json. Available: "
            f"{', '.join(available()) or '(none)'}. Copy the closest one and "
            f"edit it -- see AGENTS.md.")
    if path not in _cache:
        with open(path) as f:
            _cache[path] = json.load(f)
    return _merge(_cache[path], prof.get("object_overrides"))


# ------------------------------------------------------------------ accessors
# Everything below exists so the callers never dig into the JSON shape.

def arms(pack):
    """-> {name: spec}. A spec is {"type": "clip", positives, negatives} or
    {"type": "colour", hue, sat_min, val}."""
    return pack.get("arms", {})


def clip_groups(pack):
    """-> {name: (positives, negatives)} for the CLIP arms only."""
    return {k: (v["positives"], v["negatives"])
            for k, v in arms(pack).items() if v.get("type", "clip") == "clip"}


def colour_arms(pack):
    return {k: v for k, v in arms(pack).items() if v.get("type") == "colour"}


def gate_arms(pack):
    """Arms the pre-gate may use. Anything else scores and ranks but never
    decides whether a photo is paid for."""
    return list(pack.get("gate_arms") or arms(pack))


def gate(pack, prof, ring="near"):
    """-> {arm: threshold} for this ring.

    A `vlm_pregate_<arm>` / `vlm_pregate_far_<arm>` key in the item profile
    beats the pack, because that is where the first six weeks of tuning was
    written and silently overriding it would be a recall regression.
    """
    key = "gate_far" if ring == "far" else "gate_near"
    out = dict(pack.get(key, {}))
    prefix = "vlm_pregate_far_" if ring == "far" else "vlm_pregate_"
    for arm in list(out) + list(arms(pack)):
        v = prof.get(prefix + arm)
        if v is not None:
            out[arm] = v
    return out


def words(pack, kind):
    return tuple(pack.get("words", {}).get(kind, ()))


def category_ids(pack):
    return set(pack.get("category_ids", ()))


def sweep_queries(pack):
    return tuple((q, int(n)) for q, n in pack.get("sweep_queries", ()))


def noun(pack, key="singular"):
    return pack.get("noun", {}).get(key, "object")
