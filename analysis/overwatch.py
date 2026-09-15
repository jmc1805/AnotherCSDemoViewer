"""Orchestrator for the Overwatch tab: raw extract → features → rank-banded
percentiles → suspicion summary + reviewable moments. Caches per match.

Output contract (consumed by the Overwatch tab in match.html):
{
  "ok": true,
  "match": "<match_id>",
  "min_kills": 15,
  "players": [{
     "steam_id", "name", "rank", "rank_type", "band", "kills",
     "level": "insufficient" | "low" | "elevated" | "high",
     "composite_z": float,          # Option 2 - combined suspicious z-score
     "composite_z_shrunk": float,   # same, but low-n features are shrunk
                                     # toward the baseline mean first (see
                                     # PSEUDO_N below) - ships alongside
                                     # composite_z, doesn't replace it
     "features": [{
         key, label, group, desc, value, fmt, direction,
                                     # desc: plain-language explanation of
                                     # what the indicator measures, shown on
                                     # hover over the indicator label
         percentile, n_baseline,    # global baseline percentile
         peer_z,                    # Option 3 - z-score vs this match's 10 players
         ci,                        # [lo, hi] confidence interval on value,
                                     # Wilson (proportions) or bootstrap
                                     # (everything else); None if too few
                                     # events back the value
         n_events,                  # this player's own sample size backing
                                     # `value` (distinct from n_baseline,
                                     # which is the baseline pool size)
         pooled,                    # {n_pool, n_matches, ci, percentile} -
                                     # same value re-estimated by pooling this
                                     # player's own history across every
                                     # *other* analyzed match; None below the
                                     # baseline-sample gate
     }],
     "moments": [{tick, round, start_tick, end_tick, label, detail}]
                                     # start_tick/end_tick bracket the clip
                                     # window for this moment (see
                                     # analysis/clipwindow.py) - used by the
                                     # in-tab "Preview clip" widget
  }],
  "baseline_note": "..."
}
Levels are evidence-for-review indicators, never verdicts: "high" means
≥2 features past the 99th percentile for the player's rank band with ≥15
kills - exactly the combination rule from the design doc.
"""

import json
import math
import os
import statistics
import time

import jsonio   # root module (like paths.py) - .br-transparent JSON reads

from .features import compute_features, FEATURE_DEFS, FEATURE_INDEX, FEATURE_STAT_FNS
from .baselines import BaselineStore, band_for_rank
from .stats import wilson_interval, bootstrap_ci, shrink_toward_prior, stable_seed
from .clipwindow import clip_window_for_kill

MIN_KILLS_FOR_FLAGS = 15
MIN_BASELINE_FOR_FLAGS = 20  # percentile flags need a real population first
MIN_CHEAT_BASELINE = 3       # confirmed cheaters are rare; 3 is enough to compare
MAX_MOMENTS = 12

# Option 2: composite z-score per feature is clamped to [0, CAP] before
# summing so a single extreme metric can't dominate the total.
_Z_CAP = 3.0

# Empirical-Bayes-style shrinkage pseudo-counts per feature family, used to
# build composite_z_shrunk (see analyze()). weight = n_events / (n_events +
# PSEUDO_N[key]) - tuned to roughly match each feature's own minimum-count
# gate in features.py, so a feature exactly at its gate shrinks ~50% toward
# the baseline mean, and a well-supported feature is barely shrunk at all.
PSEUDO_N = {
    "flick_sniper": 3, "settle_sniper": 3,
    "flick_rifle": 5, "settle_rifle": 5, "flick_speed": 5,
    "settle_smooth": 5, "missing_badness": 5, "settle_signflips": 5,
    "first_bullet_hs": 5, "dumb_deaths": 5,
    "round_flick_cv": 5, "round_settle_cv": 5,
    "hits_moving": 10, "tot_median": 10,
    "first_sight_div": 8, "reaction_ms": 8,
    "hs_rate": 15, "smoke_kill_rate": 15, "wallbang_rate": 15, "blind_kill_rate": 15,
    "occl_streaks": 20, "pitch_corr": 50, "occl_track_rate": 2000,
}
_DEFAULT_PSEUDO_N = 10


def _fmt_value(val, fmt):
    if val is None:
        return None
    if fmt == "pct":
        return round(val, 1)
    if fmt in ("deg", "ms"):
        return round(val, 1)
    return round(val, 3)


def analyze(match_id, processed_dir, raw_dir, data_dir, force=False, labels=None,
            is_pro_match=False):
    """match_id without .json suffix. Raises FileNotFoundError when the raw
    extract is missing (caller decides how to produce it).
    labels: dict of {steam_id: {label, name, ...}} from labels.json.
    is_pro_match: when True all players are treated as pros - excluded from
    clean baselines and level overridden to verified_pro."""
    raw_path = os.path.join(raw_dir, f"{match_id}.json")
    cache_path = os.path.join(processed_dir, f"{match_id}.overwatch.json")
    raw_file = jsonio.resolve(raw_path)   # plain or .br (brotli at rest)
    if raw_file is None:
        raise FileNotFoundError(raw_path)

    if not force and os.path.exists(cache_path) \
            and os.path.getmtime(cache_path) >= os.path.getmtime(raw_file):
        with open(cache_path, encoding="utf-8") as f:
            return json.load(f)

    raw = jsonio.load(raw_path)

    ranks = {}
    round_start_by_num = {}
    proc_path = os.path.join(processed_dir, f"{match_id}.json")
    if jsonio.resolve(proc_path):
        proc = jsonio.load(proc_path)
        for r in proc.get("ranks", []) or []:
            ranks[r.get("steam_id", "")] = r
        for rnd in proc.get("rounds", []) or []:
            if rnd.get("round_num") is not None and rnd.get("start") is not None:
                round_start_by_num[rnd["round_num"]] = rnd["start"]

    labels = labels or {}
    cheater_ids     = {sid for sid, info in labels.items() if info.get("label") == "cheater"}
    pro_ids         = {sid for sid, info in labels.items() if info.get("label") == "professional"}
    suspicious_ids  = {sid for sid, info in labels.items() if info.get("label") == "suspicious"}

    feats, extras, feat_meta = compute_features(raw)
    names = {p["steam_id"]: p["name"] for p in raw.get("players", [])}
    kills = raw.get("kills", [])
    occl = raw.get("occlusion", {})

    store = BaselineStore(os.path.join(data_dir, "baselines.json"))
    cheat_store = BaselineStore(os.path.join(data_dir, "cheater_baselines.json"))
    rows = []
    for pid, f in feats.items():
        r = ranks.get(pid, {})
        band = band_for_rank(r.get("rank_type", 0), r.get("rank", 0))
        rows.append((pid, band, f))
    # Only update the clean baseline with non-cheater, non-pro players.
    # Pros (individually labeled or whole-match pro) are excluded because their
    # elite stats would inflate "normal" percentiles, making real cheaters look
    # less anomalous against the population.
    if is_pro_match:
        clean_rows = []
    else:
        clean_rows = [(pid, band, f) for pid, band, f in rows
                      if pid not in cheater_ids and pid not in pro_ids
                      and pid not in suspicious_ids]
    store.update_match(match_id, clean_rows)
    # Update the cheater-only baseline; no rank banding (too few samples to stratify).
    cheat_rows = [(pid, "all", f) for pid, band, f in rows if pid in cheater_ids]
    if cheat_rows:
        cheat_store.update_match(match_id, cheat_rows)

    # Option 3: build within-match feature distributions for peer z-scores.
    # Each feature accumulates all 10 players' values; we z-score each player
    # against the match population (including themselves - with only 10 players
    # the bias from self-inclusion is small and avoids leave-one-out complexity).
    match_feat_vals = {}   # feature_key -> [raw value]
    for pid2, _band2, f2 in rows:
        for key2, val2 in f2.items():
            if key2 != "n_kills" and val2 is not None:
                match_feat_vals.setdefault(key2, []).append(val2)
    # Pre-compute mean + std per feature for the match population.
    match_stats = {}   # feature_key -> (mean, std)
    for key2, vals2 in match_feat_vals.items():
        if len(vals2) < 3:
            continue
        m2 = sum(vals2) / len(vals2)
        s2 = math.sqrt(sum((v - m2) ** 2 for v in vals2) / len(vals2))
        if s2 > 0:
            match_stats[key2] = (m2, s2)

    # Per-match flick p95 for moment labelling.
    all_flicks = sorted(fl for ex in extras.values() for _, fl in ex["kill_flicks"])
    flick_p95 = all_flicks[int(len(all_flicks) * 0.95)] if len(all_flicks) >= 20 else None

    players_out = []
    for pid, band, f in rows:
        n_kills = f.get("n_kills", 0)
        feature_rows = []
        count95 = count99 = 0

        # Option 2 accumulators.
        composite_parts = []
        composite_parts_shrunk = []

        for key, label, group, direction, fmt, desc in FEATURE_DEFS:
            val = f.get(key)
            if val is None:
                continue
            meta = feat_meta.get(pid, {}).get(key)

            # Global baseline percentile.
            pct, n_base = store.percentile(key, band, val, direction,
                                           exclude_ids=cheater_ids)
            if pct is not None and n_kills >= MIN_KILLS_FOR_FLAGS \
                    and n_base >= MIN_BASELINE_FOR_FLAGS:
                if pct >= 99:
                    count99 += 1
                    count95 += 1
                elif pct >= 95:
                    count95 += 1

            # Option 2: per-feature suspicious z-score vs baseline mean/std.
            mean_b, std_b, n_b = store.feature_stats(key, band,
                                                      exclude_ids=cheater_ids)
            if mean_b is not None and std_b is not None and std_b > 0 \
                    and n_b >= MIN_BASELINE_FOR_FLAGS:
                z_sus = (val - mean_b) / std_b if direction == "high" \
                        else (mean_b - val) / std_b
                composite_parts.append(min(max(z_sus, 0.0), _Z_CAP))

                # Shrinkage variant (composite_z_shrunk): pull `val` toward
                # the baseline mean by weight n_events/(n_events+PSEUDO_N)
                # before scoring, so a feature backed by only a handful of
                # events doesn't contribute at full strength. Independent
                # accumulator - never feeds composite_parts/composite_z.
                n_events = meta["n"] if meta else None
                shrunk_val = shrink_toward_prior(
                    val, n_events, mean_b, PSEUDO_N.get(key, _DEFAULT_PSEUDO_N))
                z_sus_shrunk = (shrunk_val - mean_b) / std_b if direction == "high" \
                               else (mean_b - shrunk_val) / std_b
                composite_parts_shrunk.append(min(max(z_sus_shrunk, 0.0), _Z_CAP))

            # Option 3: within-match peer z-score in suspicious direction.
            peer_z = None
            if key in match_stats:
                m_peer, s_peer = match_stats[key]
                raw_z = (val - m_peer) / s_peer
                peer_z = round(raw_z if direction == "high" else -raw_z, 2)

            # Cheater reference: percentile vs confirmed-cheater distribution.
            cheat_pct, n_cheat = cheat_store.percentile(key, "all", val, direction)

            # Confidence interval on `val` itself: Wilson for proportion
            # features (exact successes/trials), bootstrap-percentile for
            # everything else (resampling the raw per-event values that fed
            # the point estimate). None when there's nothing to resample.
            ci = None
            if meta is not None:
                if meta["kind"] == "prop":
                    succ, trials = meta["raw"]
                    w = wilson_interval(succ, trials)
                    if w is not None:
                        ci = [round(w[0] * 100, 1), round(w[1] * 100, 1)] if fmt == "pct" \
                             else [round(w[0], 3), round(w[1], 3)]
                else:
                    stat_fn = FEATURE_STAT_FNS.get(key)
                    if stat_fn is not None:
                        b = bootstrap_ci(meta["raw"], stat_fn,
                                          seed=stable_seed(match_id, pid, key))
                        if b is not None:
                            lo = _fmt_value(b[0], fmt)
                            hi = _fmt_value(b[1], fmt)
                            if lo is not None and hi is not None:
                                ci = [lo, hi]

            # Pooled cross-match evidence: this player's own value for `key`
            # re-estimated from every *other* analyzed match they appear in
            # (data already sits in baselines.json - no new storage). This is
            # independent corroboration, not a bigger version of this
            # match's number: a single-match 99th-pct flag backed by a tight
            # pooled CI over many matches is much stronger evidence than one
            # backed by a single-match fluke.
            #
            # player_samples() returns one *per-match point estimate* per
            # entry (whatever baselines.json stored for that match - e.g.
            # one flick_speed p90 per match, one pitch_corr coefficient per
            # match), never the raw per-event data a single match's own
            # FEATURE_STAT_FNS reducer expects (pairs for pitch_corr, block
            # counts for occl_streaks, etc). So pooling always reduces via
            # a plain mean of those per-match values, regardless of feature
            # kind - a deliberate simplification, not a re-application of
            # each feature's single-match formula.
            pooled = None
            pool_vals, n_pool, pool_matches = store.player_samples(
                key, pid, exclude_matches={match_id})
            if n_pool >= MIN_BASELINE_FOR_FLAGS:
                pool_point = statistics.mean(pool_vals)
                pb = bootstrap_ci(pool_vals, statistics.mean,
                                   seed=stable_seed('pool', pid, key))
                pool_pct, _ = store.percentile(key, band, pool_point, direction,
                                                exclude_ids=cheater_ids)
                pooled = {
                    "n_pool": n_pool, "n_matches": len(pool_matches),
                    "ci": [round(pb[0], 3), round(pb[1], 3)] if pb is not None else None,
                    "percentile": round(pool_pct, 1) if pool_pct is not None else None,
                }

            feature_rows.append({
                "key": key, "label": label, "group": group, "desc": desc,
                "direction": direction, "fmt": fmt,
                "value": _fmt_value(val, fmt),
                "percentile": round(pct, 1) if pct is not None else None,
                "n_baseline": n_base,
                "peer_z": peer_z,
                "cheat_percentile": round(cheat_pct, 1)
                    if cheat_pct is not None and n_cheat >= MIN_CHEAT_BASELINE
                    else None,
                "n_cheat_baseline": n_cheat,
                "ci": ci,
                "n_events": meta["n"] if meta else None,
                "pooled": pooled,
            })

        # Option 2: composite_z = sum of clamped per-feature z-scores,
        # divided by sqrt(n) so it doesn't grow unboundedly with feature count.
        composite_z = (sum(composite_parts) / math.sqrt(len(composite_parts))
                       if composite_parts else 0.0)
        composite_z_shrunk = (sum(composite_parts_shrunk) / math.sqrt(len(composite_parts_shrunk))
                              if composite_parts_shrunk else 0.0)

        if is_pro_match or pid in pro_ids:
            level = "verified_pro"
        elif n_kills < MIN_KILLS_FOR_FLAGS:
            level = "insufficient"
        elif count99 >= 2:
            level = "high"
        elif count99 >= 1 or count95 >= 2:
            level = "elevated"
        else:
            level = "low"

        # Reviewable moments: flagged kills + longest occluded-tracking streaks.
        moments = []
        for i, k in enumerate(kills):
            if k["attacker_id"] != pid:
                continue
            reasons = []
            if k["through_smoke"]:
                reasons.append("through smoke")
            if k["penetrated"] > 0:
                reasons.append("wallbang")
            if k["attacker_blind"]:
                reasons.append("while flashed")
            if k["noscope"]:
                reasons.append("noscope")
            flick = next((fl for ki, fl in extras.get(pid, {}).get("kill_flicks", [])
                          if ki == i), None)
            if flick is not None and flick_p95 is not None and flick >= flick_p95:
                reasons.append(f"{flick:.0f}°/tick flick")
            if k["headshot"] and reasons:
                reasons.append("HS")
            if reasons:
                start_tick, end_tick = clip_window_for_kill(
                    k["tick"], round_start_by_num.get(k["round"]))
                moments.append({
                    "tick": k["tick"], "round": k["round"],
                    "start_tick": start_tick, "end_tick": end_tick,
                    "label": f"Kill ({k['weapon']})",
                    "detail": ", ".join(reasons),
                    "weight": len(reasons) + (2 if k["through_smoke"] or k["attacker_blind"] else 0),
                })
        streak_moments = []
        for s in (occl.get(pid, {}) or {}).get("streaks", []):
            dur = s["end_tick"] - s["start_tick"]
            streak_moments.append({
                "tick": s["start_tick"], "round": None,
                "start_tick": s["start_tick"], "end_tick": s["end_tick"],
                "label": "Tracked unspotted enemy",
                "detail": f"{names.get(s['enemy_id'], '?')} · {dur} ticks at "
                          f"{s['mean_angle']:.1f}° mean offset",
                "weight": dur / 64.0,
            })
        moments.sort(key=lambda m: -m["weight"])
        streak_moments.sort(key=lambda m: -m["weight"])
        half = MAX_MOMENTS // 2
        moments = moments[:MAX_MOMENTS - min(half, len(streak_moments))] \
            + streak_moments[:half]
        for m in moments:
            m.pop("weight", None)

        players_out.append({
            "steam_id": pid,
            "name": names.get(pid) or ranks.get(pid, {}).get("name", pid),
            "rank": ranks.get(pid, {}).get("rank", 0),
            "rank_type": ranks.get(pid, {}).get("rank_type", 0),
            "band": band,
            "kills": n_kills,
            "level": level,
            "composite_z": round(composite_z, 2),   # Option 2
            "composite_z_shrunk": round(composite_z_shrunk, 2),
            "features": feature_rows,
            "moments": moments[:MAX_MOMENTS],
        })

    players_out.sort(key=lambda p: ({"high": 0, "elevated": 1, "low": 2,
                                     "insufficient": 3, "verified_pro": 4}.get(
                                         p["level"], 5), -p["kills"]))

    n_matches = len({s["m"] for fs in store._load()["samples"].values()
                     for lst in fs.values() for s in lst})
    n_cheat_players = cheat_store.unique_player_count()
    cheater_note    = (f" {len(cheater_ids)} confirmed-cheater player(s) excluded from baselines."
                       if cheater_ids else "")
    suspicious_note = (f" {len(suspicious_ids)} suspicious player(s) excluded from baselines."
                       if suspicious_ids else "")
    pro_note = (
        " This match is marked as a professional match - all players excluded from baselines."
        if is_pro_match else
        (f" {len(pro_ids)} professional player(s) excluded from baselines "
         "(their elite stats would inflate population norms)."
         if pro_ids else "")
    )
    result = {
        "ok": True,
        "match": match_id,
        "generated": int(time.time()),
        "min_kills": MIN_KILLS_FOR_FLAGS,
        "n_labeled_cheaters": len(cheater_ids),
        "n_labeled_pros": len(pro_ids),
        "is_pro_match": is_pro_match,
        "n_cheat_players": n_cheat_players,
        "players": players_out,
        "baseline_note": (
            f"Baselines built from {n_matches} analyzed match(es).{cheater_note}{suspicious_note}{pro_note} "
            "Percentiles are weak below ~10 matches - analyze more demos to "
            "sharpen them. Indicators are evidence for review, never a verdict: "
            "single-match samples are small and legit players hit outliers too."
        ),
    }

    os.makedirs(processed_dir, exist_ok=True)
    tmp = cache_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(result, f)
    os.replace(tmp, cache_path)
    return result
