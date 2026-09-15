"""Utility Rating: a Quantity/Quality/Combined utility score per player per
match. Sources entirely from the already-parsed
match JSON (processed_matches/<match>.json) - no 2nd-pass extractor, no
re-parse.

Reuses analysis/baselines.py's rank-banded BaselineStore for Quality's
population z-scores - the same baselines.json Overwatch writes to, just new
feature keys. This is intentional: Overwatch's statistical infrastructure is
a shared suite (anti-cheat + utility/playstyle effectiveness), not something
Utility Rating needs to keep independent from.

v1 simplifications, documented rather than silently baked in (see plan §4):
- No sqrt(matchesCount) inflation - this is a single-match snapshot, not a
  longitudinal rolling average (that's stage-4 territory).
- Equal weighting across the 6 Quality sub-stats. The published methodology
  this follows does not give its own weights, so inventing a ranking between
  them would be a guess dressed as a formula.
"""

import json
import math
import os
import statistics

import jsonio  # root module (like paths.py) - .br-transparent JSON reads

from .baselines import BaselineStore, band_for_rank

# (feature key, direction "high"|"low" = good direction)
QUALITY_FEATURES = [
    ("flash_assist_pct", "high"),
    ("enemies_flashed_per_flash", "high"),
    ("friends_flashed_per_flash", "low"),
    ("avg_blind_time_per_flash", "high"),
    ("he_dmg_per_he", "high"),
    ("he_team_dmg_per_he", "low"),
]

NADE_ARRAYS = ("he", "flashes", "smokes", "infernos")

TICKRATE = 64  # matches match.html's flash-assist window constant


def _round_lookup(rounds):
    """tick -> round_num, from each round's [start, end] tick bounds. Needed
    because GrenadeDetonate/SmokeEvent (he/flashes/smokes/infernos) carry
    only `tick`, unlike Kill/Damage which the parser already backfills with
    round_num."""
    bounds = []
    for r in rounds:
        start = r.get("start") or 0
        end = r.get("end")
        end = end if end is not None else float("inf")
        bounds.append((start, end, r.get("round_num") or 0))
    bounds.sort()

    def round_of(tick):
        for start, end, rn in bounds:
            if start <= tick <= end:
                return rn
        return None

    return round_of


def _is_enemy(a_side, v_side):
    a = (a_side or "").lower()
    v = (v_side or "").lower()
    return (a == "ct" and v in ("t", "terrorist")) or (a in ("t", "terrorist") and v == "ct")


def _is_teammate(a_name, v_name, a_side, v_side):
    if a_name == v_name:
        return False
    a = (a_side or "").lower()
    v = (v_side or "").lower()
    return (a == "ct" and v == "ct") or (a in ("t", "terrorist") and v in ("t", "terrorist"))


def compute_quantity(data):
    """-> {name: quantity_rating (0-100)}. Doc §3, fully spec'd, no baseline."""
    rounds = data.get("rounds") or []
    num_rounds = len(rounds)
    counts = {}
    for arr_name in NADE_ARRAYS:
        for ev in data.get(arr_name) or []:
            name = ev.get("thrower_name")
            if name:
                counts[name] = counts.get(name, 0) + 1
    out = {}
    for name, count in counts.items():
        avg = count / num_rounds if num_rounds else 0.0
        ratio = min(avg / 3.0, 1.0)
        out[name] = round((ratio ** (2.0 / 3.0)) * 100, 1)
    return out


def compute_quality_inputs(data):
    """-> {name: {feature_key: value}}, one player-row per thrower/flasher
    seen this match. Missing/zero-denominator features are simply absent
    from a player's dict (never coerced to 0)."""
    flashes_thrown = {}
    he_thrown = {}
    for ev in data.get("flashes") or []:
        name = ev.get("thrower_name")
        if name:
            flashes_thrown[name] = flashes_thrown.get(name, 0) + 1
    for ev in data.get("he") or []:
        name = ev.get("thrower_name")
        if name:
            he_thrown[name] = he_thrown.get(name, 0) + 1

    flash_assists = {}
    enemies_flashed = {}
    friends_flashed = {}
    enemy_blind_time = {}
    kills = data.get("kills") or []

    for b in data.get("blind") or []:
        a_name = b.get("attacker_name")
        if not a_name:
            continue
        v_name = b.get("victim_name")
        a_side = b.get("attacker_side")
        v_side = b.get("victim_side") or b.get("user_side")
        dur = b.get("blind_duration") or 0.0

        if _is_enemy(a_side, v_side):
            enemies_flashed[a_name] = enemies_flashed.get(a_name, 0) + 1
            enemy_blind_time[a_name] = enemy_blind_time.get(a_name, 0.0) + dur
        elif _is_teammate(a_name, v_name, a_side, v_side):
            friends_flashed[a_name] = friends_flashed.get(a_name, 0) + 1

        # Flash-assist: same window/logic as match.html's flashAssists pass -
        # a kill on the blinded victim, by someone other than the flasher,
        # within a duration-scaled tick window.
        if dur >= 0.5:
            window = round(dur * TICKRATE) + 32
            killed = next((k for k in kills
                           if k.get("victim_name") == v_name
                           and abs((k.get("tick") or 0) - (b.get("tick") or 0)) <= window),
                          None)
            if killed and killed.get("attacker_name") != a_name:
                flash_assists[a_name] = flash_assists.get(a_name, 0) + 1

    he_enemy_dmg = {}
    he_team_dmg = {}
    for d in data.get("damage") or []:
        a_name = d.get("attacker_name")
        v_name = d.get("victim_name")
        if not a_name or a_name == v_name:
            continue
        wep = (d.get("weapon") or "").replace("weapon_", "").lower()
        if wep != "hegrenade":
            continue
        dmg = d.get("dmg_health") or 0
        a_side = d.get("attacker_side")
        v_side = d.get("victim_side")
        if _is_enemy(a_side, v_side):
            he_enemy_dmg[a_name] = he_enemy_dmg.get(a_name, 0) + dmg
        elif _is_teammate(a_name, v_name, a_side, v_side):
            he_team_dmg[a_name] = he_team_dmg.get(a_name, 0) + dmg

    names = set(flashes_thrown) | set(he_thrown)
    out = {}
    for name in names:
        feats = {}
        nf = flashes_thrown.get(name, 0)
        nh = he_thrown.get(name, 0)
        if nf:
            feats["flash_assist_pct"] = 100.0 * flash_assists.get(name, 0) / nf
            feats["enemies_flashed_per_flash"] = enemies_flashed.get(name, 0) / nf
            feats["friends_flashed_per_flash"] = friends_flashed.get(name, 0) / nf
            feats["avg_blind_time_per_flash"] = enemy_blind_time.get(name, 0.0) / nf
        if nh:
            feats["he_dmg_per_he"] = he_enemy_dmg.get(name, 0) / nh
            feats["he_team_dmg_per_he"] = he_team_dmg.get(name, 0) / nh
        if feats:
            out[name] = feats
    return out


def _label_sets(labels):
    labels = labels or {}
    cheater_ids = {sid for sid, info in labels.items() if info.get("label") == "cheater"}
    pro_ids = {sid for sid, info in labels.items() if info.get("label") == "professional"}
    suspicious_ids = {sid for sid, info in labels.items() if info.get("label") == "suspicious"}
    return cheater_ids, pro_ids, suspicious_ids


def analyze(match_id, processed_dir, data_dir, force=False, labels=None, is_pro_match=False):
    """match_id without .json suffix. Raises FileNotFoundError when the
    processed match JSON is missing (mirrors analysis/overwatch.py:analyze)."""
    proc_path = os.path.join(processed_dir, f"{match_id}.json")
    cache_path = os.path.join(processed_dir, f"{match_id}.utility.json")
    src_file = jsonio.resolve(proc_path)
    if src_file is None:
        raise FileNotFoundError(proc_path)

    if not force and os.path.exists(cache_path) \
            and os.path.getmtime(cache_path) >= os.path.getmtime(src_file):
        with open(cache_path, encoding="utf-8") as f:
            return json.load(f)

    data = jsonio.load(proc_path)

    quantity = compute_quantity(data)
    quality_inputs = compute_quality_inputs(data)

    ranks = {}
    steam_ids = {}
    for r in data.get("ranks") or []:
        name = r.get("name")
        if name:
            ranks[name] = r
            steam_ids[name] = r.get("steam_id")

    cheater_ids, pro_ids, suspicious_ids = _label_sets(labels)

    names = set(quantity) | set(quality_inputs)
    bands = {}
    for name in names:
        r = ranks.get(name, {})
        bands[name] = band_for_rank(r.get("rank_type", 0), r.get("rank", 0))

    store = BaselineStore(os.path.join(data_dir, "baselines.json"))

    if is_pro_match:
        clean_rows = []
    else:
        clean_rows = [
            (steam_ids.get(name) or name, bands[name], feats)
            for name, feats in quality_inputs.items()
            if (steam_ids.get(name) or name) not in cheater_ids
            and (steam_ids.get(name) or name) not in pro_ids
            and (steam_ids.get(name) or name) not in suspicious_ids
        ]
    store.update_match(match_id, clean_rows)

    players_out = []
    for name in sorted(names):
        band = bands[name]
        feats = quality_inputs.get(name, {})
        z_parts = []
        for key, direction in QUALITY_FEATURES:
            val = feats.get(key)
            if val is None:
                continue
            mean, std, n = store.feature_stats(key, band)
            if mean is None or not std:
                continue
            z = (val - mean) / std if direction == "high" else (mean - val) / std
            z_parts.append(z)

        quality = None
        if z_parts:
            z_combined = sum(z_parts) / math.sqrt(len(z_parts))
            quality = round(statistics.NormalDist().cdf(z_combined) * 100, 1)

        q = quantity.get(name, 0.0)
        combined = round(math.sqrt(q * quality), 1) if quality is not None else None

        players_out.append({
            "name": name,
            "steam_id": steam_ids.get(name),
            "quantity": q,
            "quality": quality,
            "combined": combined,
        })

    result = {"ok": True, "match": match_id, "players": players_out}

    os.makedirs(processed_dir, exist_ok=True)
    tmp = cache_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(result, f)
    os.replace(tmp, cache_path)
    return result
