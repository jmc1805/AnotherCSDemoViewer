"""analysis/mapzones.py - Valve's own named callout regions (env_cs_place
entities), lazily loaded per canonical map name and cached in memory.

Two extractors produce this data - identical output, same underlying math
(measured from the game's own per-map VPK, never hand-traced - the same
"measured, never eyeballed" convention as radar calibration and the
Nuke/Vertigo floor splits): tools/extract_map_zones.mjs (Node, a developer's
tool) and mapzone_extract.py (in-process, driven from the Settings page - the
only route that works in the installed desktop app, which ships no Node).
This module only loads and queries the result.

Two places a map's zones can live, DATA_DIR winning when both exist:

  ANALYSIS_DATA_DIR/map_zones/<map>.json   gitignored, an install's own
                                            extraction - fresher, e.g. after a
                                            map update changed a callout, or
                                            for a map added to MAP_LIBRARY
                                            after this build shipped
  <repo>/map_zones/<map>.json              committed, ships with the app -
                                            small measured numbers (box
                                            coordinates + callout names), not
                                            Valve art/textures, so this is the
                                            same "committed transcription"
                                            precedent as MAP_LIBRARY's radar
                                            calibration, not the gitignored
                                            static/assets/ treatment. What
                                            makes an installed build have zone
                                            data with no extraction step.

A map covered by neither degrades to "no opinion" (None) rather than raising -
every lookup here must stay total, since an uncalibrated or freshly-added map
is a normal, expected state, not an error.
"""
import json
import os

import maps
import paths

_CACHE = {}  # canonical map name -> list[dict] | None (tried once either way)


def _zones_path(canon):
    return os.path.join(paths.ANALYSIS_DATA_DIR, 'map_zones', '%s.json' % canon)


def _seed_path(canon):
    return os.path.join(paths.MAP_ZONES_SEED_DIR, '%s.json' % canon)


def _load(canon):
    if canon in _CACHE:
        return _CACHE[canon]
    zones = None
    for p in (_zones_path(canon), _seed_path(canon)):
        if not os.path.isfile(p):
            continue
        try:
            with open(p, 'r', encoding='utf-8') as f:
                zones = json.load(f)
            break
        except (OSError, ValueError):
            continue
    _CACHE[canon] = zones
    return zones


def has_zones(map_name):
    """The `has_zones` cap: whether this map's callout regions have actually
    been extracted. A map with no zone file must hide the Zone filter, not
    show one that always answers 'unknown'."""
    return bool(_load(maps.canonical(map_name)))


def zone_of(map_name, x, y, z=None):
    """The smallest Valve-named callout region containing (x, y[, z]), or
    None when the map has no zone data or the point falls outside every box.

    Boxes are exact axis-aligned volumes - env_cs_place entities are authored
    as literal box brushes - so the smallest CONTAINING box is the correct
    most-specific answer for any deliberately nested/overlapping regions,
    not a heuristic.

    `z` is optional: a moment with no reliable Z (an execute's centroid, an
    old parse) still gets a 2D-only match rather than no zone at all - it
    just can't disambiguate two floors stacked in the same X/Y footprint on
    Nuke/Vertigo, which is exactly the case Z exists to resolve when it IS
    available. A linear scan is fine here: 15-60 boxes per map, once per
    moment at index-build time, not per query.
    """
    zones = _load(maps.canonical(map_name))
    if not zones or x is None or y is None:
        return None
    best_name, best_vol = None, None
    for zone in zones:
        mn, mx = zone.get('mins'), zone.get('maxs')
        if not mn or not mx:
            continue
        if not (mn[0] <= x <= mx[0] and mn[1] <= y <= mx[1]):
            continue
        if z is not None and not (mn[2] <= z <= mx[2]):
            continue
        vol = (mx[0] - mn[0]) * (mx[1] - mn[1]) * (mx[2] - mn[2])
        if best_vol is None or vol < best_vol:
            best_name, best_vol = zone.get('name'), vol
    return best_name
