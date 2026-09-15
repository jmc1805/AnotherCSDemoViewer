"""Unit tests for analysis/stats.py (Wilson intervals, bootstrap CI,
empirical-Bayes shrinkage, stable seeding) and the feat_meta plumbing added
to analysis/features.py that feeds them.

Run:  python3 test/overwatch_stats_test.py     (no deps, no browser)
"""
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from analysis import stats  # noqa: E402
from analysis import features  # noqa: E402

_passed = 0
_failed = 0


def ok(cond, msg):
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
        print("  x FAIL:", msg)


def eq(a, b, msg):
    ok(a == b, f"{msg} (got {a!r}, want {b!r})")


def approx(a, b, tol, msg):
    ok(abs(a - b) <= tol, f"{msg} (got {a!r}, want ~{b!r} +/- {tol})")


# ── wilson_interval ──────────────────────────────────────────────────────────

def test_wilson_interval_bounds():
    eq(stats.wilson_interval(0, 0), None, "n=0 -> None")

    lo, hi = stats.wilson_interval(0, 10)
    eq(lo, 0.0, "0 successes -> lower bound 0")
    ok(hi > 0.0, "0 successes -> nonzero upper bound (non-degenerate)")

    lo, hi = stats.wilson_interval(10, 10)
    eq(hi, 1.0, "all successes -> upper bound 1")
    ok(lo < 1.0, "all successes -> lower bound < 1 (non-degenerate)")

    lo, hi = stats.wilson_interval(50, 100)
    ok(lo < 0.5 < hi, "50/100 interval contains 0.5")


def test_wilson_interval_shrinks_with_n():
    lo1, hi1 = stats.wilson_interval(50, 100)
    lo2, hi2 = stats.wilson_interval(500, 1000)
    ok((hi2 - lo2) < (hi1 - lo1), "interval narrows as n grows at same proportion")


def test_wilson_interval_contains_point_estimate():
    for succ, n in [(1, 5), (3, 20), (19, 20), (0, 3)]:
        lo, hi = stats.wilson_interval(succ, n)
        p = succ / n
        ok(lo <= p <= hi, f"interval [{lo},{hi}] contains p={p} for ({succ},{n})")


# ── bootstrap_ci ─────────────────────────────────────────────────────────────

def test_bootstrap_ci_recovers_mean():
    values = [10.0] * 40 + [20.0] * 10   # true mean = 12.0
    true_mean = sum(values) / len(values)
    lo, hi = stats.bootstrap_ci(values, statistics.mean, n_boot=500, seed=42)
    ok(lo <= true_mean <= hi, f"bootstrap CI [{lo},{hi}] contains true mean {true_mean}")
    ok((hi - lo) < 5.0, f"bootstrap CI reasonably tight, got width {hi - lo}")


def test_bootstrap_ci_deterministic_seed():
    values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]
    a = stats.bootstrap_ci(values, statistics.mean, n_boot=200, seed=7)
    b = stats.bootstrap_ci(values, statistics.mean, n_boot=200, seed=7)
    eq(a, b, "same seed -> identical bootstrap CI")
    c = stats.bootstrap_ci(values, statistics.mean, n_boot=200, seed=99)
    ok(a != c, "different seed -> different bootstrap CI")


def test_bootstrap_ci_degenerate_inputs():
    eq(stats.bootstrap_ci([], statistics.mean), None, "empty values -> None")
    eq(stats.bootstrap_ci([5.0], statistics.mean), None, "single value -> None")


def test_bootstrap_ci_drops_none_replicates():
    # stat_fn returns None whenever a resample happens to be homogeneous.
    def flaky(sample):
        return None if len(set(sample)) == 1 else statistics.mean(sample)

    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    ci = stats.bootstrap_ci(values, flaky, n_boot=300, seed=1)
    ok(ci is not None, "enough valid replicates survive with varied input")

    homogeneous = [7.0, 7.0, 7.0, 7.0]
    ci2 = stats.bootstrap_ci(homogeneous, flaky, n_boot=50, seed=1)
    eq(ci2, None, "every replicate degenerate -> None (not enough valid reps)")


# ── shrink_toward_prior ──────────────────────────────────────────────────────

def test_shrink_toward_prior():
    heavily_shrunk = stats.shrink_toward_prior(100, 1, 10, 50)
    ok(heavily_shrunk < 20, f"tiny n pulls value near prior_mean, got {heavily_shrunk}")

    barely_shrunk = stats.shrink_toward_prior(100, 10000, 10, 50)
    approx(barely_shrunk, 100, 1.0, "huge n stays near raw value")

    eq(stats.shrink_toward_prior(100, 5, None, 50), 100, "prior_mean=None -> pass-through")
    eq(stats.shrink_toward_prior(100, None, 10, 50), 100, "n=None -> pass-through")
    eq(stats.shrink_toward_prior(100, 0, 10, 50), 100, "n=0 -> pass-through")

    # Monotonic: increasing n moves the shrunk value from prior toward raw value.
    v1 = stats.shrink_toward_prior(100, 1, 10, 50)
    v2 = stats.shrink_toward_prior(100, 10, 10, 50)
    v3 = stats.shrink_toward_prior(100, 100, 10, 50)
    ok(v1 < v2 < v3, f"shrinkage monotonic in n: {v1} < {v2} < {v3}")


# ── stable_seed ──────────────────────────────────────────────────────────────

def test_stable_seed():
    a = stats.stable_seed("match1", "steam1", "hs_rate")
    b = stats.stable_seed("match1", "steam1", "hs_rate")
    eq(a, b, "same inputs -> same seed")
    c = stats.stable_seed("match1", "steam1", "flick_speed")
    ok(a != c, "different feature_key -> different seed")


# ── compute_features / feat_meta / FEATURE_STAT_FNS consistency ─────────────
# Highest-value check in this file: catches silent drift between a feature's
# point-estimate formula and the reducer FEATURE_STAT_FNS uses to replicate
# it during bootstrapping.

# Row layout must match features.COL_* constants.
_ROW_LEN = 13


def _row(rel, yaw, ang=10.0):
    row = [0.0] * _ROW_LEN
    row[features.COL_REL] = rel
    row[features.COL_YAW] = yaw
    row[features.COL_ANG] = ang
    return row


def _make_window(kill_idx, flick_deg):
    # 10 consecutive pre-kill frames (rel -9..0). A single big yaw jump on
    # the last step produces a flick of exactly `flick_deg` degrees (pitch
    # held at 0, so the combined angular step equals the yaw delta).
    rows = [_row(rel, 0.0) for rel in range(-9, 0)]
    rows.append(_row(0, flick_deg))
    return {"kill": kill_idx, "rows": rows}


def _make_fixture():
    p1, p2 = "P1", "P2"
    kills = [
        {"attacker_id": p1, "victim_id": p2, "tick": 100 + i * 100, "round": i + 1,
         "headshot": i < 3, "through_smoke": False, "penetrated": 0,
         "attacker_blind": False, "noscope": False, "weapon": "Glock"}
        for i in range(5)
    ]
    flicks = [10.0, 15.0, 20.0, 25.0, 30.0]
    windows = [_make_window(i, flicks[i]) for i in range(5)]
    return {
        "players": [{"steam_id": p1, "name": "Player One"},
                    {"steam_id": p2, "name": "Player Two"}],
        "kills": kills,
        "damage": [],
        "windows": windows,
        "tot_episodes": {},
        "occlusion": {},
        "first_sights": [],
        "tickrate": 64.0,
    }, p1, flicks


def test_feat_meta_proportion_matches_point_estimate():
    raw, p1, _ = _make_fixture()
    feats, extras, feat_meta = features.compute_features(raw)
    eq(len(feat_meta), 2, "feat_meta has one entry per player")

    meta = feat_meta[p1]["hs_rate"]
    eq(meta["kind"], "prop", "hs_rate is a proportion feature")
    succ, trials = meta["raw"]
    eq((succ, trials), (3, 5), "hs_rate raw (successes, trials)")
    approx(feats[p1]["hs_rate"], 100.0 * succ / trials, 1e-9,
           "hs_rate point estimate matches successes/trials")


def test_feat_meta_continuous_matches_stat_fn():
    raw, p1, flicks = _make_fixture()
    feats, extras, feat_meta = features.compute_features(raw)

    meta = feat_meta[p1]["flick_speed"]
    eq(meta["kind"], "cont", "flick_speed is a continuous feature")
    eq(sorted(meta["raw"]), flicks, "flick_speed raw values match engineered flicks")

    replicated = features.FEATURE_STAT_FNS["flick_speed"](meta["raw"])
    approx(replicated, feats[p1]["flick_speed"], 1e-9,
           "FEATURE_STAT_FNS reproduces the stored point estimate exactly")
    approx(feats[p1]["flick_speed"], 28.0, 1e-9,
           "flick_speed p90 of [10,15,20,25,30] is 28.0")


for _fn in list(globals().values()):
    if callable(_fn) and getattr(_fn, "__name__", "").startswith("test_"):
        _fn()

print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
