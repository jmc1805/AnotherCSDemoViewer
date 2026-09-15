"""Rank-banded population baselines for Tier-1 features.

Every analyzed match contributes its players' feature values to a local store
(analysis_data/baselines.json), keyed by feature and rank band. A player's
value is then scored as a percentile *in the suspicious direction* against
their band. Rank-conditioning is the core defense against flagging legit
high-skill players.

The store grows with every match you parse; with few matches the percentiles
are weak - n_baseline is reported so the UI can say so.
"""

import json
import math
import os
import threading

_LOCK = threading.Lock()

MIN_BAND_SAMPLES = 8     # fall back to the global pool below this
MIN_SAMPLES_FOR_PCT = 5  # report no percentile below this

# Premier rating buckets (rank_type 11).
_PREMIER_BUCKETS = [(0, 5000), (5000, 10000), (10000, 15000),
                    (15000, 20000), (20000, 25000), (25000, 10 ** 9)]


def _premier_band_key(lo, hi):
    return f"premier_{lo // 1000}k_{hi // 1000}k" if hi < 10 ** 9 \
        else f"premier_{lo // 1000}k_plus"


_PREMIER_BAND_KEYS = [_premier_band_key(lo, hi) for lo, hi in _PREMIER_BUCKETS]

# Competitive/Wingman skill groups (rank_type 12 / 7), 1-18. Real population
# is heavily right-skewed (Silver ~57%, top tiers ~2.4% combined per csstats.gg
# aggregates) so individual skill groups are kept as exact bands, with a
# named-tier fallback for the sparse top end rather than collapsing everyone
# into coarse buckets up front.
_SKILL_TIERS = [("silver", range(1, 7)), ("nova", range(7, 11)),
                ("guardian", range(11, 15)), ("elite", range(15, 19))]


def _skill_tier(n):
    for name, rng in _SKILL_TIERS:
        if n in rng:
            return name
    return None


def band_for_rank(rank_type, rank):
    """The baseline bucket a player belongs to.

    rank_type: int  -- 11 Premier, 12 Competitive, 7 Wingman
    rank: int       -- Premier rating, or skill-group number
    -> str          -- e.g. 'premier_15000_20000', 'mm_12', 'wingman_7',
                       or 'unbanded' when the rank is missing/unknown.

    Banding is non-negotiable for scoring: a global baseline just flags good
    players as anomalous.
    """
    if rank_type == 11 and rank and rank > 0:
        for lo, hi in _PREMIER_BUCKETS:
            if lo <= rank < hi:
                return _premier_band_key(lo, hi)
    elif rank_type == 12 and rank and rank > 0:
        return f"mm_{rank}"
    elif rank_type == 7 and rank and rank > 0:
        return f"wingman_{rank}"
    return "unbanded"


def _tier_siblings(band):
    """Bands belonging to the same named skill tier as `band` (fallback level
    1) - keeps fallback rank-monotonic instead of jumping straight to a pool
    dominated by whichever band has the most samples (Silver, in practice)."""
    for prefix in ("mm_", "wingman_"):
        if band.startswith(prefix):
            try:
                n = int(band[len(prefix):])
            except ValueError:
                return None
            tier = _skill_tier(n)
            if tier is None:
                return None
            tier_range = next(rng for name, rng in _SKILL_TIERS if name == tier)
            return [f"{prefix}{i}" for i in tier_range]
    if band in _PREMIER_BAND_KEYS:
        idx = _PREMIER_BAND_KEYS.index(band)
        tier = idx // 2
        return [k for i, k in enumerate(_PREMIER_BAND_KEYS) if i // 2 == tier]
    return None


def _mode_pool_bands(band, all_bands):
    """All stored bands sharing the same mode prefix as `band` (fallback
    level 2) - never mixes Premier/Competitive/Wingman samples together."""
    for prefix in ("mm_", "wingman_", "premier_"):
        if band.startswith(prefix):
            return [b for b in all_bands if b.startswith(prefix)]
    return None


class BaselineStore:
    """Accumulating population baselines, keyed by rank band then feature.

    Backed by one JSON file (`analysis_data/baselines.json` for the clean pool,
    or a sibling for the cheater/suspicious pools). Every match analysed adds
    its players' feature values, so percentiles sharpen as the corpus grows.
    Reads are cached in memory; writes are serialised by a module-level lock.
    """

    def __init__(self, path):
        self.path = path
        self._data = None

    def _load(self):
        if self._data is None:
            try:
                with open(self.path, encoding="utf-8") as f:
                    self._data = json.load(f)
            except (OSError, ValueError):
                self._data = {"samples": {}}
        return self._data

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f)
        os.replace(tmp, self.path)

    def update_match(self, match_id, player_rows):
        """player_rows: [(steamid, band, {feature: value})]. Idempotent per
        match - re-analyzing replaces that match's samples."""
        with _LOCK:
            data = self._load()
            samples = data["samples"]
            for feat_samples in samples.values():
                for band_list in feat_samples.values():
                    band_list[:] = [s for s in band_list if s["m"] != match_id]
            for pid, band, feats in player_rows:
                for key, val in feats.items():
                    if key == "n_kills" or val is None:
                        continue
                    samples.setdefault(key, {}).setdefault(band, []).append(
                        {"m": match_id, "p": pid, "v": val})
            self._save()

    def _pool(self, feature, band, exclude_ids=None):
        """Return the sample pool for a feature, cascading from the exact
        band out to wider pools only as needed: exact band -> named skill
        tier -> same-mode pool (Premier/Competitive/Wingman kept separate)
        -> fully flat (every band, last resort). Keeps fallback rank-monotonic
        instead of jumping straight to a pool dominated by whichever band has
        the most samples (Silver, in practice, given the real population is
        ~57% Silver vs ~2.4% combined top tiers)."""
        data = self._load()
        feat = data["samples"].get(feature, {})
        pool = feat.get(band, [])
        if len(pool) < MIN_BAND_SAMPLES:
            siblings = _tier_siblings(band)
            if siblings:
                pool = [s for b in siblings for s in feat.get(b, [])]
        if len(pool) < MIN_BAND_SAMPLES:
            mode_bands = _mode_pool_bands(band, feat.keys())
            if mode_bands:
                pool = [s for b in mode_bands for s in feat.get(b, [])]
        if len(pool) < MIN_BAND_SAMPLES:
            pool = [s for lst in feat.values() for s in lst]
        if exclude_ids:
            pool = [s for s in pool if s["p"] not in exclude_ids]
        return pool

    def percentile(self, feature, band, value, direction, exclude_ids=None):
        """Percentile of `value` in the suspicious `direction` against the
        band's samples (global pool fallback). Returns (pct, n) or (None, n).
        exclude_ids: set of steam_id strings to omit (e.g. confirmed cheaters)."""
        with _LOCK:
            pool = self._pool(feature, band, exclude_ids)
        vals = [s["v"] for s in pool]
        n = len(vals)
        if n < MIN_SAMPLES_FOR_PCT:
            return None, n
        if direction == "high":
            below = sum(1 for v in vals if v < value)
            eq = sum(1 for v in vals if v == value)
        else:
            below = sum(1 for v in vals if v > value)
            eq = sum(1 for v in vals if v == value)
        return 100.0 * (below + 0.5 * eq) / n, n

    def feature_stats(self, feature, band, exclude_ids=None):
        """Returns (mean, std, n) for the feature pool. Used for Option 2
        composite z-score - mean/std of the *clean* baseline distribution."""
        with _LOCK:
            pool = self._pool(feature, band, exclude_ids)
        vals = [s["v"] for s in pool]
        n = len(vals)
        if n < MIN_SAMPLES_FOR_PCT:
            return None, None, n
        mean = sum(vals) / n
        std = math.sqrt(sum((v - mean) ** 2 for v in vals) / n)
        return mean, std, n

    def player_samples(self, feature, steam_id, exclude_matches=None):
        """All of one player's own raw samples for `feature` across every
        match currently in the store, regardless of band - a player's rank
        can drift across matches, and pooling their own history is meant to
        grow n for *this player*, not to re-segment by skill. Returns
        (values, n, matches) where matches is the sorted set of match_ids
        contributing, so callers can label "pooled over N matches."
        exclude_matches: optional set of match_ids to omit - e.g. the match
        currently being scored, so its own value isn't pooled against
        itself and "pooled" stays independent corroboration."""
        with _LOCK:
            data = self._load()
            feat = data["samples"].get(feature, {})
        vals, matches = [], set()
        for band_list in feat.values():
            for s in band_list:
                if s["p"] == steam_id and (not exclude_matches or s["m"] not in exclude_matches):
                    vals.append(s["v"])
                    matches.add(s["m"])
        return vals, len(vals), sorted(matches)

    def unique_player_count(self):
        """Count distinct steam_ids stored across all features and bands."""
        with _LOCK:
            data = self._load()
            return len({s["p"]
                        for feat in data["samples"].values()
                        for lst in feat.values()
                        for s in lst})
