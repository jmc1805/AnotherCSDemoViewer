"""Tier-1 statistical features per player, computed from overwatch_raw JSON.

Feature groups:
  aim     - aim mechanics: ToT, snap/flick, settle-curve shape, pitch texture
  incon   - performance–skill inconsistency: first-bullet HS, divergence, TTK
  info    - information advantage (soft wallhacks): occlusion tracking, prefires

Every feature declares the direction that counts as suspicious; scoring
against rank-banded baselines happens in baselines.py.
"""

import math
import statistics

from .moments import weapon_class

# Window row column indices - MUST match windowColumns in cmd/overwatch/main.go.
COL_REL = 0
COL_YAW = 1
COL_PITCH = 2
COL_A_SPEED = 6
COL_ANG = 10      # angle crosshair ↔ victim; -1 when victim state unknown/dead
COL_SPOTTED = 11
COL_FIRED = 12

HITGROUP_HEAD = 1
RUN_SPEED = 130.0          # units/s - faster than walking ⇒ "moving"
ENGAGEMENT_GAP_TICKS = 128 # >2 s between damage events ⇒ new engagement
REACTION_JOIN_TICKS = 64   # first sight → first damage within 1 s

# Option 4: weapon class buckets for stratified baselines.
SNIPER_WEAPONS = frozenset({"AWP", "SSG 08", "SCAR-20", "G3SG1"})
RIFLE_WEAPONS  = frozenset({"AK-47", "M4A4", "M4A1-S", "FAMAS",
                             "Galil AR", "SG 553", "AUG"})

# key, label, group, suspicious direction ('high'|'low'), format, description
# (description is plain-language, shown on hover in the Overwatch tab - see
# match.html's ow-feat-label tooltip)
FEATURE_DEFS = [
    ("hs_rate",            "Headshot %",                   "incon", "high", "pct",
     "Share of this player's kills that were headshots. Elevated when a player lands "
     "headshots far more often than same-rank peers."),
    ("first_bullet_hs",    "First-bullet HS %",            "incon", "high", "pct",
     "Headshot rate on just the first bullet of each new engagement - harder to explain "
     "away with spray control alone than overall headshot %."),
    ("hits_moving",        "Hits while moving %",          "incon", "high", "pct",
     "Share of hits landed while the shooter was moving faster than a walk. Human accuracy "
     "usually drops while strafing; elevated values mean it barely does."),
    ("first_sight_div",    "Crosshair divergence (°)",     "incon", "high", "deg",
     "How many degrees off-target the crosshair was the instant an enemy first became "
     "visible. Small values mean the crosshair was already close before it should be "
     "possible to know the enemy was there."),
    ("reaction_ms",        "Sight→damage time (ms)",       "incon", "low",  "ms",
     "Time from first sight of an enemy to landing damage on them. Suspiciously fast values "
     "leave less time than a typical human visual-reaction budget."),
    ("dumb_deaths",        "Deaths w/o damaging killer %", "incon", "high", "pct",
     "Share of deaths where the player never damaged their killer at all that round - "
     "a context feature, not suspicious by itself."),

    ("flick_speed",        "Flick speed p90 (°/tick)",     "aim",   "high", "deg",
     "How fast the crosshair snaps onto the eventual target in the moment before a kill "
     "(90th-percentile angular speed). Very fast, very consistent flicks across many kills "
     "is the classic aim-assist signature."),
    ("flick_sniper",       "Flick speed · sniper (p90)",   "aim",   "high", "deg",
     "Same flick-speed measure, restricted to sniper-rifle kills (scoped-in flicks behave "
     "differently from hipfire)."),
    ("flick_rifle",        "Flick speed · rifle (p90)",    "aim",   "high", "deg",
     "Same flick-speed measure, restricted to rifle kills."),
    ("settle_smooth",      "Settle smoothness",            "aim",   "high", "raw",
     "How consistently the crosshair closes in on the target in one smooth motion before "
     "firing, rather than overshooting and correcting. Unnaturally smooth settling across "
     "many kills is a soft-aimbot signature."),
    ("settle_sniper",      "Settle smoothness · sniper",   "aim",   "high", "raw",
     "Settle smoothness restricted to sniper-rifle kills."),
    ("settle_rifle",       "Settle smoothness · rifle",    "aim",   "high", "raw",
     "Settle smoothness restricted to rifle kills."),
    ("settle_signflips",   "Settle direction changes",     "aim",   "low",  "raw",
     "How many times the crosshair reverses direction while closing in on a target. Very "
     "few reversals across many kills means the aim path is unnaturally direct."),
    ("pitch_corr",         "Pitch step correlation",       "aim",   "low",  "raw",
     "How correlated up/down (pitch) crosshair movement is from one frame to the next. "
     "Human aim has natural pitch/yaw coupling (~0.66); near-zero correlation is a "
     "bot/assist signature."),
    ("missing_badness",    "Worst-kill smoothness (p10)",  "aim",   "high", "raw",
     "The worst (10th-percentile) settle-smoothness value across a player's kills. Real "
     "players have some genuinely bad aim moments; a suspiciously good worst case means "
     "that lower tail is missing."),
    ("tot_median",         "Time-on-target median (ticks)", "aim",  "high", "raw",
     "Median time the crosshair stays on-target before firing. Very short time-on-target "
     "combined with high accuracy is another soft-aim signature."),
    # Option 1: round-by-round consistency (togglers show high CV).
    ("round_flick_cv",     "Flick consistency (CV)",       "aim",   "high", "raw",
     "Round-to-round variability in flick speed. Very low variability means aim quality "
     "barely changes round to round - real players fluctuate with focus and nerves."),
    ("round_settle_cv",    "Settle consistency (CV)",      "aim",   "high", "raw",
     "Round-to-round variability in settle smoothness - same idea as flick consistency."),

    ("occl_track_rate",    "Occluded-enemy tracking %",    "info",  "high", "pct",
     "How often the crosshair tracks an enemy's position while no teammate has spotted "
     "that enemy yet - a proxy for wallhack-style information advantage, since there's no "
     "legitimate way to know exactly where an unspotted enemy is."),
    ("occl_streaks",       "Occluded tracking streaks",    "info",  "high", "raw",
     "Number of sustained tracking streaks on unspotted enemies (see Occluded-enemy "
     "tracking %) - repeated streaks are stronger evidence than a single one."),
    ("smoke_kill_rate",    "Through-smoke kill %",         "info",  "high", "pct",
     "Share of kills landed on enemies obscured by smoke. Occasional through-smoke kills "
     "are normal; a high rate across many kills is unusual."),
    ("wallbang_rate",      "Wallbang kill %",              "info",  "high", "pct",
     "Share of kills landed by shooting through a wall or other solid object. Occasional "
     "wallbangs are normal play; a high rate is unusual."),
    ("blind_kill_rate",    "Kills while flashed %",        "info",  "high", "pct",
     "Share of kills the player landed while flashed themselves. Occasional flashed kills "
     "happen from muscle memory; a high rate is unusual."),
]

FEATURE_INDEX = {f[0]: f for f in FEATURE_DEFS}


def _pctile(sorted_vals, q):
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * q
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def _corr(xs, ys):
    n = len(xs)
    if n < 8:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def _wrap_deg(d):
    return ((d + 180.0) % 360.0) - 180.0


def _cv(vals):
    """Coefficient of variation (std/mean). Returns None if mean ≈ 0."""
    if len(vals) < 4:
        return None
    mean = sum(vals) / len(vals)
    if mean < 1e-6:
        return None
    std = math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals))
    return std / mean


def _streak_block_counts(occl_record, block_ticks=1024):
    """Partition streak start-ticks into fixed-width tick blocks and count
    streaks per block - a genuine resampling unit for bootstrapping
    occl_streaks. Resampling the streak list itself to its own length always
    returns a same-length list (a zero-width, meaningless CI), so the
    bootstrap unit here is "block of the occluded-tracking timeline",
    not "streak"."""
    streaks = occl_record.get("streaks") or []
    if not streaks:
        return []
    starts = sorted(s["start_tick"] for s in streaks)
    hi = max(s["end_tick"] for s in streaks)
    lo = starts[0]
    n_blocks = max(1, (hi - lo) // block_ticks + 1)
    counts = [0] * n_blocks
    for t in starts:
        counts[min((t - lo) // block_ticks, n_blocks - 1)] += 1
    return counts


# Reducer used to turn a bootstrap resample of a feature's raw values back
# into that feature's point-estimate statistic - colocated with the formulas
# above so the point estimate and its bootstrap replicate can never diverge.
# Proportion-kind features (see compute_features) don't need an entry here;
# they get a Wilson interval directly from (successes, trials) instead.
FEATURE_STAT_FNS = {
    "flick_speed":      lambda vs: _pctile(sorted(vs), 0.9),
    "flick_sniper":     lambda vs: _pctile(sorted(vs), 0.9),
    "flick_rifle":      lambda vs: _pctile(sorted(vs), 0.9),
    "settle_smooth":    lambda vs: statistics.mean(vs) if vs else None,
    "settle_sniper":    lambda vs: statistics.mean(vs) if vs else None,
    "settle_rifle":     lambda vs: statistics.mean(vs) if vs else None,
    "settle_signflips": lambda vs: statistics.mean(vs) if vs else None,
    "missing_badness":  lambda vs: _pctile(sorted(vs), 0.1),
    "tot_median":       lambda vs: _pctile(sorted(vs), 0.5),
    "first_sight_div":  lambda vs: _pctile(sorted(vs), 0.5),
    "reaction_ms":      lambda vs: _pctile(sorted(vs), 0.5),
    "round_flick_cv":   _cv,
    "round_settle_cv":  _cv,
    "pitch_corr":       lambda pairs: _corr([x for x, _ in pairs], [y for _, y in pairs]),
    "occl_streaks":     sum,
}


# ── Window-derived per-kill metrics ──────────────────────────────────────────

def _window_metrics(window):
    """Returns dict with flick, settle metrics for one kill window, or None."""
    rows = sorted((r for r in window.get("rows", [])), key=lambda r: r[COL_REL])
    pre = [r for r in rows if -30 <= r[COL_REL] <= 0]
    if len(pre) < 8:
        return None

    # Flick speed: max combined angular step over the last 10 frames pre-kill.
    flick = 0.0
    steps_yaw, steps_pitch = [], []
    prev = None
    for r in pre:
        if prev is not None and r[COL_REL] - prev[COL_REL] == 1:
            dy = _wrap_deg(r[COL_YAW] - prev[COL_YAW])
            dp = r[COL_PITCH] - prev[COL_PITCH]
            steps_yaw.append(dy)
            steps_pitch.append(dp)
            if r[COL_REL] >= -10:
                flick = max(flick, math.hypot(dy, dp))
        prev = r

    # Settle curve: angle-to-victim over the last 15 frames before the shot.
    settle = [(r[COL_REL], r[COL_ANG]) for r in pre
              if r[COL_REL] >= -15 and r[COL_ANG] >= 0]
    smooth, flips = None, None
    if len(settle) >= 6:
        deltas = [b[1] - a[1] for a, b in zip(settle, settle[1:])]
        moving = [d for d in deltas if abs(d) > 1e-6]
        if moving:
            smooth = sum(1 for d in moving if d < 0) / len(moving)
            signs = [d > 0 for d in moving]
            flips = sum(1 for a, b in zip(signs, signs[1:]) if a != b)

    return {
        "flick": flick,
        "smooth": smooth,
        "flips": flips,
        "steps_yaw": steps_yaw,
        "steps_pitch": steps_pitch,
    }


# ── Main entry ────────────────────────────────────────────────────────────────

def compute_features(raw):
    """raw: parsed overwatch_raw JSON. Returns (feats, extras, feat_meta):
    feats = {steamid: {feature: value}}; extras = per-player kill metrics
    for moment ranking, {steamid: {"kill_flicks": [(kill_idx, flick)], ...}};
    feat_meta = {steamid: {feature: {"kind", "n", "raw"}}} - the sample size
    and underlying raw values/counts backing each feature value, used by
    overwatch.py to attach confidence intervals. "kind" is "prop" (raw is a
    (successes, trials) pair, scored with a Wilson interval), "cont" (raw is
    a list of floats, bootstrapped via FEATURE_STAT_FNS), "corr" (raw is a
    list of (x, y) pairs), or "count_blocks" (raw is a list of per-block
    counts, see _streak_block_counts). A feature missing from feat_meta[pid]
    has no CI (e.g. it wasn't computed for that player at all)."""
    kills = raw.get("kills", [])
    damage = raw.get("damage", [])
    windows = raw.get("windows", [])
    tot = raw.get("tot_episodes", {})
    occl = raw.get("occlusion", {})
    sights = raw.get("first_sights", [])
    players = {p["steam_id"] for p in raw.get("players", [])}

    feats = {pid: {} for pid in players}
    extras = {pid: {"kill_flicks": []} for pid in players}
    feat_meta = {pid: {} for pid in players}

    # ── Kill-event rates ──
    by_attacker = {}
    deaths = {}
    for i, k in enumerate(kills):
        by_attacker.setdefault(k["attacker_id"], []).append((i, k))
        deaths.setdefault(k["victim_id"], []).append(k)

    for pid in players:
        pk = by_attacker.get(pid, [])
        n = len(pk)
        feats[pid]["n_kills"] = n
        if n:
            hs_n = sum(1 for _, k in pk if k["headshot"])
            smoke_n = sum(1 for _, k in pk if k["through_smoke"])
            wallbang_n = sum(1 for _, k in pk if k["penetrated"] > 0)
            blind_n = sum(1 for _, k in pk if k["attacker_blind"])
            feats[pid]["hs_rate"] = 100.0 * hs_n / n
            feats[pid]["smoke_kill_rate"] = 100.0 * smoke_n / n
            feats[pid]["wallbang_rate"] = 100.0 * wallbang_n / n
            feats[pid]["blind_kill_rate"] = 100.0 * blind_n / n
            feat_meta[pid]["hs_rate"] = {"kind": "prop", "n": n, "raw": (hs_n, n)}
            feat_meta[pid]["smoke_kill_rate"] = {"kind": "prop", "n": n, "raw": (smoke_n, n)}
            feat_meta[pid]["wallbang_rate"] = {"kind": "prop", "n": n, "raw": (wallbang_n, n)}
            feat_meta[pid]["blind_kill_rate"] = {"kind": "prop", "n": n, "raw": (blind_n, n)}

    # ── Damage-derived ──
    dmg_sorted = sorted(damage, key=lambda d: (d["attacker_id"], d["victim_id"], d["tick"]))
    eng_first = []
    prev_key, prev_tick = None, None
    for d in dmg_sorted:
        key = (d["attacker_id"], d["victim_id"])
        if key != prev_key or d["tick"] - prev_tick > ENGAGEMENT_GAP_TICKS:
            eng_first.append(d)
        prev_key, prev_tick = key, d["tick"]

    fb_by_p, hits_by_p = {}, {}
    for d in eng_first:
        fb_by_p.setdefault(d["attacker_id"], []).append(d["hitgroup"] == HITGROUP_HEAD)
    for d in damage:
        if d["attacker_speed"] >= 0:
            hits_by_p.setdefault(d["attacker_id"], []).append(d["attacker_speed"] > RUN_SPEED)
    for pid in players:
        fb = fb_by_p.get(pid, [])
        if len(fb) >= 5:
            fb_n = sum(fb)
            feats[pid]["first_bullet_hs"] = 100.0 * fb_n / len(fb)
            feat_meta[pid]["first_bullet_hs"] = {"kind": "prop", "n": len(fb), "raw": (fb_n, len(fb))}
        hits = hits_by_p.get(pid, [])
        if len(hits) >= 10:
            hits_n = sum(hits)
            feats[pid]["hits_moving"] = 100.0 * hits_n / len(hits)
            feat_meta[pid]["hits_moving"] = {"kind": "prop", "n": len(hits), "raw": (hits_n, len(hits))}

    # Dumb deaths: died without ever damaging the killer that round.
    dealt = {}
    for d in damage:
        dealt[(d["attacker_id"], d["victim_id"], d["round"])] = True
    for pid in players:
        dd = deaths.get(pid, [])
        if len(dd) >= 5:
            no_dmg = sum(1 for k in dd
                         if not dealt.get((pid, k["attacker_id"], k["round"])))
            feats[pid]["dumb_deaths"] = 100.0 * no_dmg / len(dd)
            feat_meta[pid]["dumb_deaths"] = {"kind": "prop", "n": len(dd), "raw": (no_dmg, len(dd))}

    # ── Window-derived aim mechanics ──
    # Collect all window metrics, grouped by player, by round (opt 1), and by
    # weapon class (opt 4) in a single pass over windows.
    wm_by_p          = {}   # pid -> [metrics]
    wm_by_p_by_round = {}   # pid -> round -> [metrics]  (option 1)
    wm_by_p_by_class = {}   # pid -> wepclass -> [metrics]  (option 4)

    for w in windows:
        if w["kill"] >= len(kills):
            continue
        k = kills[w["kill"]]
        m = _window_metrics(w)
        if m is None:
            continue
        pid = k["attacker_id"]
        wm_by_p.setdefault(pid, []).append(m)
        wm_by_p_by_round.setdefault(pid, {}).setdefault(k["round"], []).append(m)
        extras.setdefault(pid, {"kill_flicks": []})
        extras[pid]["kill_flicks"].append((w["kill"], m["flick"]))

        # Option 4: bucket by weapon class.
        wclass = ("sniper" if k["weapon"] in SNIPER_WEAPONS
                  else "rifle" if k["weapon"] in RIFLE_WEAPONS
                  else None)
        if wclass:
            wm_by_p_by_class.setdefault(pid, {}).setdefault(wclass, []).append(m)

    # Aggregate aim mechanics (all weapons).
    for pid, ms in wm_by_p.items():
        if pid not in feats:
            continue
        flicks = sorted(m["flick"] for m in ms)
        smooths = sorted(m["smooth"] for m in ms if m["smooth"] is not None)
        flipss = [m["flips"] for m in ms if m["flips"] is not None]
        if len(flicks) >= 5:
            feats[pid]["flick_speed"] = _pctile(flicks, 0.9)
            feat_meta[pid]["flick_speed"] = {"kind": "cont", "n": len(flicks), "raw": flicks}
        if len(smooths) >= 5:
            feats[pid]["settle_smooth"] = statistics.mean(smooths)
            feats[pid]["missing_badness"] = _pctile(smooths, 0.1)
            feat_meta[pid]["settle_smooth"] = {"kind": "cont", "n": len(smooths), "raw": smooths}
            feat_meta[pid]["missing_badness"] = {"kind": "cont", "n": len(smooths), "raw": smooths}
        if len(flipss) >= 5:
            feats[pid]["settle_signflips"] = statistics.mean(flipss)
            feat_meta[pid]["settle_signflips"] = {"kind": "cont", "n": len(flipss), "raw": flipss}
        py = [s for m in ms for s in m["steps_pitch"]]
        if len(py) >= 50:
            c = _corr(py[:-1], py[1:])
            if c is not None:
                feats[pid]["pitch_corr"] = c
                feat_meta[pid]["pitch_corr"] = {"kind": "corr", "n": len(py) - 1,
                                                 "raw": list(zip(py[:-1], py[1:]))}

    # Option 1: round-by-round variance - catches togglers whose per-round
    # aim quality is far more variable than a consistent legit player.
    for pid in players:
        rnd_map = wm_by_p_by_round.get(pid, {})
        rnd_flick_means = []
        rnd_smooth_means = []
        for ms in rnd_map.values():
            if not ms:
                continue
            rnd_flick_means.append(sum(m["flick"] for m in ms) / len(ms))
            sm = [m["smooth"] for m in ms if m["smooth"] is not None]
            if sm:
                rnd_smooth_means.append(sum(sm) / len(sm))
        cv_f = _cv(rnd_flick_means)
        if cv_f is not None:
            feats[pid]["round_flick_cv"] = cv_f
            feat_meta[pid]["round_flick_cv"] = {"kind": "cont", "n": len(rnd_flick_means),
                                                 "raw": rnd_flick_means}
        cv_s = _cv(rnd_smooth_means)
        if cv_s is not None:
            feats[pid]["round_settle_cv"] = cv_s
            feat_meta[pid]["round_settle_cv"] = {"kind": "cont", "n": len(rnd_smooth_means),
                                                  "raw": rnd_smooth_means}

    # Option 4: weapon-stratified flick + settle.
    for pid in players:
        for wclass in ("sniper", "rifle"):
            ms = wm_by_p_by_class.get(pid, {}).get(wclass, [])
            flicks = sorted(m["flick"] for m in ms)
            smooths = sorted(m["smooth"] for m in ms if m["smooth"] is not None)
            if len(flicks) >= 3:
                feats[pid][f"flick_{wclass}"] = _pctile(flicks, 0.9)
                feat_meta[pid][f"flick_{wclass}"] = {"kind": "cont", "n": len(flicks), "raw": flicks}
            if len(smooths) >= 3:
                feats[pid][f"settle_{wclass}"] = statistics.mean(smooths)
                feat_meta[pid][f"settle_{wclass}"] = {"kind": "cont", "n": len(smooths), "raw": smooths}

    # ── ToT ──
    for pid in players:
        eps = sorted(tot.get(pid, []))
        if len(eps) >= 10:
            feats[pid]["tot_median"] = _pctile(eps, 0.5)
            feat_meta[pid]["tot_median"] = {"kind": "cont", "n": len(eps), "raw": eps}

    # ── Occlusion ──
    for pid in players:
        o = occl.get(pid)
        if o and o["total_frames"] >= 2000:
            feats[pid]["occl_track_rate"] = 100.0 * o["track_frames"] / o["total_frames"]
            feats[pid]["occl_streaks"] = len(o.get("streaks", []))
            feat_meta[pid]["occl_track_rate"] = {"kind": "prop", "n": o["total_frames"],
                                                  "raw": (o["track_frames"], o["total_frames"])}
            block_counts = _streak_block_counts(o)
            if block_counts:
                feat_meta[pid]["occl_streaks"] = {"kind": "count_blocks", "n": len(block_counts),
                                                   "raw": block_counts}

    # ── First sight: divergence + reaction ──
    dmg_by_pair = {}
    for d in damage:
        dmg_by_pair.setdefault((d["attacker_id"], d["victim_id"]), []).append(d["tick"])
    for ticks in dmg_by_pair.values():
        ticks.sort()
    div_by_p, react_by_p = {}, {}
    tickrate = raw.get("tickrate") or 64.0
    for s in sights:
        pair = (s["observer_id"], s["enemy_id"])
        hit_tick = None
        for t in dmg_by_pair.get(pair, []):
            if s["tick"] <= t <= s["tick"] + REACTION_JOIN_TICKS:
                hit_tick = t
                break
        if hit_tick is not None:
            div_by_p.setdefault(s["observer_id"], []).append(s["divergence"])
            react_by_p.setdefault(s["observer_id"], []).append(
                1000.0 * (hit_tick - s["tick"]) / tickrate)
    for pid in players:
        dv = sorted(div_by_p.get(pid, []))
        if len(dv) >= 8:
            feats[pid]["first_sight_div"] = _pctile(dv, 0.5)
            feat_meta[pid]["first_sight_div"] = {"kind": "cont", "n": len(dv), "raw": dv}
        rc = sorted(react_by_p.get(pid, []))
        if len(rc) >= 8:
            feats[pid]["reaction_ms"] = _pctile(rc, 0.5)
            feat_meta[pid]["reaction_ms"] = {"kind": "cont", "n": len(rc), "raw": rc}

    return feats, extras, feat_meta


# ── Timing metrics + spotted-shot accuracy - player-page metrics ────────────
# Deliberately separate from compute_features() above: no rank-banded
# baseline, no confidence intervals, no anti-cheat percentile framing - the
# player page just wants a plain per-player median next to a flat average
# over players who have a rank, not a percentile system.
#
# a_fired (cmd/overwatch/main.go's column 12) cannot be used here even though
# it looks like the obvious source for "did this player fire" - the Go
# extractor's own comment on shotFrame() says it plainly: "a_fired is only
# known for the LIVE frame (events are consumed per frame); when building
# rows from history we can't recover it, so it's 0 there." A kill's window
# is built by reconstructing its 224-tick PRE-kill portion from a history
# ring buffer after the fact - exactly the case that comment describes - so
# every pre-kill row's a_fired reads back as a hardcoded 0, confirmed against
# this repo's own overwatch_raw corpus (zero pre-kill a_fired=1 rows found
# anywhere; all real fired=1 rows sit at rel>0, the live post-kill tail).
# v_spotted has no such gap - `frameSnap.spotted` is a real field set on
# every historical snapshot - so "first spot" below is trustworthy; only the
# "did they shoot" side needed a different source.
#
# That source is the main parser's own shots[] (per-tick, whole match,
# recorded live regardless of any 2nd-pass window) - passed in by the caller
# (app.py reads it off the already-cached .slim.json, not the full match
# JSON with grenades[] attached) and cross-referenced by tick instead.
MIN_TIMING_SAMPLES = 5   # per match - a couple of kills is noise, not a
                         # measurement; app.py's cross-match rollup averages
                         # together whichever matches clear this gate.

# Shot weapons worth calling "accuracy" - excludes grenades/knife/bomb/etc.,
# which analysis.moments.weapon_class() also returns for a shots[] entry.
_GUN_CLASSES = frozenset({"rifle", "sniper", "smg", "pistol", "shotgun", "lmg"})


def compute_timing_features(raw, shots=None):
    """raw: parsed overwatch_raw JSON (same file compute_features() reads).
    shots: this match's own `shots[]` (see module docstring above for why -
    omit it and time_to_shoot/spotted_acc are simply not computed, not wrong).

    Returns {steam_id: {"time_to_kill": {"value","n"}|None,
    "time_to_shoot": ..., "time_to_damage": ..., "spotted_acc": {weapon_class:
    {"value","n"}}}} - medians in SECONDS over this match's kills (spotted_acc
    in %), gated on MIN_TIMING_SAMPLES so "not enough data" and "instant" can
    never look the same (absent, not 0).

    For each kill's window, "first spot" is the first pre-kill row with
    SPOTTED==1 - EXCLUDING the window's very first row: if that one is
    already spotted, the real first sight happened before the 224-tick
    lookback and the true time-to-X for this kill is unknown, not zero, so
    the kill is skipped rather than left-censored into the median.
      time_to_kill   = (kill tick) - (first-spot tick)
      time_to_shoot  = (this attacker's first shots[] tick at/after first
                        spot, no later than the kill + the post-kill tail) -
                        (first-spot tick)
      time_to_damage = (first damage tick, this attacker→victim pair, at/after
                        first-spot) - (first-spot tick)
      spotted_acc    = hit% among this attacker's shots[] entries that landed
                        on a SPOTTED row of this kill's window, bucketed by
                        that shot's own weapon (not the kill's - a player can
                        swap weapons mid-engagement even if the window can't
                        see it swap crosshairs).
    A shot can be attributed to more than one kill's window when two kills
    happen close together (over-lapping 224-tick lookbacks) - a real but
    minor double-count, same order of approximation as the rest of this
    per-kill-window pipeline.
    """
    import bisect

    kills = raw.get("kills", [])
    damage = raw.get("damage", [])
    windows = raw.get("windows", [])
    tickrate = raw.get("tickrate") or 64.0

    dmg_by_pair = {}
    for d in damage:
        dmg_by_pair.setdefault((d["attacker_id"], d["victim_id"]), []).append(d["tick"])
    for ticks in dmg_by_pair.values():
        ticks.sort()

    shots_by_id = {}   # pid -> [(tick, weapon), ...] sorted by tick
    if shots:
        id_by_name = {p["name"]: p["steam_id"] for p in raw.get("players", [])}
        for s in shots:
            pid = id_by_name.get(s.get("player_name"))
            if pid and s.get("tick") is not None:
                shots_by_id.setdefault(pid, []).append((s["tick"], s.get("weapon")))
        for lst in shots_by_id.values():
            lst.sort()
    shot_ticks_by_id = {pid: [t for t, _ in lst] for pid, lst in shots_by_id.items()}

    kill_by_p, shoot_by_p, dmg_time_by_p = {}, {}, {}
    acc_by_p = {}   # pid -> weapon_class -> [shots_spotted, hits_spotted]

    for w in windows:
        if w["kill"] >= len(kills):
            continue
        k = kills[w["kill"]]
        pid = k["attacker_id"]
        kill_tick = k["tick"]
        rows = sorted((r for r in w.get("rows", []) if r[COL_REL] <= 0), key=lambda r: r[COL_REL])
        if not rows:
            continue

        first_spot_idx = next((i for i, r in enumerate(rows) if r[COL_SPOTTED]), None)
        if first_spot_idx is None or first_spot_idx == 0:
            continue
        first_spot_rel = rows[first_spot_idx][COL_REL]
        first_spot_abs = kill_tick + first_spot_rel

        kill_by_p.setdefault(pid, []).append((0 - first_spot_rel) / tickrate)

        pair_ticks = dmg_by_pair.get((k["attacker_id"], k["victim_id"]), [])
        dmg_tick = next((t for t in pair_ticks if t >= first_spot_abs), None)
        if dmg_tick is not None:
            dmg_time_by_p.setdefault(pid, []).append((dmg_tick - first_spot_abs) / tickrate)

        s_ticks = shot_ticks_by_id.get(pid, [])
        if s_ticks:
            i = bisect.bisect_left(s_ticks, first_spot_abs)
            if i < len(s_ticks) and s_ticks[i] <= kill_tick + 32:
                shoot_by_p.setdefault(pid, []).append((s_ticks[i] - first_spot_abs) / tickrate)

        row_by_rel = {r[COL_REL]: r for r in rows}
        for (t, wep) in shots_by_id.get(pid, []):
            rel = t - kill_tick
            if rel < first_spot_rel or rel > 0:
                continue
            row = row_by_rel.get(rel)
            if row is None or not row[COL_SPOTTED]:
                continue
            gclass = weapon_class(wep)
            if gclass not in _GUN_CLASSES:
                continue
            bucket = acc_by_p.setdefault(pid, {}).setdefault(gclass, [0, 0])
            bucket[0] += 1
            if any(abs(pt - t) <= 2 for pt in pair_ticks):
                bucket[1] += 1

    def _entry(vals):
        vals = sorted(vals)
        if len(vals) < MIN_TIMING_SAMPLES:
            return None
        return {"value": round(_pctile(vals, 0.5), 3), "n": len(vals)}

    pids = set(kill_by_p) | set(shoot_by_p) | set(dmg_time_by_p) | set(acc_by_p)
    out = {}
    for pid in pids:
        spotted_acc = {}
        for gclass, (shots_n, hits_n) in acc_by_p.get(pid, {}).items():
            if shots_n >= MIN_TIMING_SAMPLES:
                spotted_acc[gclass] = {"value": round(100.0 * hits_n / shots_n, 1), "n": shots_n}
        out[pid] = {
            "time_to_kill":   _entry(kill_by_p.get(pid, [])),
            "time_to_shoot":  _entry(shoot_by_p.get(pid, [])),
            "time_to_damage": _entry(dmg_time_by_p.get(pid, [])),
            "spotted_acc": spotted_acc,
        }
    return out
