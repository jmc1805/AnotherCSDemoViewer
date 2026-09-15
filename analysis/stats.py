"""Generic small-sample statistics helpers: confidence intervals and
empirical-Bayes-style shrinkage. Pure stdlib, zero dependency on the rest of
analysis/ - features.py/baselines.py/overwatch.py import from here, never
the reverse.

These exist because Tier-1 features are often backed by very few events
(a handful of sniper kills, a dozen flashes) and reporting a bare point
estimate hides how little that number should be trusted.
"""

import math
import random
import zlib

Z_95 = 1.959963985


def wilson_interval(successes, n, z=Z_95):
    """Wilson score interval for a binomial proportion. Returns (lo, hi) as
    fractions in [0, 1], or None if n <= 0. Unlike a naive normal
    approximation, stays non-degenerate at successes=0 or successes=n."""
    if n <= 0:
        return None
    p = successes / n
    denom = 1.0 + z * z / n
    center = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    lo = max(0.0, (center - margin) / denom)
    hi = min(1.0, (center + margin) / denom)
    return lo, hi


def _percentile(sorted_vals, q):
    """Linear-interpolation percentile. Local copy of features._pctile so
    this module stays a zero-dependency leaf - keep the formula identical if
    features._pctile ever changes."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * q
    lo_i = int(math.floor(pos))
    hi_i = min(lo_i + 1, len(sorted_vals) - 1)
    frac = pos - lo_i
    return sorted_vals[lo_i] * (1 - frac) + sorted_vals[hi_i] * frac


def bootstrap_ci(values, stat_fn, n_boot=300, alpha=0.05, seed=None, min_valid=20):
    """Percentile-method bootstrap CI for an arbitrary statistic. `values`
    is the raw sample (a list - of floats, or of (x, y) pairs when stat_fn
    is a correlation, or of ints for a block-count sum; stat_fn owns
    interpretation). Resamples `values` with replacement `n_boot` times via
    a *local* RNG (never touches the global `random` state, so this can't
    perturb anything else in the process), applies stat_fn to each resample,
    and returns the (alpha/2, 1-alpha/2) percentiles of the surviving
    replicate distribution. Replicates where stat_fn returns None (e.g. a
    degenerate zero-variance resample) are dropped. Returns None if
    len(values) < 2 or fewer than min_valid replicates survive.

    n_boot defaults to 300, not the textbook 1000+: at these sample sizes
    (typically 5-50 events) precision is bottlenecked by the underlying
    sample, not by replicate count - going higher barely moves the bounds.
    """
    n = len(values)
    if n < 2:
        return None
    rng = random.Random(seed)
    reps = []
    for _ in range(n_boot):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        r = stat_fn(sample)
        if r is not None:
            reps.append(r)
    if len(reps) < min_valid:
        return None
    reps.sort()
    lo = _percentile(reps, alpha / 2)
    hi = _percentile(reps, 1 - alpha / 2)
    return lo, hi


def shrink_toward_prior(value, n, prior_mean, prior_n):
    """Empirical-Bayes-style shrinkage: weight = n / (n + prior_n). Pulls
    `value` toward `prior_mean` when the observation's own sample size n is
    small relative to the pseudo-count prior_n, and toward the raw `value`
    as n grows. Pass-through (returns value unchanged) if prior_mean, n, or
    n<=0 make shrinkage undefined."""
    if prior_mean is None or n is None or n <= 0:
        return value
    w = n / (n + prior_n)
    return w * value + (1 - w) * prior_mean


def stable_seed(*parts):
    """Deterministic integer seed from arbitrary string/number parts (e.g.
    match_id, steam_id, feature_key), so re-analyzing the same match
    reproduces byte-identical cached JSON. Uses zlib.crc32 rather than
    Python's built-in hash(), which is salted per-process for strings and
    would make bootstrap output nondeterministic across runs."""
    s = "|".join(str(p) for p in parts).encode("utf-8")
    return zlib.crc32(s)
