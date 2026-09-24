"""Flask application: every HTTP route, and the background jobs behind them.

This is the only web layer. It owns routing, request/response shaping, the
job registries for long-running work, and the caches that sit beside each
match file - and delegates everything else:

    parsing        cmd/parser (subprocess)      demo -> match JSON + tick chunks
    analytics      analysis/*                   moments, query, highlights,
                                                utility rating, Overwatch
    CS2 control    clip_record.py               watch mode + clip recording
    paths          paths.py                     never build a data path here
    map names      maps.py                      canonicalise before grouping
    JSON I/O       jsonio.py                    brotli-at-rest, .br-transparent

Route groups, in file order: match list + upload, players, highlights,
settings (incl. asset extraction and manual import), viewer/match/analyser
pages, data + chunk serving, Overwatch, clips and watch mode.

Four conventions this file must keep:

  1. Find match files with _list_match_files()/_match_path(), never
     os.listdir for '.json'. They know about .br and about _CACHE_SUFFIXES.
  2. A new derived cache MUST be added to _CACHE_SUFFIXES, or it registers as
     a phantom match across the dashboard, players rollup and analyser.
  3. Expensive derived data is computed lazily on first request and cached
     beside the match, guarded on source mtime plus a schema constant.
     Deleting a cache file is always safe.
  4. clip_url is re-attached per request and never cached, so a clip recorded
     after a cache filled is still visible.

Long-running work (parse, extract, record) runs in a daemon thread against an
in-memory job dict and is polled by the client through a /status/<job_id>
route; nothing here blocks a request on a subprocess.
"""
from flask import Flask, render_template, request, redirect, url_for, send_from_directory, abort
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta, timezone
import brotli
import json
import os
import re
import shutil
import statistics
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile

# ── Paths ─────────────────────────────────────────────────────────────────────
# All on-disk locations come from paths.py (the single source of truth). The
# FOLDER/EXE aliases below keep the rest of this file unchanged; paths.py is
# what decides data/ vs bin/ vs static/.

import paths
import procutil  # NO_WINDOW for helper subprocesses (the desktop build has no console)
import assetimport   # manual UI-icon import (the alternative to the VPK extractor)
import assetindex    # Premier tint + asset manifest, in-process (no Node)
import mapzone_extract  # env_cs_place callout regions, in-process (no Node)
import maps     # canonical map names + display labels (mirrored by static/maps.logic.js)
import jsonio   # brotli-at-rest JSON (match JSON + overwatch raw are stored .json.br)
import match_summary   # pure computeSummary twin
import clip_record     # real in-game CS2 clip recording (cs-demo-manager/HLAE style)
from analysis import moments, query   # moment index + cross-match query engine

_HERE = paths.ROOT
DATA_DIR = paths.DATA_DIR

# Shown on the Settings page. Worth stating in a bug report, because the parser
# and the analysis caches are versioned separately (MOMENTS_SCHEMA,
# SUMMARY_SCHEMA, chunkFormat) and "which build wrote this data" is otherwise
# only answerable from git.
__version__ = '1.0.0'

# Compiled Go binaries (bin/, root fallback for the Docker image).
_PARSER_EXE    = paths.PARSER_BIN
_OVERWATCH_EXE = paths.OVERWATCH_BIN

# Generated-data dirs (all under DATA_DIR).
PROCESSED_FOLDER     = paths.PROCESSED_DIR
UPLOAD_FOLDER        = paths.DEMOS_DIR
CHUNKS_FOLDER        = paths.CHUNKS_DIR
OVERWATCH_RAW_FOLDER = paths.OVERWATCH_RAW_DIR
ANALYSIS_DATA_FOLDER = paths.ANALYSIS_DATA_DIR
CLIPS_FOLDER         = paths.CLIPS_DIR
DEMO_MAP_PATH        = paths.DEMO_MAP_PATH
LABELS_PATH      = os.path.join(ANALYSIS_DATA_FOLDER, 'labels.json')
PRO_MATCHES_PATH = os.path.join(ANALYSIS_DATA_FOLDER, 'pro_matches.json')
# Dashboard summary index + per-match tags/comments.
MATCH_INDEX_PATH = os.path.join(ANALYSIS_DATA_FOLDER, 'match_index.json')
MATCH_META_PATH  = os.path.join(ANALYSIS_DATA_FOLDER, 'match_meta.json')

paths.ensure_dirs()

# 2D radar overview PNGs are read from the extracted overheadmaps assets
# (static/assets/overheadmaps/, Valve's `<map>_radar_psd.png` names). Valve content,
# never generated, so a missing/empty dir means the 2D viewer has no radar images;
# warn loudly at startup.
_MAP_IMG_DIR = paths.MAP_IMG_DIR
if not os.path.isdir(_MAP_IMG_DIR) or not os.listdir(_MAP_IMG_DIR):
    print(f'WARNING: no radar images found in {_MAP_IMG_DIR} - the 2D viewer needs '
          f'<map>_radar_psd.png per map. Extract them from CS2 or import them on the '
          f'Settings page (see README "Map images").')

# Job store for async demo processing (non-blocking uploads).
# job_id → {'state': 'pending'|'processing'|'done'|'error', 'match': str|None,
#           'error': str|None, 'stage': str|None, 'progress': float, 'eta': float|None}
_JOBS: dict = {}
_JOBS_LOCK       = threading.Lock()
_DEMO_MAP_LOCK   = threading.Lock()
_LABELS_LOCK     = threading.Lock()
_PRO_MATCHES_LOCK = threading.Lock()
_MATCH_INDEX_LOCK = threading.Lock()
_MATCH_META_LOCK  = threading.Lock()

# Job store for async real-clip recording (clip_record.py) - same shape/
# pattern as _JOBS above, kept separate since a clip job's lifetime and
# failure modes (needs a local CS2/HLAE/ffmpeg install) are unrelated to
# demo-processing jobs.
# job_id → {'state': 'pending'|'processing'|'done'|'error', 'clip_url': str|None,
#            'message': str|None, 'error': str|None}
_CLIP_JOBS: dict = {}
_CLIP_JOBS_LOCK = threading.Lock()

def _load_demo_map():
    try:
        with open(DEMO_MAP_PATH, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_demo_map(demo_map):
    # Atomic like every other save helper: a truncated demo_map.json unlinks
    # every match from its original .dem (watch mode, clips, Overwatch).
    tmp = DEMO_MAP_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(demo_map, f, indent=1)
    os.replace(tmp, DEMO_MAP_PATH)


def _load_labels():
    """Return {steam_id: {label, name, updated}} from analysis_data/labels.json."""
    try:
        with open(LABELS_PATH, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_labels(labels):
    os.makedirs(ANALYSIS_DATA_FOLDER, exist_ok=True)
    tmp = LABELS_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(labels, f, indent=2)
    os.replace(tmp, LABELS_PATH)


def _load_pro_matches():
    """Return set of match_ids (no .json suffix) flagged as professional."""
    try:
        with open(PRO_MATCHES_PATH, encoding='utf-8') as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return set()


def _save_pro_matches(pro_matches):
    os.makedirs(ANALYSIS_DATA_FOLDER, exist_ok=True)
    tmp = PRO_MATCHES_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(sorted(pro_matches), f, indent=2)
    os.replace(tmp, PRO_MATCHES_PATH)


# ── Per-match metadata: tags + comments (dashboard_plan.md §4, Phase 5) ────────

def _load_match_meta():
    """Return {match_file: {tags:[...], comment:"..."}} from match_meta.json."""
    try:
        with open(MATCH_META_PATH, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_match_meta(meta):
    os.makedirs(ANALYSIS_DATA_FOLDER, exist_ok=True)
    tmp = MATCH_META_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(meta, f, indent=2)
    os.replace(tmp, MATCH_META_PATH)


# ── Dashboard summary index (the load-time fix, dashboard_plan.md §4) ──────────
# One precomputed row per match (score, top-fraggers, duration, overwatch flags,
# average rank, size) so the dashboard loads ONE small JSON instead of N large
# .json.br files. Bump SUMMARY_SCHEMA whenever the entry shape changes - entries
# from an older schema are treated as stale and rebuilt on the next read.
SUMMARY_SCHEMA = 4
# Invalidation is mtime-based and self-healing: an entry is rebuilt whenever the
# match's processed mtime or its overwatch cache mtime changes, and pruned when
# the match is gone. Written eagerly at parse time; backfilled lazily on read.

def _overwatch_flag_count(stem):
    """(flags, cache_mtime) for a match - flags = players at level 'high',
    or (None, 0) when no overwatch analysis has been cached yet."""
    cache = os.path.join(PROCESSED_FOLDER, stem + '.overwatch.json')
    if not os.path.isfile(cache):
        return None, 0
    try:
        with open(cache, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None, 0
    flags = sum(1 for p in data.get('players', []) if p.get('level') == 'high')
    try:
        mtime = int(os.path.getmtime(cache))
    except OSError:
        mtime = 0
    return flags, mtime


def _build_summary_entry(meta):
    """Compute the summary row for one match from its full JSON + overwatch cache.
    `meta` is a `_match_meta()` dict. Returns None if the match JSON can't load."""
    real = _match_path(meta['file'])
    if not real:
        return None
    try:
        data = jsonio.load(os.path.join(PROCESSED_FOLDER, meta['file']))
    except (OSError, ValueError):
        return None
    try:
        size_bytes = os.path.getsize(real)
    except OSError:
        size_bytes = 0

    stats = match_summary.compute_summary_stats(data)
    rank = match_summary.avg_rank(data)
    flags, ow_mtime = _overwatch_flag_count(meta['stem'])
    top = lambda team: [{'name': p['name'], 'kills': p['kills']} for p in team]

    return {
        '_schema':         SUMMARY_SCHEMA,
        'file':            meta['file'],
        'stem':            meta['stem'],
        'map':             meta['map'],
        'mode':            meta['mode'],
        # What the mode is DISPLAYED as. `mode` stays the filename's own token
        # (the uploader's label, 'mm' by default); `modeLabel` prefers what the
        # demo itself proves - a Premier rating means a Premier match, however
        # the file was named. See match_summary.mode_label().
        'modeLabel':       match_summary.mode_label(data, meta['mode']),
        'demo':            meta['demo'],
        'played_epoch':    meta['played_epoch'],
        'played_label':    meta['played_label'],
        'processed_epoch': meta['processed_epoch'],
        'processed_label': meta['processed_label'],
        'scoreCT':         stats['scoreCT'],
        'scoreT':          stats['scoreT'],
        'topCT':           top(stats['team1']),
        'topT':            top(stats['team2']),
        'durationTicks':   stats['durationTicks'],
        'killsTotal':      stats['killsTotal'],
        'roundsTotal':     stats['rounds'],
        'avgRank':         rank['avg'],
        'rankType':        rank['rankType'],
        'rankLabel':       rank['label'],
        'rankSort':        rank['sort'],
        'rankedPlayers':   rank['n'],
        'overwatchFlags':  flags,
        'overwatch_mtime': ow_mtime,
        'sizeBytes':       size_bytes,
    }


def _load_match_index():
    try:
        with open(MATCH_INDEX_PATH, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_match_index(index):
    os.makedirs(ANALYSIS_DATA_FOLDER, exist_ok=True)
    tmp = MATCH_INDEX_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(index, f)
    os.replace(tmp, MATCH_INDEX_PATH)


def _summary_entry_is_stale(entry, meta):
    """An index entry must be rebuilt when the match was re-processed or its
    overwatch analysis changed since the entry was cached."""
    if not entry:
        return True
    if entry.get('_schema') != SUMMARY_SCHEMA:
        return True
    if entry.get('processed_epoch') != meta['processed_epoch']:
        return True
    _, ow_mtime = _overwatch_flag_count(meta['stem'])
    return entry.get('overwatch_mtime', 0) != ow_mtime


def _match_summary_index(files=None):
    """Return the list of summary rows for all (or the given) matches, rebuilding
    stale/missing entries and pruning deleted ones. Self-healing on every call."""
    demo_lookup = _demo_lookup()
    demo_map    = _load_demo_map()
    names = files if files is not None else _list_match_files()

    with _MATCH_INDEX_LOCK:
        index = _load_match_index()
        dirty = False
        current = set()
        rows = []
        for fname in names:
            meta = _match_meta(fname, demo_lookup, demo_map)
            current.add(fname)
            entry = index.get(fname)
            if _summary_entry_is_stale(entry, meta):
                entry = _build_summary_entry(meta)
                if entry is None:
                    continue
                index[fname] = entry
                dirty = True
            rows.append(entry)

        # Prune entries for matches that no longer exist (only on a full sweep).
        if files is None:
            for stale in [k for k in index if k not in current]:
                del index[stale]
                dirty = True

        if dirty:
            _save_match_index(index)

    return rows


def _update_match_index(fname):
    """Eagerly (re)build one match's summary entry - called at parse time so the
    first dashboard load after an upload doesn't pay the full-JSON recompute."""
    try:
        meta = _match_meta(fname, _demo_lookup(), _load_demo_map())
        entry = _build_summary_entry(meta)
    except Exception:
        return
    if entry is None:
        return
    with _MATCH_INDEX_LOCK:
        index = _load_match_index()
        index[fname] = entry
        _save_match_index(index)


# ── Players aggregate (dashboard_plan.md Phase 6) ─────────────────────────────
# Per-name rollup across the whole corpus: matches, K/D/A, top maps. Rosters are
# cached per-match (mtime-keyed) in players_index.json so unchanged matches are
# not re-read; the aggregate itself is recomputed cheaply on each request.
PLAYERS_INDEX_PATH = os.path.join(ANALYSIS_DATA_FOLDER, 'players_index.json')
# Bump when the per-match row shape changes so cached entries are rebuilt even
# though the match's processed_epoch is unchanged (ui_feature_expansion_plan §6).
PLAYERS_INDEX_VERSION = 4  # bumped for kast_rounds (cross-match Rating 2.0)


def _players_index():
    """Return {match_file: {processed_epoch, map, rows:[{name,kills,deaths,assists}]}},
    rebuilding stale/missing per-match rosters and pruning deleted matches."""
    demo_lookup = _demo_lookup()
    demo_map    = _load_demo_map()
    labels      = _load_labels()
    pro_matches = _load_pro_matches()
    with _MATCH_INDEX_LOCK:
        try:
            with open(PLAYERS_INDEX_PATH, encoding='utf-8') as f:
                index = json.load(f)
        except (OSError, ValueError):
            index = {}
        dirty = False
        current = set()
        for fname in _list_match_files():
            current.add(fname)
            meta = _match_meta(fname, demo_lookup, demo_map)
            entry = index.get(fname)
            if (not entry or entry.get('processed_epoch') != meta['processed_epoch']
                    or entry.get('v') != PLAYERS_INDEX_VERSION):
                try:
                    data = jsonio.load(os.path.join(PROCESSED_FOLDER, fname))
                except (OSError, ValueError):
                    continue
                rows = match_summary.compute_player_profile_rows(data)
                match_id = fname[:-len('.json')] if fname.endswith('.json') else fname
                try:
                    from analysis.utility_rating import analyze as _analyze_utility
                    util = _analyze_utility(match_id, PROCESSED_FOLDER, ANALYSIS_DATA_FOLDER,
                                             force=False, labels=labels,
                                             is_pro_match=match_id in pro_matches)
                    util_by_name = {p['name']: p.get('combined') for p in util.get('players', [])}
                except Exception:
                    util_by_name = {}
                for r in rows:
                    r['utilCombined'] = util_by_name.get(r.get('name'))
                index[fname] = {
                    'v':      PLAYERS_INDEX_VERSION,
                    'processed_epoch': meta['processed_epoch'],
                    'map':    meta['map'],
                    'rounds': len(data.get('rounds') or []),
                    'rows':   rows,
                }
                dirty = True
        for stale in [k for k in index if k not in current]:
            del index[stale]
            dirty = True
        if dirty:
            os.makedirs(ANALYSIS_DATA_FOLDER, exist_ok=True)
            tmp = PLAYERS_INDEX_PATH + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(index, f)
            os.replace(tmp, PLAYERS_INDEX_PATH)
    return index


def _players_aggregate(index=None):
    """Roll the per-match profile rosters up into one full profile per player name
    (ui_feature_expansion_plan.md §6). Keeps `files` (which demos a player appears
    in) alongside the new aggregate stats, maps W/L, weapons+accuracy, teammates
    and latest rank. All name-keyed, consistent with the rest of the page."""
    if index is None:
        index = _players_index()
    agg = {}

    def blank(name):
        return {
            'name': name, 'steam_id': None, 'matches': 0,
            'kills': 0, 'deaths': 0, 'assists': 0, 'hs': 0, 'dmg': 0, 'rounds': 0,
            'kast_rounds': 0,
            'util_num': 0.0, 'util_rounds': 0,
            'wins': 0, 'losses': 0,
            'maps': {},       # map -> [n, wins, losses]
            'map_rank': {},   # map -> {'rank_type', 'rank', 'epoch'} (newest match on that map)
            'weapons': {},    # weapon -> [shots, hits, kills]
            'mates': {},      # teammate name -> [games, wins]
            'rank': None, 'rankEpoch': -1,
            'files': [],
        }

    for fname, entry in index.items():
        mp = entry.get('map', 'unknown')
        rounds = entry.get('rounds', 0) or 0
        epoch = entry.get('processed_epoch', 0) or 0
        rows = entry.get('rows', [])
        # team -> [names] for teammate pairing
        teams = {}
        for r in rows:
            if r.get('name') and r.get('team'):
                teams.setdefault(r['team'], []).append(r)

        for r in rows:
            name = r.get('name')
            if not name:
                continue
            a = agg.get(name) or agg.setdefault(name, blank(name))
            a['matches'] += 1
            a['kills']   += r.get('kills', 0)
            a['deaths']  += r.get('deaths', 0)
            a['assists'] += r.get('assists', 0)
            a['hs']      += r.get('hs', 0)
            a['dmg']     += r.get('dmg', 0)
            a['rounds']  += rounds
            a['kast_rounds'] += r.get('kast_rounds', 0)
            util_combined = r.get('utilCombined')
            if util_combined is not None and rounds:
                a['util_num']    += util_combined * rounds
                a['util_rounds'] += rounds
            a['files'].append(fname)
            if r.get('steam_id') and not a['steam_id']:
                a['steam_id'] = r['steam_id']
            won = r.get('win')
            if won is True:
                a['wins'] += 1
            elif won is False:
                a['losses'] += 1
            # maps
            m = a['maps'].setdefault(mp, [0, 0, 0])
            m[0] += 1
            if won is True:
                m[1] += 1
            elif won is False:
                m[2] += 1
            # per-map rank, newest match on that map wins (same epoch>= rule
            # as the overall 'latest rank' below, just keyed by map too)
            if r.get('rank') and epoch >= a['map_rank'].get(mp, {}).get('epoch', -1):
                a['map_rank'][mp] = {'rank_type': r.get('rank_type'), 'rank': r.get('rank'), 'epoch': epoch}
            # weapons
            for w, (sh, hi, ki) in (r.get('weapons') or {}).items():
                acc = a['weapons'].setdefault(w, [0, 0, 0])
                acc[0] += sh; acc[1] += hi; acc[2] += ki
            # latest rank
            if r.get('rank') and epoch >= a['rankEpoch']:
                a['rankEpoch'] = epoch
                a['rank'] = {'rank_type': r.get('rank_type'), 'rank': r.get('rank')}
            # teammates (same team, same match)
            for mate in teams.get(r.get('team'), []):
                if mate is r or not mate.get('name'):
                    continue
                mt = a['mates'].setdefault(mate['name'], [0, 0])
                mt[0] += 1
                if won is True:
                    mt[1] += 1

    out = []
    for a in agg.values():
        top_maps = sorted(a['maps'].items(), key=lambda kv: -kv[1][0])
        decided = a['wins'] + a['losses']
        weapons = sorted(
            ({'weapon': w, 'shots': v[0], 'hits': v[1], 'kills': v[2],
              'acc': round(100 * v[1] / v[0], 1) if v[0] else 0} for w, v in a['weapons'].items()),
            key=lambda x: (-x['kills'], -x['shots']))
        mates = sorted(
            ({'name': n, 'games': g, 'wins': w, 'winPct': round(100 * w / g) if g else 0}
             for n, (g, w) in a['mates'].items()),
            key=lambda x: (-x['games'], -x['winPct']))
        # HLTV-2.0-style Rating, ported from templates/match.html's per-match
        # client JS (KAST + Rating 2.0 regression against real HLTV ratings).
        # Every term is a per-round rate (kills/rounds, dmg/rounds, ...), so
        # evaluating the formula once on the player's SUMMED totals is exactly
        # a rounds-weighted average of what each individual match's own
        # Rating would have been - no need to store or average a per-match
        # value separately.
        rating = None
        if a['rounds']:
            kpr = a['kills'] / a['rounds']
            dpr = a['deaths'] / a['rounds']
            apr = a['assists'] / a['rounds']
            kast = 100 * a['kast_rounds'] / a['rounds']
            adr = a['dmg'] / a['rounds']
            impact = 2.13 * kpr + 0.42 * apr - 0.41
            rating = round(max(0.01, 0.0073 * kast + 0.3591 * kpr - 0.5329 * dpr
                                + 0.2372 * impact + 0.0032 * adr + 0.1587), 2)
        out.append({
            'name':     a['name'],
            'steam_id': a['steam_id'],
            'matches':  a['matches'],
            'kills':    a['kills'],
            'deaths':   a['deaths'],
            'assists':  a['assists'],
            'kd':       round(a['kills'] / a['deaths'], 2) if a['deaths'] else float(a['kills']),
            'adr':      round(a['dmg'] / a['rounds'], 1) if a['rounds'] else 0,
            'hsPct':    round(100 * a['hs'] / a['kills'], 1) if a['kills'] else 0,
            'utilRating': round(a['util_num'] / a['util_rounds'], 1) if a['util_rounds'] else None,
            'rating':   rating,
            'wins':     a['wins'],
            'losses':   a['losses'],
            'winPct':   round(100 * a['wins'] / decided) if decided else 0,
            'rank':     a['rank'],
            'rankLabel': (match_summary.rank_label(a['rank']['rank_type'], a['rank']['rank'])
                          if a['rank'] else None),
            'topMaps':  [{'map': m, 'n': v[0]} for m, v in top_maps[:3]],
            'maps':     [{'map': m, 'n': v[0], 'wins': v[1], 'losses': v[2],
                          'winPct': round(100 * v[1] / (v[1] + v[2])) if (v[1] + v[2]) else 0,
                          'rank': a['map_rank'].get(m),
                          'rankLabel': (match_summary.rank_label(a['map_rank'][m]['rank_type'], a['map_rank'][m]['rank'])
                                        if a['map_rank'].get(m) else None)}
                         for m, v in top_maps],
            'weapons':  weapons[:10],
            'teammates': mates[:8],
            'files':    a['files'],
        })
    out.sort(key=lambda p: (-p['matches'], -p['kills']))
    return out


# ── App ───────────────────────────────────────────────────────────────────────

# Keep both folders explicit and from paths.py: in the frozen build they live in
# the bundle, and Flask's own derivation from __name__ would not follow that.
app = Flask(__name__,
            static_folder=paths.STATIC_DIR,
            template_folder=paths.TEMPLATE_DIR)

# `{{ 'de_dust2'|map_label }}` → "Dust 2". Registered as a filter so a template
# can render a map name without every one of them growing its own
# `.replace('de_', '')`, which is how Dust II ended up written four different
# ways across the pages.
app.jinja_env.filters['map_label'] = maps.label


# ── Parse progress ────────────────────────────────────────────────────────────
#
# The dashboard draws one bar per uploaded demo, so a job carries an overall
# fraction, the stage it is in and an ETA. The parser reports its own stage
# fractions on stdout ("PROGRESS <stage> <0..1>", see cmd/parser/main.go); this
# side weights them into one number and adds its own post-parse work.
#
# THE WEIGHTS ARE MEASURED, NOT PICKED. Three corpus demos (186 MB, 293 MB,
# 345 MB) each split the parser's wall time 68% / 30% / 2% across parse /
# chunk-writing / JSON encode, stable to a couple of points across all three;
# the app's own read + brotli + move costs about another 4% on top. A bar that
# sits at 90% for a third of the run is the failure mode these avoid.
_PARSE_STAGE_WEIGHTS = (
    ('parse',  0.66),   # reading the .dem, by bytes consumed
    ('chunks', 0.28),   # writing the v2 tick chunks, per chunk
    ('encode', 0.02),   # the parser's master-JSON write
    ('save',   0.04),   # this process: json.load, brotli, move chunks into place
)

# Below this fraction an ETA is extrapolated from too little of the run to mean
# anything, so none is shown rather than a wild one.
_ETA_MIN_FRACTION = 0.04
# The ETA is smoothed, or it jitters by seconds between polls as the rate moves.
_ETA_SMOOTHING = 0.3


def _overall_progress(stage, frac):
    """Weighted 0..1 across the whole pipeline for `frac` through `stage`.

    An unknown stage returns None, so a parser that grows a stage this table has
    not learned yet leaves the bar where it is instead of jumping backwards."""
    done = 0.0
    for name, weight in _PARSE_STAGE_WEIGHTS:
        if name == stage:
            return done + weight * max(0.0, min(1.0, frac))
        done += weight
    return None


def _set_job_progress(job_id, stage, frac):
    """Record a stage fraction on a parse job and re-derive its ETA."""
    overall = _overall_progress(stage, frac)
    if overall is None:
        return
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        started = job.get('_started')
        # Monotonic: a stage that restarts its own count must not walk the bar
        # back, which reads as the parse having failed and begun again.
        job['progress'] = max(job.get('progress') or 0.0, overall)
        job['stage']    = stage
        elapsed = time.monotonic() - started if started else 0.0
        if overall >= _ETA_MIN_FRACTION and elapsed > 0:
            eta = elapsed * (1.0 - overall) / overall
            prev = job.get('eta')
            job['eta'] = eta if prev is None else prev + _ETA_SMOOTHING * (eta - prev)


# The parser has always been given 300 s; streaming its output means the wait is
# no longer subprocess.run's, so the limit is enforced by a watchdog that kills
# it. Without one a parser that hangs blocks its thread forever and the row it
# owns sits at whatever fraction it last reported.
_PARSE_TIMEOUT = 300


def _run_parser(argv, job_id):
    """Run the Go parser, feeding its PROGRESS lines into the job store.

    -> (exit_code, stderr). Raises RuntimeError on timeout.

    stderr is drained on its own thread: it is only read on failure, but a
    parser that filled the pipe while nobody read it would deadlock against the
    stdout loop below.
    """
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1, **procutil.NO_WINDOW)
    stderr_parts = []

    def drain_stderr():
        try:
            stderr_parts.append(proc.stderr.read() or '')
        except Exception:
            pass

    err_thread = threading.Thread(target=drain_stderr, daemon=True)
    err_thread.start()

    timed_out = threading.Event()

    def on_timeout():
        timed_out.set()
        proc.kill()

    watchdog = threading.Timer(_PARSE_TIMEOUT, on_timeout)
    watchdog.start()
    try:
        for line in proc.stdout:
            if not line.startswith('PROGRESS '):
                continue
            parts = line.split()
            if len(parts) != 3:
                continue
            try:
                _set_job_progress(job_id, parts[1], float(parts[2]))
            except ValueError:
                pass
        code = proc.wait()
    finally:
        watchdog.cancel()
        # A read that blew up would otherwise leave the parser running with
        # nobody waiting on it - its next write fills the pipe and it hangs.
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        err_thread.join(timeout=5)

    if timed_out.is_set():
        raise RuntimeError(f'Parser timed out after {_PARSE_TIMEOUT}s')
    return code, ''.join(stderr_parts)


def _parse_worker(job_id, demo_path, demo_filename):
    """Run the Go parser in a background thread and update the job store.

    The parser's stdout is read line by line rather than collected at the end,
    because its PROGRESS lines are what drives the dashboard's bar."""
    with _JOBS_LOCK:
        _JOBS[job_id].update(state='processing', stage='parse',
                             progress=0.0, _started=time.monotonic())

    match_id = None
    error    = None

    with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as tmp:
        tmp_json = tmp.name

    tmp_chunks = tmp_json + '_chunks'
    try:
        os.makedirs(tmp_chunks, exist_ok=True)

        # v2 binary chunks are the only tick format: every page reads them
        # through static/chunks.js. -no-json-chunks skips the legacy JSON tick
        # writer, which produced the same data ~100x larger on disk; the parser
        # rejects that flag unless -chunks-v2 is given too.
        parser_args = [_PARSER_EXE,
                       '-demo',   demo_path,
                       '-out',    tmp_json,
                       '-chunks', tmp_chunks,
                       '-chunks-v2', '-no-json-chunks']
        code, stderr = _run_parser(parser_args, job_id)
        if code != 0:
            raise RuntimeError(f"Parser failed (exit {code}):\n{stderr}")

        _set_job_progress(job_id, 'save', 0.0)
        with open(tmp_json, encoding='utf-8') as f:
            data = json.load(f)

        map_name  = data.get('mapName',   'unknown')
        mode      = data.get('mode',      'mm')
        timestamp = data.get('timestamp', '00000000_0000')

        with _DEMO_MAP_LOCK:
            demo_map = _load_demo_map()

            match_id = f"{map_name}_{timestamp}_{mode}"
            while (jsonio.resolve(os.path.join(PROCESSED_FOLDER, f"{match_id}.json"))
                   and demo_map.get(f"{match_id}.json") != demo_filename):
                ts = datetime.strptime(timestamp, '%Y%m%d_%H%M') + timedelta(minutes=1)
                timestamp = ts.strftime('%Y%m%d_%H%M')
                match_id = f"{map_name}_{timestamp}_{mode}"

            chunks_dir = os.path.join(CHUNKS_FOLDER, match_id)
            if os.path.exists(chunks_dir):
                shutil.rmtree(chunks_dir)
            # shutil.move, not os.rename: the temp dir can live on another
            # filesystem than the data dir (os.rename raises EXDEV there).
            shutil.move(tmp_chunks, chunks_dir)
            tmp_chunks = None  # renamed; don't clean up in finally

            for entry in data.get('chunkIndex', []) + data.get('chunkIndexV2', []):
                fname = os.path.basename(entry['file'])
                entry['file'] = f'/chunks/{match_id}/{fname}'

            data.pop('mode',      None)
            data.pop('timestamp', None)

            _set_job_progress(job_id, 'save', 0.4)
            jsonio.dump_br(data, os.path.join(PROCESSED_FOLDER, f"{match_id}.json.br"))
            _set_job_progress(job_id, 'save', 0.9)

            demo_map[f"{match_id}.json"] = demo_filename
            _save_demo_map(demo_map)

    except Exception as e:
        error = str(e)
    finally:
        if os.path.exists(tmp_json):
            os.unlink(tmp_json)
        if tmp_chunks and os.path.exists(tmp_chunks):
            shutil.rmtree(tmp_chunks, ignore_errors=True)

    with _JOBS_LOCK:
        if error:
            _JOBS[job_id].update({'state': 'error', 'error': error})
        else:
            _JOBS[job_id].update({'state': 'done', 'match': f'{match_id}.json',
                                  'stage': 'done', 'progress': 1.0, 'eta': 0})

    if not error and match_id:
        _update_match_index(f'{match_id}.json')

    if not error and os.path.isfile(_OVERWATCH_EXE):
        threading.Thread(
            target=_run_overwatch_background,
            args=(match_id, demo_path),
            daemon=True,
        ).start()

    if not error and match_id:
        threading.Thread(
            target=_run_utility_rating_background,
            args=(match_id,),
            daemon=True,
        ).start()


def _clip_worker(job_id, match_id, dem_path, start_tick, end_tick, focus_player):
    """Run clip_record.record_clip() in a background thread - same fire-and-
    poll shape as _parse_worker/_JOBS. Almost always ends in a clear 'not
    configured' error unless this machine has a real CS2 + HLAE + ffmpeg
    install pointed to from Settings (see clip_record.py's module docstring)."""
    with _CLIP_JOBS_LOCK:
        _CLIP_JOBS[job_id]['state'] = 'processing'

    def progress(msg):
        with _CLIP_JOBS_LOCK:
            _CLIP_JOBS[job_id]['message'] = msg

    try:
        result = clip_record.record_clip(
            match_id, dem_path, start_tick, end_tick, focus_player, on_progress=progress)
        with _CLIP_JOBS_LOCK:
            _CLIP_JOBS[job_id].update(
                state='done',
                clip_url=f"/clips/{match_id}/{result['clip_file']}",
                warning=result['warning'])
    except clip_record.ClipError as e:
        with _CLIP_JOBS_LOCK:
            _CLIP_JOBS[job_id].update(state='error', error=str(e))
    except Exception as e:
        with _CLIP_JOBS_LOCK:
            _CLIP_JOBS[job_id].update(state='error', error=f'Unexpected error: {e}')


def _run_utility_rating_background(match_id):
    """Compute Utility Rating in the background after a demo upload. Unlike
    Overwatch this needs no 2nd-pass extractor/demo - just the already-parsed
    match JSON - so it isn't gated on a binary being present. Errors are
    silently swallowed - this must never affect the main app."""
    try:
        from analysis.utility_rating import analyze
        analyze(match_id, PROCESSED_FOLDER, ANALYSIS_DATA_FOLDER, force=False,
                labels=_load_labels(), is_pro_match=match_id in _load_pro_matches())
    except Exception:
        pass


def _run_overwatch_background(match_id, demo_path):
    """Extract + analyze overwatch data in the background after a demo upload.
    Errors are silently swallowed - this must never affect the main app."""
    raw_path = os.path.join(OVERWATCH_RAW_FOLDER, f'{match_id}.json')
    os.makedirs(OVERWATCH_RAW_FOLDER, exist_ok=True)
    tmp_raw = raw_path + '.tmp'
    try:
        result = subprocess.run(
            [_OVERWATCH_EXE, '-demo', demo_path, '-out', tmp_raw],
            capture_output=True, text=True, timeout=600, **procutil.NO_WINDOW)
        if result.returncode != 0:
            return
        jsonio.compress_file(tmp_raw, raw_path + '.br', delete_src=True)
        if os.path.exists(raw_path):   # stale pre-compression twin would win resolve()
            os.unlink(raw_path)
    except Exception:
        if os.path.exists(tmp_raw):
            os.unlink(tmp_raw)
        return
    try:
        from analysis.overwatch import analyze
        analyze(match_id, PROCESSED_FOLDER, OVERWATCH_RAW_FOLDER,
                ANALYSIS_DATA_FOLDER, force=False)
        # Overwatch flag count is now known - refresh the dashboard summary row.
        _update_match_index(f'{match_id}.json')
    except Exception:
        pass

# ── Routes ────────────────────────────────────────────────────────────────────

# Match filename: <map>_<YYYYMMDD>_<HHMM>_<mode>.json  (map may contain '_', e.g. de_dust2)
_MATCH_RE = re.compile(r'^(?P<map>.+?)_(?P<date>\d{8})_(?P<time>\d{4})_(?P<mode>[^_]+)$')


def _demo_lookup():
    """Map demo mtime (UTC, minute resolution) -> [.dem filenames].

    The parser derives a match's timestamp from its demo file's mtime
    (main.go: ModTime().UTC().Format("20060102_1504")), so this links each
    processed match back to the original .dem in the demos folder.
    """
    lookup = {}
    for f in os.listdir(UPLOAD_FOLDER):
        if not f.lower().endswith('.dem'):
            continue
        try:
            mt = os.path.getmtime(os.path.join(UPLOAD_FOLDER, f))
        except OSError:
            continue
        key = datetime.fromtimestamp(mt, timezone.utc).strftime('%Y%m%d_%H%M')
        lookup.setdefault(key, []).append(f)
    return lookup


def _match_path(fname):
    """On-disk file for a logical '<id>.json' name under processed_matches -
    the plain file or its .br variant (jsonio), None when the match is unknown."""
    return jsonio.resolve(os.path.join(PROCESSED_FOLDER, os.path.basename(fname)))


# Derived caches that live alongside the match JSON in processed_matches/ and
# must never be mistaken for a match. Anything added here is excluded from
# _list_match_files() -- the one place the app enumerates matches -- so a new
# cache kind cannot leak in as a phantom match across the dashboard, the
# players rollup, the analyser and the Overwatch dashboard all at once.
_CACHE_SUFFIXES = ('.overwatch.json', '.utility.json', '.moments.json', '.slim.json')


def _list_match_files():
    """Logical '<id>.json' names of all processed matches, .br-aware,
    excluding the derived analysis caches (_CACHE_SUFFIXES)."""
    names = set()
    for f in os.listdir(PROCESSED_FOLDER):
        if f.endswith('.json.br'):
            f = f[:-len('.br')]
        if f.endswith('.json') and not f.endswith(_CACHE_SUFFIXES):
            names.add(f)
    return sorted(names)


def _match_meta(fname, demo_lookup=None, demo_map=None):
    """Build display/sort metadata for one processed match file."""
    stem = fname[:-5] if fname.endswith('.json') else fname

    try:
        real = _match_path(fname)
        processed_epoch = int(os.path.getmtime(real)) if real else 0
    except OSError:
        processed_epoch = 0

    # Exact mapping recorded at process time; mtime matching is only a fallback
    # for matches processed before the map existed, and only when unambiguous.
    demo = (demo_map or {}).get(fname, '')
    m = _MATCH_RE.match(stem)
    if m:
        map_name, mode = m.group('map'), m.group('mode')
        if not demo and demo_lookup:
            cands = demo_lookup.get(m.group('date') + '_' + m.group('time'), [])
            if len(cands) == 1:
                demo = cands[0]
        try:
            played_dt = datetime.strptime(m.group('date') + m.group('time'), '%Y%m%d%H%M')
            played_dt = played_dt.replace(tzinfo=timezone.utc)
            played_epoch = int(played_dt.timestamp())
            played_label = played_dt.strftime('%Y-%m-%d %H:%M')
        except ValueError:
            played_epoch, played_label = 0, 'unknown'
    else:
        map_name = stem.split('_')[0] if stem else 'unknown'
        mode, played_epoch, played_label = '', 0, 'unknown'

    processed_label = (
        datetime.fromtimestamp(processed_epoch, timezone.utc).strftime('%Y-%m-%d %H:%M')
        if processed_epoch else 'unknown'
    )

    return {
        'file':            fname,
        'stem':            stem,
        'demo':            demo,
        # `map` is canonical (maps.py) so every consumer groups, filters and
        # compares on one name: matches written under a legacy spelling land in
        # the same bucket as matches written under the current one. `map_raw`
        # keeps the filename's own token for anything that has to find the file
        # again, and `map_label` is the readable form.
        'map':             maps.canonical(map_name),
        'map_raw':         map_name,
        'map_label':       maps.label(map_name),
        'mode':            mode,
        'played_epoch':    played_epoch,
        'played_label':    played_label,
        'processed_epoch': processed_epoch,
        'processed_label': processed_label,
    }


@app.route('/')
def index():
    """Matches page: the upload form plus the match list (templates/preprocessor.html).

    Rows come from the mtime-guarded summary index, not from re-reading every
    match document - see _match_summary_index().
    """
    demo_lookup = _demo_lookup()
    demo_map    = _load_demo_map()
    matches = [_match_meta(f, demo_lookup, demo_map) for f in _list_match_files()]
    # Default view: most recently processed first.
    matches.sort(key=lambda m: m['processed_epoch'], reverse=True)
    maps  = sorted({m['map']  for m in matches})
    # The displayed mode comes from the summary index, which is the only place
    # that has read the match document (and so knows whether it is Premier).
    # The filter list is built from the SAME values the rows carry, or picking
    # "MM" would hide the matches now labelled PREM.
    labels = {e['file']: e.get('modeLabel') for e in _match_summary_index()}
    for m in matches:
        m['mode_label'] = labels.get(m['file']) or (m['mode'] or '').upper()
    modes = sorted({m['mode_label'] for m in matches if m['mode_label']})
    match_meta = _load_match_meta()
    for m in matches:
        m['tags'] = (match_meta.get(m['file'], {}) or {}).get('tags', [])
        m['comment'] = (match_meta.get(m['file'], {}) or {}).get('comment', '')
    all_tags = sorted({t for m in matches for t in m['tags']})
    return render_template('preprocessor.html', matches=matches, maps=maps,
                           modes=modes, all_tags=all_tags, nav_active='matches')


@app.route('/matches/summary')
def matches_summary():
    """Precomputed dashboard rows (score, top-fraggers, duration, overwatch
    flags, size) - one small JSON so the dashboard needn't fetch every match's
    full .json.br. Self-healing index; see _match_summary_index."""
    files_q = request.args.get('files')
    files = None
    if files_q:
        wanted = set(files_q.split(','))
        files = [f for f in _list_match_files() if f in wanted]
    return {'ok': True, 'matches': _match_summary_index(files)}


@app.route('/matches/<match_id>/meta', methods=['POST'])
def matches_meta(match_id):
    """Persist per-match tags + comment (dashboard_plan.md §4, Phase 5).
    match_id is the logical '<id>.json' name."""
    fname = os.path.basename(match_id)
    if not fname.endswith('.json'):
        return {'ok': False, 'error': 'invalid match id'}, 400
    if not _match_path(fname):
        return {'ok': False, 'error': 'unknown match'}, 404

    body = request.get_json(silent=True) or {}
    tags = body.get('tags')
    comment = body.get('comment')

    with _MATCH_META_LOCK:
        meta = _load_match_meta()
        entry = meta.get(fname, {}) or {}
        if tags is not None:
            if not isinstance(tags, list):
                return {'ok': False, 'error': 'tags must be a list'}, 400
            entry['tags'] = [str(t).strip() for t in tags if str(t).strip()][:12]
        if comment is not None:
            entry['comment'] = str(comment)[:2000]
        if entry.get('tags') or entry.get('comment'):
            meta[fname] = entry
        else:
            meta.pop(fname, None)
        _save_match_meta(meta)

    return {'ok': True, 'tags': entry.get('tags', []), 'comment': entry.get('comment', '')}


# In-memory cache of the full aggregate, keyed by the players-index file mtime,
# so the /players table and each /players/<name> page render off the same warm
# dict instead of recomputing the 1000-player rollup on every request (§9).
_PLAYERS_AGG = {'key': None, 'list': [], 'by_name': {}}


def _players_aggregate_cached():
    idx = _players_index()                       # freshens the per-match cache file
    try:
        key = os.path.getmtime(PLAYERS_INDEX_PATH)
    except OSError:
        key = id(idx)
    if _PLAYERS_AGG['key'] != key:
        profs = _players_aggregate(idx)
        _PLAYERS_AGG.update(key=key, list=profs,
                            by_name={p['name']: p for p in profs})
    return _PLAYERS_AGG


@app.route('/players')
def players():
    """Cross-match Players aggregate (dashboard_plan.md Phase 6, expanded per
    ui_feature_expansion_plan §6) - one row per player name. The table is server-
    rendered with lightweight per-row fields only; each player's full profile
    (maps/weapons/teammates/demos) lives behind its own /players/<name> page so
    the table payload stays small across a large corpus."""
    players_list = _players_aggregate_cached()['list']
    return render_template('players.html', players=players_list,
                           nav_active='players')


@app.route('/players/<path:name>')
def player_page(name):
    """One player's full profile - a real, bookmarkable/linkable page (maps W/L,
    weapons+accuracy, teammates, demos). Resource-efficient by construction: it's
    an O(1) lookup into the same in-memory _players_aggregate_cached() dict the
    /players table already warmed, rendered server-side in one response - no
    separate JSON fetch/client-side card-building round trip (ui_feature_
    expansion_plan.md §6)."""
    prof = _players_aggregate_cached()['by_name'].get(name)
    if not prof:
        abort(404)
    # Copy before adding request-scoped keys - `prof` is the same dict object
    # held in _PLAYERS_AGG's cache, shared across requests/pages.
    p = dict(prof)
    p['timing'] = _player_timing_features(prof.get('steam_id'))
    p['rankedTiming'] = _ranked_avg_timing_features()
    return render_template('player.html', p=p, nav_active='players')


# name -> {'fingerprint': (...), 'highlights': [...]}, same mtime-fingerprint
# cache pattern as _players_aggregate_cached() - recomputed only when one of
# the player's match files changes (a re-parse, a new match added).
_PLAYER_HIGHLIGHTS_CACHE = {}


@app.route('/player/<path:name>/highlights')
def player_highlights(name):
    """Cross-match highlight reel for one player: multi-kills/aces, 1vX clutch
    wins, and high-impact single kills (analysis/highlights.py). Sources
    straight from each match's own kills[]/rounds[] - no Overwatch 2nd-pass
    extractor needed, so this works for every match the player appears in,
    not just ones with an Overwatch analysis cached."""
    prof = _players_aggregate_cached()['by_name'].get(name)
    if not prof:
        return {'ok': False, 'error': f'unknown player {name}'}, 404

    files = sorted(set(prof.get('files', [])))
    fingerprint = tuple(
        (f, int(os.path.getmtime(p))) for f in files
        for p in [_match_path(f)] if p
    )
    cached = _PLAYER_HIGHLIGHTS_CACHE.get(name)
    if cached and cached['fingerprint'] == fingerprint:
        return {'ok': True, 'player': name,
                'highlights': _with_clip_urls(cached['highlights'], name)}

    from analysis.highlights import compute_match_highlights, highlight_sort_key
    demo_map = _load_demo_map()
    highlights = []
    for f in files:
        path = _match_path(f)
        if not path:
            continue
        try:
            match_data = jsonio.load(os.path.join(PROCESSED_FOLDER, f))
        except (OSError, ValueError):
            continue
        meta = _match_meta(f, demo_map=demo_map)
        for h in compute_match_highlights(match_data, name):
            h['match'] = f
            # Carried so the UI can group/label by match without a second
            # round-trip per row.
            h['match_stem'] = meta['stem']
            h['match_map'] = meta['map']
            h['match_played_label'] = meta['played_label']
            h['match_played_epoch'] = meta['played_epoch']
            highlights.append(h)

    # Group by match (newest match first), then by highlight priority within
    # a match - clutches, multi-kills, impact kills (analysis/highlights.py's
    # highlight_sort_key), so the UI's per-match cap keeps the best moments.
    # A flat sort on tick alone came first here and interleaved rows from
    # every match the player appears in - ticks aren't comparable across
    # demos, so the list looked shuffled.
    highlights.sort(key=lambda h: (-(h.get('match_played_epoch') or 0),
                                   h.get('match') or '',
                                   highlight_sort_key(h)))
    _PLAYER_HIGHLIGHTS_CACHE[name] = {'fingerprint': fingerprint, 'highlights': highlights}
    return {'ok': True, 'player': name,
            'highlights': _with_clip_urls(highlights, name)}


def _with_clip_urls(highlights, player_name=None):
    """Attach clip_url to any highlight whose in-game clip is already
    recorded on disk. Deliberately computed per request rather than stored
    in the highlight caches: those are keyed on match-file mtimes, so a clip
    recorded after one was populated would never show up until a re-parse.
    One os.path.isfile per row is cheap enough for that.

    A clip id is player-specific, so the focus player is the row's own
    `player` when it has one - the match-wide list, where every row can be a
    different player - and otherwise `player_name`, the single player whose
    cross-match reel this is."""
    out = []
    for h in highlights:
        h = dict(h)
        match = h.get('match') or ''
        focus = h.get('player') or player_name
        if match.endswith('.json') and focus \
                and h.get('start_tick') is not None \
                and h.get('end_tick') is not None:
            match_id = match[:-len('.json')]
            clip_file = clip_record.existing_clip_file(
                match_id, h['start_tick'], h['end_tick'], focus)
            h['clip_url'] = f'/clips/{match_id}/{clip_file}' if clip_file else None
            h['clip_short'] = None
            if clip_file:
                short = clip_record.clip_length_mismatch(
                    clip_record.clip_file_path(match_id, clip_file[:-len('.mp4')]),
                    h['start_tick'], h['end_tick'])
                if short:
                    h['clip_short'] = {'actual': round(short[0], 2),
                                       'expected': round(short[1], 2)}
        else:
            h['clip_url'] = None
            h['clip_short'] = None
        out.append(h)
    return out


# match file -> {'mtime': int, 'highlights': [...]}, the single-file form of
# _PLAYER_HIGHLIGHTS_CACHE's fingerprint: one match's highlights only change
# when that match is re-parsed.
_MATCH_HIGHLIGHTS_CACHE = {}


@app.route('/match/highlights')
def match_highlights():
    """Every player's highlight moments for ONE match, merged into a single
    priority-ranked list (the match page's Highlights tab). Same source as the
    player-page reel - each match's own kills[]/rounds[], no Overwatch 2nd-pass
    extractor and no original .dem - so it works for every parsed match."""
    match = os.path.basename(request.args.get('match', ''))
    if not match.endswith('.json'):
        return {'ok': False, 'error': 'missing or invalid ?match='}, 400
    path = _match_path(match)
    if not path:
        return {'ok': False, 'error': f'unknown match {match}'}, 404

    try:
        mtime = int(os.path.getmtime(path))
    except OSError:
        mtime = None
    cached = _MATCH_HIGHLIGHTS_CACHE.get(match)
    if cached and mtime is not None and cached['mtime'] == mtime:
        return {'ok': True, 'match': match,
                'highlights': _with_clip_urls(cached['highlights'])}

    try:
        match_data = jsonio.load(os.path.join(PROCESSED_FOLDER, match))
    except (OSError, ValueError) as e:
        return {'ok': False, 'error': f'could not read match: {e}'}, 500

    # Roster from the match's own events (kills/damage/shots), not ranks[] -
    # ranks[] is only populated for official Valve demos. `team` is the fixed
    # first-half-CT team identity, so the tab can colour a name consistently
    # across the halftime side swap.
    roster = match_summary.compute_player_profile_rows(match_data)
    team_by_name = {r['name']: r.get('team', 0) for r in roster}

    from analysis.highlights import compute_all_match_highlights
    highlights = compute_all_match_highlights(match_data, list(team_by_name))
    for h in highlights:
        h['team'] = team_by_name.get(h['player'], 0)
        h['match'] = match      # _with_clip_urls() derives the clip's match_id from this

    if mtime is not None:
        _MATCH_HIGHLIGHTS_CACHE[match] = {'mtime': mtime, 'highlights': highlights}
    return {'ok': True, 'match': match,
            'highlights': _with_clip_urls(highlights)}


# tools/extract_ui_assets.{sh,ps1,bat} already read this exact plain-text
# file (their setting()/Get-Setting helpers) - this just adds a Settings-page
# editor for it so it doesn't have to be hand-written to disk. Unrelated to
# clip_record.py's tool paths: nothing the running app launches uses this,
# only the one-off offline extraction script a developer runs manually.
_SOURCE2VIEWER_CLI_PATH = os.path.join(paths.ANALYSIS_DATA_DIR, 'source2viewer_cli')


def _load_text_setting(path):
    if not os.path.isfile(path):
        return None
    with open(path, encoding='utf-8') as f:
        return f.read().strip() or None


def _save_text_setting(path, value):
    os.makedirs(paths.ANALYSIS_DATA_DIR, exist_ok=True)
    if value:
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(value + '\n')
        os.replace(tmp, path)
    else:
        try:
            os.remove(path)
        except OSError:
            pass


@app.route('/settings')
def settings():
    """Lightweight settings / about page - a rail destination (dashboard_plan.md
    §3.1). Surfaces data location, corpus size, and feature toggles."""
    match_files = _list_match_files()
    total_bytes = 0
    for f in match_files:
        real = _match_path(f)
        if real:
            try:
                total_bytes += os.path.getsize(real)
            except OSError:
                pass
    info = {
        'version':       __version__,
        'data_dir':      DATA_DIR,
        'match_count':   len(match_files),
        'total_bytes':   total_bytes,
        'overwatch_bin': os.path.isfile(_OVERWATCH_EXE),
        'parser_bin':    os.path.isfile(_PARSER_EXE),
    }
    clip_settings = clip_record.load_settings()
    clip_settings['ready'] = clip_record.settings_ready(clip_settings)
    clip_settings['resolution_presets'] = [
        {'label': label, 'width': w, 'height': h,
         'mb_per_second': (round(clip_record.frame_bytes_per_second(w, h) / 1e6)
                           if w and h else None)}
        for label, w, h in clip_record.CLIP_RESOLUTION_PRESETS
    ]
    clip_settings['resolution_is_custom'] = bool(
        clip_settings['clip_width'] and not any(
            clip_settings['clip_width'] == w and clip_settings['clip_height'] == h
            for _label, w, h in clip_record.CLIP_RESOLUTION_PRESETS))
    return render_template('settings.html', info=info, clip_settings=clip_settings,
                           assets=_assets_inventory(),
                           zones=_zones_inventory(),
                           clip_risk_notice=clip_record.RISK_NOTICE,
                           source2viewer_cli=_load_text_setting(_SOURCE2VIEWER_CLI_PATH),
                           cs2_game_dir=clip_settings['cs2_game_dir'],
                           cs2_exe_rel=clip_record.cs2_exe_path('').lstrip('/\\'),
                           cs2_dir_placeholder=('~/.local/share/Steam/steamapps/common/Counter-Strike Global Offensive'
                                                if clip_record.IS_LINUX else
                                                r'C:\...\Counter-Strike Global Offensive'),
                           asset_kinds=paths.ASSET_KINDS,
                           nav_active='settings')


@app.route('/settings/cs2-game-dir', methods=['POST'])
def settings_cs2_game_dir():
    """Save (or auto-detect) the single, app-wide CS2 game directory
    (paths.CS2_GAME_DIR_FILE - see clip_record.load_cs2_game_dir()). Shared
    by every tool that needs a real CS2 install: Clip Recording's readiness
    check and tools/extract_ui_assets.*'s VPK lookup both read this same
    value, so it's configured in exactly one place rather than once per
    tool/card."""
    body = request.get_json(silent=True) or {}
    if body.get('autodetect'):
        detected = clip_record.autodetect_paths().get('cs2_game_dir')
        value = clip_record.load_cs2_game_dir() or detected
    else:
        value = (body.get('cs2_game_dir') or '').strip() or None
    clip_record.save_cs2_game_dir(value)
    return {'ok': True, 'cs2_game_dir': value,
            'cs2_found': bool(value and os.path.isfile(clip_record.cs2_exe_path(value)))}


@app.route('/settings/clip-paths', methods=['POST'])
def settings_clip_paths():
    """Save the HLAE/ffmpeg paths real clip recording needs (clip_record.py),
    plus the separate VAC-risk acknowledgment flag that actually arms the
    feature (clip_record.settings_ready()). The CS2 game directory itself is
    configured separately (see /settings/cs2-game-dir) since it's an app-wide
    setting, not specific to clip recording. A purely local-machine setting -
    irrelevant on a server deployment, which is why it's opt-in Settings
    rather than baked into the main pipeline.

    There is deliberately no auto-detect here (the CS2 game directory keeps
    its own): HLAE and ffmpeg are user-installed third-party binaries with no
    canonical install location, so the probe was guessing more often than
    finding, and a wrong path that looks configured is worse than an empty
    one. clip_record.autodetect_paths() is still used by /settings/cs2-game-dir,
    where Steam's layout makes the guess reliable."""
    body = request.get_json(silent=True) or {}
    width, height = clip_record.normalize_resolution(
        body.get('clip_width'), body.get('clip_height'))
    merged = {
        'hlae_path': (body.get('hlae_path') or '').strip() or None,
        'ffmpeg_path': (body.get('ffmpeg_path') or '').strip() or None,
        'vac_risk_ack': bool(body.get('vac_risk_ack')),
        'clip_width': width,
        'clip_height': height,
        'clip_hud': bool(body.get('clip_hud')),
        'clip_crosshair': bool(body.get('clip_crosshair', True)),
    }
    clip_record.save_settings(merged)
    merged['cs2_game_dir'] = clip_record.load_cs2_game_dir()
    merged['ready'] = clip_record.settings_ready(merged)
    return {'ok': True, **merged}


@app.route('/settings/source2viewer-path', methods=['POST'])
def settings_source2viewer_path():
    """Persist tools/extract_ui_assets.*'s Source2Viewer-CLI setting
    (data/analysis_data/source2viewer_cli - the same plain-text file those
    shell/PowerShell scripts already read) so it can be edited here instead
    of by hand. Purely a config file for a script a developer runs manually;
    saving a path here doesn't launch or download anything. The CS2 game
    directory this tool also needs is configured separately, app-wide (see
    /settings/cs2-game-dir) - not duplicated here."""
    body = request.get_json(silent=True) or {}
    value = (body.get('source2viewer_cli') or '').strip()
    _save_text_setting(_SOURCE2VIEWER_CLI_PATH, value)
    return {'ok': True, 'source2viewer_cli': value or None}


# ── UI asset extraction (Settings page button) ────────────────────────────────
#
# tools/extract_ui_assets.{sh,ps1} pulls the icons out of the local CS2 install.
# It stays a standalone script - it's a once-per-game-update job that people run
# from a terminal too - this just drives it from Settings so nobody has to find
# a shell, with the same fire-and-poll shape as /process and /clip/record.

_ASSET_JOBS: dict = {}
_ASSET_JOBS_LOCK = threading.Lock()
_ASSET_LOG_LINES = 400        # tail kept per job; the script is chatty per file


def _asset_script_argv(kinds=None):
    """argv for the extractor on this platform. The .sh and .ps1 are siblings
    kept in lockstep (see either script's header); .bat is only a convenience
    wrapper around the .ps1, so PowerShell is invoked directly here. The script
    only extracts - tinting and indexing happen in-process afterwards.

    Kinds are filtered against paths.ASSET_KINDS here as well as at the route,
    so the helper can't pass caller-supplied text through to a command line
    however it's called (Popen takes a list, never a shell, so this is defence
    in depth rather than the only guard)."""
    root = paths.BUNDLE_ROOT
    if os.name == 'nt':
        argv = ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                os.path.join(root, 'tools', 'extract_ui_assets.ps1')]
    else:
        argv = ['bash', os.path.join(root, 'tools', 'extract_ui_assets.sh')]
    return argv + [k for k in (kinds or []) if k in paths.ASSET_KINDS]


def _asset_log(job_id, line):
    with _ASSET_JOBS_LOCK:
        job = _ASSET_JOBS[job_id]
        job['log'].append(line)
        del job['log'][:-_ASSET_LOG_LINES]
        job['message'] = line


def _asset_worker(job_id, argv):
    """Run the extractor (argv None = index only), streaming its output into
    the job's log tail, then tint + index in-process with assetindex. The
    script writes progress per file, so the UI can show what it's doing rather
    than a spinner that hides a two-minute VPK crawl."""
    with _ASSET_JOBS_LOCK:
        _ASSET_JOBS[job_id].update(state='running', message='Starting…')
    if argv is not None:
        try:
            # The scripts find their settings and output dir through these two,
            # not through their own position - the read-only bundle when frozen.
            # CS2VIEWER_SKIP_INDEX: the tint + manifest step is ours, below.
            env = dict(os.environ, CS2VIEWER_DATA_DIR=paths.DATA_DIR,
                       CS2VIEWER_ASSETS_DIR=paths.ASSETS_DIR, CS2VIEWER_SKIP_INDEX='1')
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding='utf-8', errors='replace',
                                    cwd=paths.BUNDLE_ROOT, env=env, **procutil.NO_WINDOW)
        except OSError as e:
            with _ASSET_JOBS_LOCK:
                _ASSET_JOBS[job_id].update(state='error', error=f'Could not start the script: {e}')
            return
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                _asset_log(job_id, line)
        code = proc.wait()
        if code != 0:
            with _ASSET_JOBS_LOCK:
                job = _ASSET_JOBS[job_id]
                # The script's own last words are far more useful than the exit
                # code (missing CS2 dir, missing Source2Viewer-CLI, …).
                tail = ' / '.join(job['log'][-3:]) or f'exit code {code}'
                job.update(state='error', error=f'Extraction failed: {tail}')
            return

    _asset_log(job_id, '==> tinting + rebuilding manifest…')
    ok, msg, lines = assetindex.finish(paths.ASSETS_DIR)
    for line in lines:
        _asset_log(job_id, line)
    with _ASSET_JOBS_LOCK:
        job = _ASSET_JOBS[job_id]
        if ok:
            job.update(state='done', message='Done.', assets=_assets_inventory())
        else:
            job.update(state='error', error=f'Indexing failed: {msg}')


def _assets_inventory():
    """What's on disk right now, per kind - the Settings page shows this before
    and after a run so 'did that work?' has an answer that isn't a log tail."""
    counts = {}
    for kind in paths.ASSET_KINDS:
        d = os.path.join(paths.ASSETS_DIR, kind)
        try:
            counts[kind] = sum(1 for f in os.listdir(d)
                               if not f.startswith('.') and os.path.isfile(os.path.join(d, f)))
        except OSError:
            counts[kind] = 0
    return {'present': paths.assets_present(), 'kinds': counts,
            'total': sum(counts.values())}


@app.route('/settings/extract-assets', methods=['POST'])
def settings_extract_assets():
    """Kick off tools/extract_ui_assets.* in the background. Body may carry
    {kinds: [...]} to re-extract only some, or {manifest_only: true} to just
    re-index the images already on disk (needs no CS2 files at all)."""
    body = request.get_json(silent=True) or {}
    manifest_only = bool(body.get('manifest_only'))
    kinds = [k for k in (body.get('kinds') or []) if k in paths.ASSET_KINDS]

    if not manifest_only:
        game_dir = clip_record.load_cs2_game_dir()
        if not game_dir:
            return {'ok': False, 'error':
                    'Set the CS2 game directory first (CS2 Installation card) - '
                    'the icons are extracted from its .vpk files.'}, 409

    with _ASSET_JOBS_LOCK:
        running = [j for j in _ASSET_JOBS.values() if j['state'] == 'running']
        if running:
            return {'ok': False, 'error': 'An extraction is already running.'}, 409
        job_id = str(uuid.uuid4())
        _ASSET_JOBS[job_id] = {'state': 'pending', 'message': None, 'error': None,
                               'log': [], 'assets': None}

    threading.Thread(target=_asset_worker, daemon=True,
                     args=(job_id, None if manifest_only else _asset_script_argv(kinds))).start()
    return {'ok': True, 'job_id': job_id}


@app.route('/settings/extract-assets/status/<job_id>')
def settings_extract_assets_status(job_id):
    """Poll one UI-asset extraction job.

    -> {ok, state, message, error, assets, log[-25:]} - the log tail is what
    makes a failed VPK crawl diagnosable. 404 when the job id is unknown.
    """
    with _ASSET_JOBS_LOCK:
        job = _ASSET_JOBS.get(job_id)
        if not job:
            return {'ok': False, 'error': 'unknown job'}, 404
        return {'ok': True, **job, 'log': job['log'][-25:]}


# ── Map zone extraction (Settings page button) ────────────────────────────────
#
# mapzone_extract.py pulls Valve's env_cs_place callout regions out of each
# map's own .vpk - what lets the Multi Match Analyser's "Zone" filter answer
# with a real callout name instead of just bomb_site A/B. Same fire-and-poll
# shape as UI asset extraction above, and the same two settings (CS2 game dir,
# Source2Viewer-CLI). Unlike the old tools/extract_map_zones.mjs this needs no
# Node - the installed desktop app has none - so it is the only way a compiled
# build can ever get zone data at all.

_ZONE_JOBS: dict = {}
_ZONE_JOBS_LOCK = threading.Lock()


def _zones_inventory():
    """What's on disk right now vs. what could be extracted - the Settings
    page shows this before/after a run, same idea as _assets_inventory()."""
    calibrated = mapzone_extract.calibrated_maps()
    extracted = mapzone_extract.extracted_maps()
    return {'present': bool(extracted), 'extracted': extracted,
            'calibrated': calibrated, 'missing': [m for m in calibrated if m not in extracted]}


def _zone_worker(job_id, map_names):
    with _ZONE_JOBS_LOCK:
        _ZONE_JOBS[job_id].update(state='running', message='Starting…')

    def on_log(line):
        with _ZONE_JOBS_LOCK:
            job = _ZONE_JOBS[job_id]
            job['log'].append(line)
            del job['log'][:-_ASSET_LOG_LINES]
            job['message'] = line

    ok, msg, _lines = mapzone_extract.run(map_names, on_log=on_log)
    with _ZONE_JOBS_LOCK:
        job = _ZONE_JOBS[job_id]
        if ok:
            job.update(state='done', message='Done.', zones=_zones_inventory())
        else:
            job.update(state='error', error=msg)


@app.route('/settings/extract-zones', methods=['POST'])
def settings_extract_zones():
    """Kick off mapzone_extract.run() in the background. Body may carry
    {maps: [...]} to re-extract only some (canonical or raw names both work -
    the extractor canonicalises); default is every calibrated map."""
    body = request.get_json(silent=True) or {}
    map_names = [m for m in (body.get('maps') or []) if m] or None

    if not clip_record.load_cs2_game_dir():
        return {'ok': False, 'error':
                'Set the CS2 game directory first (CS2 Installation card) - '
                'zones are extracted from its .vpk files.'}, 409

    with _ZONE_JOBS_LOCK:
        running = [j for j in _ZONE_JOBS.values() if j['state'] == 'running']
        if running:
            return {'ok': False, 'error': 'A zone extraction is already running.'}, 409
        job_id = str(uuid.uuid4())
        _ZONE_JOBS[job_id] = {'state': 'pending', 'message': None, 'error': None,
                              'log': [], 'zones': None}

    threading.Thread(target=_zone_worker, daemon=True, args=(job_id, map_names)).start()
    return {'ok': True, 'job_id': job_id}


@app.route('/settings/extract-zones/status/<job_id>')
def settings_extract_zones_status(job_id):
    """Poll one map-zone extraction job. -> {ok, state, message, error, zones,
    log[-25:]}. 404 when the job id is unknown."""
    with _ZONE_JOBS_LOCK:
        job = _ZONE_JOBS.get(job_id)
        if not job:
            return {'ok': False, 'error': 'unknown job'}, 404
        return {'ok': True, **job, 'log': job['log'][-25:]}


def _rebuild_manifest():
    """Tint + index in-process (assetindex.finish); returns (ok, message).

    Unlike a VPK extraction this is a sub-second directory scan, so the import
    route waits for it rather than handing back a job to poll. Doing it inline
    also means one request answers the only question that matters - are the
    icons usable now? - instead of reporting a successful copy into a directory
    the app still can't look anything up in.
    """
    ok, msg, _ = assetindex.finish(paths.ASSETS_DIR)
    return ok, msg


@app.route('/settings/import-assets', methods=['POST'])
def settings_import_assets():
    """Manual alternative to the VPK extraction: take the icons as an upload.

    Accepts a multipart POST of one or more `files` - a .zip of an
    already-extracted `static/assets/` tree, or loose images - plus an optional
    `kind` naming the target directory ('auto' to route each file by its
    archive folder / filename). Copies them in, rebuilds the manifest, and
    answers with what landed where.

    This exists because the extractor needs CS2 + Source2Viewer-CLI on one
    machine, and the images are just files: someone who has extracted them once
    can carry them to a box that has neither. The game's grey Premier art is
    accepted too - the rebuild tints it.
    """
    uploads = request.files.getlist('files')
    if not uploads:
        return {'ok': False, 'error': 'No files were uploaded.'}, 400

    kind = request.form.get('kind') or 'auto'
    if kind != 'auto' and kind not in paths.ASSET_KINDS:
        return {'ok': False, 'error': f'Unknown asset kind: {kind}'}, 400
    explicit = None if kind == 'auto' else kind

    report = assetimport.new_report()
    try:
        for up in uploads:
            name = up.filename or ''
            is_zip = zipfile.is_zipfile(up.stream)
            up.stream.seek(0)
            if is_zip:
                assetimport.ingest_zip(up.stream, paths.ASSETS_DIR, explicit, report)
            elif name.lower().endswith('.zip'):
                # Named .zip but unreadable as one. Falling through to the
                # loose-image path would skip it as "not an image" and report
                # "nothing found", which reads as though the archive was fine
                # and empty - the one conclusion that isn't true.
                return {'ok': False, 'error': f'{name} could not be read as a .zip - '
                        'it looks corrupt or was only partly uploaded.'}, 400
            else:
                assetimport.ingest_file(name, up.stream.read(assetimport.MAX_FILE_BYTES + 1),
                                        paths.ASSETS_DIR, explicit, report)
    except zipfile.BadZipFile:
        return {'ok': False, 'error': 'That .zip could not be read - it looks corrupt.'}, 400
    except ValueError as e:
        return {'ok': False, 'error': str(e)}, 400
    except OSError as e:
        return {'ok': False, 'error': f'Could not write into {paths.ASSETS_DIR}: {e}'}, 500

    message = assetimport.summarize(report)
    manifest_ok, manifest_msg = (True, 'index not rebuilt (nothing imported)')
    if report['total']:
        manifest_ok, manifest_msg = _rebuild_manifest()

    return {'ok': True, 'imported': report['imported'], 'total': report['total'],
            'unrouted': report['unrouted'], 'skipped': report['skipped'],
            'truncated': report['truncated'],
            'message': message, 'manifest_ok': manifest_ok, 'manifest_message': manifest_msg,
            'assets': _assets_inventory()}


@app.route('/process', methods=['POST'])
def process_demo():
    """Accept a demo upload, save it, and start a background parse job.
    Returns {ok, job_id} immediately - the client polls /process/status/<job_id>."""
    file = request.files.get('demo_file')
    if not file or not file.filename:
        return {'ok': False, 'error': 'no file'}, 400

    demo_filename = secure_filename(file.filename)
    demo_path     = os.path.join(UPLOAD_FOLDER, demo_filename)
    file.save(demo_path)

    # Restore the file's on-disk mtime so the parser derives the correct match
    # timestamp (CS2 demos don't embed a real-world date - the parser uses mtime).
    demo_mtime = request.form.get('demo_mtime', '')
    if demo_mtime.isdigit():
        ts = int(demo_mtime) / 1000.0
        try:
            os.utime(demo_path, (ts, ts))
        except OSError:
            pass

    job_id = str(uuid.uuid4())
    with _JOBS_LOCK:
        _JOBS[job_id] = {'state': 'pending', 'match': None, 'error': None,
                         'stage': 'queued', 'progress': 0.0, 'eta': None}

    threading.Thread(
        target=_parse_worker,
        args=(job_id, demo_path, demo_filename),
        daemon=True,
    ).start()

    return {'ok': True, 'job_id': job_id}


@app.route('/process/status/<job_id>')
def process_status(job_id):
    """Poll one demo-parse job.

    -> {ok, state: 'pending'|'processing'|'done'|'error', match, error,
        stage, progress: 0..1, eta: seconds|None}

    Keys prefixed with _ are the job's own bookkeeping (the monotonic start) and
    are not part of the wire shape.
    """
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return {'ok': False, 'error': 'unknown job'}, 404
        public = {k: v for k, v in job.items() if not k.startswith('_')}
    return {'ok': True, **public}


@app.route('/viewer')
def viewer():
    """2D replayer page. Query: ?match=<file>.json[&tick=N] to deep-link a moment."""
    return render_template('index.html', match_file=request.args.get('match'),
                           nav_active='viewer')


@app.route('/match')
def match_stats():
    """Match statistics page. Query: ?match=<file>.json[&tab=<name>] to deep-link a tab."""
    return render_template('match.html', match_file=request.args.get('match'),
                           nav_active='match')


# ── Moment index + query engine (Multi Match Analyser) ────────────────────────
# The analyser asks questions across the whole corpus ("every opening duel I
# lost on Mirage while a man up"), which needs pre-derived per-event context -
# alive counts, trade links, round phase, buy type. Computing that per request
# would mean loading every match JSON (~28 MB each decompressed), so it's built
# once per match and cached on disk beside the .overwatch.json/.utility.json
# caches, then held warm in memory keyed on the corpus mtimes.
_MOMENTS_CACHE = {'key': None, 'indexes': []}


def _moments_cache_path(fname):
    stem = fname[:-5] if fname.endswith('.json') else fname
    return os.path.join(PROCESSED_FOLDER, stem + '.moments.json')


def _moment_index_for(fname, force=False):
    """Build-or-load one match's moment index. Same mtime + schema guard the
    other per-match analysis caches use."""
    src = _match_path(fname)
    if not src:
        return None
    cache = _moments_cache_path(fname)
    if not force:
        try:
            if os.path.getmtime(cache) >= os.path.getmtime(src):
                with open(cache, encoding='utf-8') as f:
                    cached = json.load(f)
                if cached.get('v') == moments.MOMENTS_SCHEMA:
                    return cached
        except (OSError, ValueError):
            pass
    data = jsonio.load(src)
    # Never let grenades[] near the index builder: 205k trajectory points,
    # ~95% of the document, and nothing a query can filter on.
    data.pop('grenades', None)
    index = moments.build_match_index(data, _match_meta(fname))
    tmp = cache + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(index, f)
    os.replace(tmp, cache)
    return index


def _moments_corpus(force=False):
    """Every match's moment index, warm. Rebuilt only when a match file's
    mtime changes (the _players_aggregate_cached fingerprint idiom)."""
    files = _list_match_files()
    key = tuple((f, int(os.path.getmtime(p)))
                for f in files for p in [_match_path(f)] if p)
    with _MATCH_INDEX_LOCK:
        if not force and _MOMENTS_CACHE['key'] == key:
            return _MOMENTS_CACHE['indexes']
    indexes = []
    for f in files:
        try:
            idx = _moment_index_for(f, force=force)
        except Exception as exc:          # one bad match must not kill the page
            app.logger.warning('moment index failed for %s: %s', f, exc)
            continue
        if idx:
            indexes.append(idx)
    with _MATCH_INDEX_LOCK:
        _MOMENTS_CACHE['key'] = key
        _MOMENTS_CACHE['indexes'] = indexes
    return indexes


def _corpus_caps(indexes):
    """Capabilities the WHOLE corpus supports - a filter is only offered when
    every match can answer it, otherwise it would silently exclude the matches
    that can't."""
    if not indexes:
        return {}
    keys = set()
    for i in indexes:
        keys.update((i.get('caps') or {}).keys())
    return {k: all((i.get('caps') or {}).get(k) for i in indexes) for k in keys}


@app.route('/analyser/fields')
def analyser_fields():
    """The filter surface + corpus-wide facet values, so the client builds its
    whole query panel from here rather than duplicating the field list."""
    indexes = _moments_corpus()
    caps = _corpus_caps(indexes)
    maps, modes, players, matches_meta = set(), set(), set(), []
    lo = hi = None
    for i in indexes:
        maps.add(i.get('map'))
        modes.add(i.get('mode'))
        for p in i.get('players') or []:
            players.add(p['name'])
        ep = i.get('played_epoch') or 0
        lo = ep if lo is None else min(lo, ep)
        hi = ep if hi is None else max(hi, ep)
        matches_meta.append({'file': i.get('match'), 'stem': i.get('stem'),
                             'map': i.get('map'), 'mode': i.get('mode'),
                             'played_label': i.get('played_label'),
                             'played_epoch': i.get('played_epoch'),
                             'moments': len(i.get('moments') or []),
                             # Per-match roster, grouped by FIXED team identity
                             # (1 = first-half CT). The player picker offers
                             # "with a teammate" and "against an opponent", and
                             # neither is answerable from the flat corpus-wide
                             # name list: scoped to one match it would put 121
                             # strangers in front of you, and it never says who
                             # was on whose side. 10 names per match.
                             'roster': {
                                 '1': sorted(p['name'] for p in (i.get('players') or [])
                                             if p.get('team') == 1),
                                 '2': sorted(p['name'] for p in (i.get('players') or [])
                                             if p.get('team') == 2),
                             }})
    matches_meta.sort(key=lambda m: m['played_epoch'] or 0, reverse=True)
    return {
        'ok': True,
        'fields': query.field_registry(caps),
        'units': sorted(query.UNITS),
        'presets': query.PRESETS,
        'breakdowns': query.breakdowns(),
        'caps': caps,
        'corpus': {
            'maps': sorted(m for m in maps if m),
            'modes': sorted(m for m in modes if m),
            'players': sorted(players),
            'matches': matches_meta,
            'played_from': lo, 'played_to': hi,
            'n_moments': sum(len(i.get('moments') or []) for i in indexes),
        },
    }


@app.route('/analyser/query', methods=['GET', 'POST'])
def analyser_query():
    """Run one query. GET takes ?q=<json> so a result set is a shareable URL;
    POST takes the spec as the body for anything too long for a querystring."""
    if request.method == 'POST':
        spec = request.get_json(silent=True) or {}
    else:
        raw = request.args.get('q')
        try:
            spec = json.loads(raw) if raw else {}
        except ValueError:
            return {'ok': False, 'error': 'q= is not valid JSON'}, 400
    # Coordinates for the whole hit set are opt-in per request rather than part
    # of the spec: they are how the client wants to DRAW the answer, not part
    # of the question, and a shared URL shouldn't carry them.
    if request.args.get('points') in ('1', 'true', 'yes'):
        spec = dict(spec or {})
        spec['points'] = True
    try:
        result = query.evaluate(spec, _moments_corpus())
    except query.QueryError as exc:
        return {'ok': False, 'error': str(exc),
                'fields': sorted(query.FIELDS)}, 400
    # Recorded clips are re-attached per request, never cached: a clip made
    # after the cache filled would otherwise stay invisible (same rule as the
    # highlights routes).
    result['rows'] = _with_clip_urls(result['rows'])
    result['ok'] = True
    return result


@app.route('/multi')
def multi_round():
    """Multi-round / multi-match overlay viewer: superimpose selected rounds and
    players, time-aligned to each round's start.

    Three ways in, and the difference between them is the point - the page says
    which one you are in and scopes BOTH halves (the query and the round rail)
    to it:

        ?match=<file>  - that one match (the rail's "Analyse this match" link,
                         a dashboard row menu). Scope is the match, not its
                         map: arriving from a match page and silently getting
                         all five matches on the map is what made this page
                         confusing.
        ?files=a,b,c   - exactly those matches (the dashboard's multi-select),
                         same-map enforced server-side.
        ?map=<name>    - every match on one map.
        ?scope=all     - the general, cross-match analyser (the rail's "Match
                         Analyser"): the corpus is in scope and one map is
                         loaded as the radar (`&map=` if given, else the newest
                         match's), because the canvas has to draw *something*
                         and the query engine doesn't care. A bare /multi, and
                         any entry naming a match that no longer exists,
                         redirect here - /multi with nothing to show used to be
                         a page whose round rail and controls were never built.

    In every case the *offered* list (`matches`) is every match on that map,
    while `scope` says what is in scope right now. Widening from the page is
    then a client-side change with no reload, and query results always point at
    matches the page can actually open.

    `entry` is the match the scope bar's "This match" refers to and the one
    whose section auto-opens; it is empty for the map-wide and corpus-wide
    entries, which is also what keeps the shell's contextual MATCH group (and
    its Stats / 2D Replay links) from pointing at an arbitrary match nobody
    asked for.
    """
    match   = request.args.get('match')
    map_q   = request.args.get('map')
    files_q = request.args.get('files')
    scope_q = (request.args.get('scope') or '').lower()

    metas = [_match_meta(f) for f in _list_match_files()]
    by_file = {m['file']: m for m in metas}
    scope_kind, scope_files = 'map', []

    general = scope_q == 'all'
    # One meaning per URL: a bare /multi, and a ?match= naming a file that is
    # not there (a stale bookmark, a deleted demo), both redirect to the
    # canonical general entry rather than quietly standing in for it. Keeping
    # `match` truthy for a file that does not exist would render the shell's
    # MATCH group with three links that all 404.
    if not general:
        if not (match or files_q or map_q):
            return redirect(url_for('multi_round', scope='all'))
        if match and match not in by_file:
            return redirect(url_for('multi_round', scope='all'))

    if general:
        # Corpus-wide. The radar is whichever map is asked for, else the newest
        # match's - a choice the page states and the scope bar can change; the
        # query is not restricted by it (kind 'all'). `?scope=all&map=<m>` is
        # what the map picker produces, and it is what makes a bookmarked
        # general page keep its radar when a newer demo on another map lands.
        radar = next((m for m in metas if m['map'] == map_q), None) if map_q else None
        if radar is None:
            radar = max(metas, key=lambda m: m['played_epoch'] or 0) if metas else None
        group = [m for m in metas if radar and m['map'] == radar['map']]
        group.sort(key=lambda m: m['played_epoch'] or 0, reverse=True)
        scope_kind, scope_files = 'all', []
    elif match:
        entry = by_file.get(match)
        group = [m for m in metas if entry and m['map'] == entry['map']]
        group.sort(key=lambda m: (m['file'] != match, -(m['played_epoch'] or 0)))
        if entry:
            scope_kind, scope_files = 'match', [match]
    elif files_q:
        picked = [by_file[f] for f in files_q.split(',') if f in by_file]
        if picked:
            picked = [m for m in picked if m['map'] == picked[0]['map']]
        picked_files = {m['file'] for m in picked}
        # Offer the whole map, scope the selection: the picked set is what the
        # user asked for, but "and now show me the rest of the map" must not
        # need a round trip.
        group = [m for m in metas if picked and m['map'] == picked[0]['map']]
        group.sort(key=lambda m: (m['file'] not in picked_files,
                                  -(m['played_epoch'] or 0)))
        if picked:
            scope_kind, scope_files = 'set', [m['file'] for m in picked]
    else:
        group = [m for m in metas if map_q and m['map'] == map_q]
        group.sort(key=lambda m: m['played_epoch'], reverse=True)

    matches = [{'file': m['file'], 'label': m['played_label'], 'mode': m['mode'],
                'map': m['map']} for m in group]
    # A set entry has no single match, but it does have a first one: the scope
    # bar's "This match" and the auto-opened section need something to point
    # at. The map-wide and corpus-wide entries deliberately get nothing.
    if not match and scope_kind == 'set' and scope_files:
        match = scope_files[0]

    scope = {
        'kind': scope_kind,
        'files': scope_files,
        # The radar this page is bound to. The overlay can only draw one, so it
        # is fixed by the entry even when the query later widens to all maps.
        'map': matches[0]['map'] if matches else '',
        'entry': match or '',
    }
    # Which rail item lights up: the contextual "Analyse this match" when the
    # page is about a match, the global "Match Analyser" when it isn't.
    return render_template('multi.html', match_file=match or '', matches=matches,
                           scope=scope, match_ctx_dynamic=True,
                           nav_active='multi' if match else 'analyser')


def _serve_br(directory, br_name, mimetype):
    """Serve a brotli-precompressed file: as Content-Encoding: br when the
    client advertises it (browsers only do so on secure origins - localhost
    qualifies, plain-HTTP LAN does not), otherwise decompressed server-side."""
    if 'br' in request.accept_encodings:
        resp = send_from_directory(directory, br_name)
        resp.headers['Content-Encoding'] = 'br'
        resp.headers['Content-Type'] = mimetype
        resp.headers['Vary'] = 'Accept-Encoding'
        return resp
    with open(os.path.join(directory, br_name), 'rb') as f:
        payload = brotli.decompress(f.read())
    resp = app.response_class(payload, mimetype=mimetype)
    resp.headers['Vary'] = 'Accept-Encoding'
    return resp


@app.route('/data/<filename>')
def get_match_json(filename):
    """Serve a match document by its logical <id>.json name.

    Sends the stored .br bytes with Content-Encoding: br when the client
    advertises brotli, else decompresses server-side - plain-HTTP LAN browsers
    do not advertise it. The URL always keeps the .json suffix.
    """
    filename = os.path.basename(filename)
    if os.path.isfile(os.path.join(PROCESSED_FOLDER, filename)):
        return send_from_directory(PROCESSED_FOLDER, filename)
    if filename.endswith('.json') and os.path.isfile(os.path.join(PROCESSED_FOLDER, filename + '.br')):
        return _serve_br(PROCESSED_FOLDER, filename + '.br', 'application/json')
    abort(404)


# Grenade trajectories are ~95% of a decompressed match (205k points, 27.8 MB
# of ~29 MB) and exist only to draw nade trails. A page that loads several
# matches at once -- the analyser overlay -- would otherwise pay ~28 MB of JS
# heap per match for data it usually doesn't draw. This route serves the same
# document with `grenades` stripped; callers fetch the full one lazily if and
# when trails are switched on.
SLIM_OMIT_KEYS = ('grenades',)


def _slim_match_path(filename):
    """Build (once) and return the on-disk .slim.json.br for a match, or None
    if the match doesn't exist. Regenerated whenever the source is newer, the
    same mtime guard the .overwatch.json/.utility.json caches use."""
    src = _match_path(filename)
    if not src:
        return None
    stem = filename[:-5] if filename.endswith('.json') else filename
    dst = os.path.join(PROCESSED_FOLDER, stem + '.slim.json.br')
    try:
        if os.path.exists(dst) and os.path.getmtime(dst) >= os.path.getmtime(src):
            return dst
    except OSError:
        pass
    data = jsonio.load(src)
    for k in SLIM_OMIT_KEYS:
        data.pop(k, None)
    jsonio.dump_br(data, dst)
    return dst


@app.route('/data/slim/<filename>')
def get_match_json_slim(filename):
    """The match JSON minus the bulk arrays a map overlay never needs."""
    filename = os.path.basename(filename)
    if not filename.endswith('.json'):
        abort(404)
    dst = _slim_match_path(filename)
    if not dst:
        abort(404)
    return _serve_br(PROCESSED_FOLDER, os.path.basename(dst), 'application/json')


@app.route('/chunks/<match_id>/<filename>')
def serve_chunk(match_id, filename):
    """Serve one v2 tick chunk.

    A custom route rather than Flask's /static/ handler, which is what let the
    chunks directory move out of the source tree into data/ with no frontend
    change. Same brotli negotiation as get_match_json().
    """
    chunk_dir = os.path.join(CHUNKS_FOLDER, os.path.basename(match_id))
    filename = os.path.basename(filename)
    if filename.endswith('.br'):
        # v2 chunks are brotli-precompressed at parse time.
        return _serve_br(chunk_dir, filename, 'application/octet-stream')
    return send_from_directory(chunk_dir, filename)


@app.route('/radar/<name>.png')
def serve_radar(name):
    """A map's radar overview PNG, from the extracted overheadmaps assets.

    The files keep Valve's export names (`de_mirage_radar_psd.png`), while every
    caller asks for the plain canonical name (`/radar/de_mirage.png`, via
    MapsLogic.radarUrl). maps.radar_filenames() bridges the two, so callers
    never have to know which suffix is on disk.
    """
    for filename in maps.radar_filenames(os.path.basename(name)):
        if os.path.isfile(os.path.join(_MAP_IMG_DIR, filename)):
            return send_from_directory(_MAP_IMG_DIR, filename)
    abort(404)


@app.route('/assets/<path:filename>')
def serve_asset(filename):
    """Extracted CS2 UI images + their manifest, from paths.ASSETS_DIR.

    A route of its own rather than Flask's /static/: in a checkout the folder is
    static/assets/, but the frozen desktop build writes it under the user's data
    dir, and static/assets.js must not care which. send_from_directory refuses
    anything that escapes the folder.
    """
    return send_from_directory(paths.ASSETS_DIR, filename)


@app.route('/overwatch')
def overwatch():
    """Overwatch add-on: Tier-1 trust analysis for one match. Lazily runs the
    2nd-pass extractor on the original demo if its raw output is missing, then
    the Python analysis (cached). Everything degrades with a JSON error the
    tab can display - the add-on must never break the main app."""
    match = os.path.basename(request.args.get('match', ''))
    if not match.endswith('.json'):
        return {'ok': False, 'error': 'missing or invalid ?match='}, 400
    match_id = match[:-len('.json')]
    if not _match_path(match):
        return {'ok': False, 'error': f'unknown match {match_id}'}, 404

    force = request.args.get('refresh') == '1'
    raw_path = os.path.join(OVERWATCH_RAW_FOLDER, f'{match_id}.json')

    if force or not jsonio.resolve(raw_path):
        demo_name = _load_demo_map().get(match)
        demo_path = os.path.join(UPLOAD_FOLDER, demo_name) if demo_name else None
        if not demo_path or not os.path.isfile(demo_path):
            return {'ok': False, 'error':
                    'Original demo file not found - it is needed for the '
                    'Overwatch extraction. Re-upload the demo and re-process.'}, 409
        if not os.path.isfile(_OVERWATCH_EXE):
            return {'ok': False, 'error':
                    'Overwatch extractor binary not found. '
                    + paths.binary_missing_hint() + ' Then reload this tab.'}, 409
        os.makedirs(OVERWATCH_RAW_FOLDER, exist_ok=True)
        tmp_raw = raw_path + '.tmp'
        try:
            result = subprocess.run(
                [_OVERWATCH_EXE, '-demo', demo_path, '-out', tmp_raw],
                capture_output=True, text=True, timeout=600, **procutil.NO_WINDOW)
            if result.returncode != 0:
                raise RuntimeError(f'extractor exit {result.returncode}: '
                                   f'{result.stderr.strip()[:400]}')
            jsonio.compress_file(tmp_raw, raw_path + '.br', delete_src=True)
            if os.path.exists(raw_path):   # stale pre-compression twin would win resolve()
                os.unlink(raw_path)
        except Exception as e:
            if os.path.exists(tmp_raw):
                os.unlink(tmp_raw)
            return {'ok': False, 'error': f'Extraction failed: {e}'}, 500

    try:
        from analysis.overwatch import analyze
        labels = _load_labels()
        is_pro_match = match_id in _load_pro_matches()
        result = analyze(match_id, PROCESSED_FOLDER, OVERWATCH_RAW_FOLDER,
                         ANALYSIS_DATA_FOLDER, force=force, labels=labels,
                         is_pro_match=is_pro_match)
        # Overlay fresh labels (always current, even when result was cached).
        for p in result.get('players', []):
            entry = labels.get(p.get('steam_id', ''))
            p['player_label'] = entry.get('label') if entry else None
            # …and any in-game clip already recorded for a flagged moment, so
            # the tab shows it instead of offering to record it again. Applied
            # per request rather than stored in the .overwatch.json cache: the
            # cache outlives a recording, so a stored value would go stale (the
            # player Highlights reel does the same, see _with_clip_urls()).
            for m in (p.get('moments') or []):
                m['clip_url'] = None
                m['clip_short'] = None
                if m.get('start_tick') is None or m.get('end_tick') is None:
                    continue
                clip_file = clip_record.existing_clip_file(
                    match_id, m['start_tick'], m['end_tick'], p.get('name', ''))
                if clip_file:
                    m['clip_url'] = f'/clips/{match_id}/{clip_file}'
                    short = clip_record.clip_length_mismatch(
                        clip_record.clip_file_path(match_id, clip_file[:-len('.mp4')]),
                        m['start_tick'], m['end_tick'])
                    if short:
                        m['clip_short'] = {'actual': round(short[0], 2),
                                           'expected': round(short[1], 2)}
        result['is_pro_match'] = is_pro_match
        return result
    except Exception as e:
        return {'ok': False, 'error': f'Analysis failed: {e}'}, 500


@app.route('/utility-rating')
def utility_rating_route():
    """Utility Rating: per-player Quantity/Quality/Combined score for one
    match. Unlike /overwatch this needs no
    2nd-pass extractor or original demo - every input already lives in the
    processed match JSON - so there's no raw-extraction step, just a
    lazy-compute-cache-self-heal like the rest of the analysis/ package."""
    match = os.path.basename(request.args.get('match', ''))
    if not match.endswith('.json'):
        return {'ok': False, 'error': 'missing or invalid ?match='}, 400
    match_id = match[:-len('.json')]
    if not _match_path(match):
        return {'ok': False, 'error': f'unknown match {match_id}'}, 404

    force = request.args.get('refresh') == '1'
    try:
        from analysis.utility_rating import analyze
        labels = _load_labels()
        is_pro_match = match_id in _load_pro_matches()
        return analyze(match_id, PROCESSED_FOLDER, ANALYSIS_DATA_FOLDER,
                        force=force, labels=labels, is_pro_match=is_pro_match)
    except Exception as e:
        return {'ok': False, 'error': f'Analysis failed: {e}'}, 500


@app.route('/clip/record', methods=['POST'])
def clip_record_start():
    """Kick off a real in-game CS2 clip recording (clip_record.py) for one
    tick window of one match. Fire-and-poll, same shape as /process +
    /process/status/<job_id>: returns {ok, job_id} immediately, background
    thread does the actual (local-machine-only) work."""
    body = request.get_json(silent=True) or {}
    match = os.path.basename(body.get('match', ''))
    if not match.endswith('.json'):
        return {'ok': False, 'error': 'missing or invalid match'}, 400
    match_id = match[:-len('.json')]
    if not _match_path(match):
        return {'ok': False, 'error': f'unknown match {match_id}'}, 404

    try:
        start_tick = int(body.get('start_tick'))
        end_tick = int(body.get('end_tick'))
    except (TypeError, ValueError):
        return {'ok': False, 'error': 'start_tick/end_tick must be integers'}, 400
    focus_player = (body.get('focus_player') or '').strip()
    if not focus_player:
        return {'ok': False, 'error': 'missing focus_player'}, 400

    clip_settings = clip_record.load_settings()
    if not clip_settings.get('vac_risk_ack'):
        return {'ok': False, 'error':
                'Clip recording requires acknowledging the VAC-risk notice in '
                'Settings first (HLAE injects into the running CS2 process - '
                'see the Clip Recording card).'}, 409
    if not clip_record.settings_ready(clip_settings):
        return {'ok': False, 'error':
                'Clip recording is not configured - set CS2, HLAE, and ffmpeg '
                'paths in Settings first.'}, 409

    demo_name = _load_demo_map().get(match)
    demo_path = os.path.join(UPLOAD_FOLDER, demo_name) if demo_name else None
    if not demo_path or not os.path.isfile(demo_path):
        return {'ok': False, 'error':
                'Original demo file not found - it is needed to record a real '
                'clip. Re-upload the demo and re-process.'}, 409

    job_id = str(uuid.uuid4())
    with _CLIP_JOBS_LOCK:
        _CLIP_JOBS[job_id] = {'state': 'pending', 'clip_url': None, 'message': None,
                              'warning': None, 'error': None}

    threading.Thread(
        target=_clip_worker,
        args=(job_id, match_id, demo_path, start_tick, end_tick, focus_player),
        daemon=True,
    ).start()

    return {'ok': True, 'job_id': job_id}


@app.route('/demo/watch', methods=['POST'])
def demo_watch():
    """Open the original .dem in CS2, seeked to one tick and spectating one
    player - the "jump into the demo" buttons on the kill log, the Highlights
    and Overwatch moment rows, and the 2D replayer's control bar.

    Local-machine-only and offline, like /clip/record - but deliberately
    WITHOUT HLAE: nothing is injected into the game (see clip_record.py's
    watch-mode header). CS2 is launched plain with -insecure and left running
    for the user to watch. Synchronous: the work is writing one .cfg and
    spawning a process."""
    body = request.get_json(silent=True) or {}
    match = os.path.basename(body.get('match', ''))
    if not match.endswith('.json'):
        return {'ok': False, 'error': 'missing or invalid match'}, 400
    match_id = match[:-len('.json')]
    if not _match_path(match):
        return {'ok': False, 'error': f'unknown match {match_id}'}, 404
    try:
        tick = int(body.get('tick'))
    except (TypeError, ValueError):
        return {'ok': False, 'error': 'tick must be an integer'}, 400
    # Optional: without a name CS2 opens in free-roam at that tick.
    focus_player = (body.get('focus_player') or '').strip()

    demo_name = _load_demo_map().get(match)
    demo_path = os.path.join(UPLOAD_FOLDER, demo_name) if demo_name else None
    if not demo_path or not os.path.isfile(demo_path):
        return {'ok': False, 'error':
                'Original demo file not found - it is needed to open the demo '
                'in CS2. Re-upload the demo and re-process.'}, 409

    try:
        message = clip_record.launch_demo_at(demo_path, tick, focus_player)
    except clip_record.ClipError as e:
        return {'ok': False, 'error': str(e)}, 409
    except Exception as e:      # never let this take the app down
        return {'ok': False, 'error': f'Could not open the demo: {e}'}, 500
    return {'ok': True, 'message': message}


@app.route('/clip/status/<job_id>')
def clip_record_status(job_id):
    """Poll one clip-recording job.

    -> {ok, state, message, clip_url, warning, error}. `warning` carries the
    post-record VAC notice, which the UI shows beside the finished clip.
    """
    with _CLIP_JOBS_LOCK:
        job = _CLIP_JOBS.get(job_id)
    if not job:
        return {'ok': False, 'error': 'unknown job'}, 404
    return {'ok': True, **job}


@app.route('/clips/<match_id>/<filename>')
def serve_clip(match_id, filename):
    """Serve a recorded clip .mp4 from data/clips/<match_id>/."""
    clip_dir = os.path.join(CLIPS_FOLDER, os.path.basename(match_id))
    return send_from_directory(clip_dir, os.path.basename(filename))


@app.route('/overwatch/label', methods=['POST'])
def overwatch_label():
    """Persist a human verdict (cheater/clean/suspicious/null) for a player.
    Invalidates the current match's overwatch cache so the next load re-scores
    with updated baseline exclusions."""
    import time as _time
    body = request.get_json(silent=True) or {}
    steam_id = str(body.get('steam_id', '')).strip()
    label    = body.get('label')          # "cheater"|"clean"|"suspicious"|None
    name     = str(body.get('name', '')).strip()
    match    = os.path.basename(str(body.get('match', '')))

    if not steam_id:
        return {'ok': False, 'error': 'missing steam_id'}, 400
    if label not in ('cheater', 'clean', 'suspicious', 'professional', None):
        return {'ok': False, 'error': 'invalid label'}, 400

    with _LABELS_LOCK:
        labels = _load_labels()
        if label is None:
            labels.pop(steam_id, None)
        else:
            labels[steam_id] = {'label': label, 'name': name,
                                 'updated': int(_time.time())}
        _save_labels(labels)

    # Invalidate overwatch cache for this match - next GET will re-score
    # with the cheater excluded from (or restored to) baselines.
    if match and match.endswith('.json'):
        cache = os.path.join(PROCESSED_FOLDER, match[:-5] + '.overwatch.json')
        if os.path.isfile(cache):
            os.unlink(cache)

    return {'ok': True, 'steam_id': steam_id, 'label': label}


@app.route('/overwatch/match-label', methods=['POST'])
def overwatch_match_label():
    """Toggle a match as a professional match. Excludes all players from clean
    baselines and suppresses suspicion flags for that match."""
    body = request.get_json(silent=True) or {}
    match = os.path.basename(str(body.get('match', '')))
    if not match or not match.endswith('.json'):
        return {'ok': False, 'error': 'missing or invalid match'}, 400

    match_id = match[:-len('.json')]
    with _PRO_MATCHES_LOCK:
        pro_matches = _load_pro_matches()
        if match_id in pro_matches:
            pro_matches.discard(match_id)
            is_pro = False
        else:
            pro_matches.add(match_id)
            is_pro = True
        _save_pro_matches(pro_matches)

    # Invalidate overwatch cache so next GET re-scores with updated flag.
    cache = os.path.join(PROCESSED_FOLDER, match_id + '.overwatch.json')
    if os.path.isfile(cache):
        os.unlink(cache)

    return {'ok': True, 'match': match_id, 'is_pro_match': is_pro}


# ── Overwatch dashboard (global rail tab, /overwatch/dashboard) ────────────────
# Cross-match rollup of the per-match .overwatch.json analysis caches: one row
# per SteamID with cross-match persistence flags (the stage-4 headline shape,
# computed as a pure rollup - no SQLite ledger yet), plus a label console and a
# baseline-health panel. Pure *reader* of analysis outputs: it never imports
# analysis/ (add-on constraint) and never triggers extraction - matches appear
# here after their per-match Overwatch tab has been computed.

_BASELINES_PATH       = os.path.join(ANALYSIS_DATA_FOLDER, 'baselines.json')
_CHEAT_BASELINES_PATH = os.path.join(ANALYSIS_DATA_FOLDER, 'cheater_baselines.json')

_OW_MIN_KILLS_DEFAULT = 15   # mirrors analysis/overwatch.py MIN_KILLS_FOR_FLAGS
_OW_MIN_BASELINE      = 20   # mirrors analysis/overwatch.py MIN_BASELINE_FOR_FLAGS

_OW_LEVEL_ORDER = {'high': 0, 'elevated': 1, 'low': 2,
                   'insufficient': 3, 'verified_pro': 4}

# In-memory cache keyed by a fingerprint of every input file's mtime, same
# perf pattern as _players_aggregate_cached().
_OW_DASH = {'key': None, 'data': None}


# Premier tier colours, mirroring the match page's rank chips
# (templates/match.html premierTier() + `.rank-chip.p-*`) so a rank band reads
# the same colour on the dashboard as the player's rating does there.
# baselines.py's Premier buckets line up with the in-game tiers 1:1 except the
# top one, which pools 25k+ - i.e. both red (25–30k) and gold (30k+) - so it
# takes the red end rather than claiming gold for a 25k player.
_OW_PREMIER_TIER_CLASS = {
    'premier_0k_5k':    'p-gray',
    'premier_5k_10k':   'p-lblue',
    'premier_10k_15k':  'p-blue',
    'premier_15k_20k':  'p-purple',
    'premier_20k_25k':  'p-pink',
    'premier_25k_plus': 'p-red',
}


def _ow_band_tier(band):
    """CSS tier class for a Premier band key; '' for Competitive/Wingman skill
    groups and unbanded players (they keep the default text colour)."""
    return _OW_PREMIER_TIER_CLASS.get(band or '', '')


def _ow_band_label(band):
    """Human label for a baselines.py band key ('premier_15k_20k' → 'Premier
    15k–20k'). Unknown keys pass through unchanged."""
    if not band:
        return '-'
    if band.startswith('premier_'):
        parts = band.split('_')[1:]
        return 'Premier ' + (parts[0] + '+' if parts[-1] == 'plus'
                             else '–'.join(parts))
    if band.startswith('mm_'):
        return 'Comp SG ' + band[3:]
    if band.startswith('wingman_'):
        return 'Wingman SG ' + band[8:]
    return {'unbanded': 'Unbanded', 'all': 'All'}.get(band, band)


def _ow_dashboard_fingerprint():
    parts = []
    try:
        with os.scandir(PROCESSED_FOLDER) as it:
            for e in it:
                if e.name.endswith('.overwatch.json'):
                    try:
                        parts.append((e.name, int(e.stat().st_mtime)))
                    except OSError:
                        pass
    except OSError:
        pass
    for p in (LABELS_PATH, PRO_MATCHES_PATH,
              _BASELINES_PATH, _CHEAT_BASELINES_PATH):
        try:
            parts.append((os.path.basename(p), int(os.path.getmtime(p))))
        except OSError:
            parts.append((os.path.basename(p), 0))
    parts.append(('__total__', len(_list_match_files())))
    return tuple(sorted(parts))


def _ow_baseline_pool(path):
    """band -> {samples, players:set, matches:set} from a BaselineStore file.
    `samples` is the max sample count across features (features can have None
    values for some players, so counts vary per feature)."""
    try:
        with open(path, encoding='utf-8') as f:
            samples = json.load(f).get('samples', {})
    except (OSError, ValueError):
        samples = {}
    bands = {}
    for by_band in samples.values():
        for band, rows in by_band.items():
            b = bands.setdefault(band, {'samples': 0, 'players': set(),
                                        'matches': set()})
            b['samples'] = max(b['samples'], len(rows))
            for s in rows:
                b['players'].add(s.get('p'))
                b['matches'].add(s.get('m'))
    return bands


def _ow_dashboard_build():
    labels = _load_labels()
    pro_matches = _load_pro_matches()

    players = {}
    analyzed = []
    for fname in sorted(os.listdir(PROCESSED_FOLDER)):
        if not fname.endswith('.overwatch.json'):
            continue
        stem = fname[:-len('.overwatch.json')]
        try:
            with open(os.path.join(PROCESSED_FOLDER, fname), encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if not data.get('ok'):
            continue
        analyzed.append(stem)
        meta = _match_meta(stem + '.json')
        min_kills = data.get('min_kills', _OW_MIN_KILLS_DEFAULT)
        for p in data.get('players', []):
            sid = str(p.get('steam_id') or '')
            if not sid:
                continue
            a = players.setdefault(sid, {
                'steam_id': sid, 'name': p.get('name') or sid,
                'name_epoch': -1, 'band': '', 'band_epoch': -1,
                'matches': [], 'kills': 0, 'levels': {},
                'z_sum': 0.0, 'z_max': 0.0,
                'feat99': {},   # feature key -> {'label', 'n'}
            })
            kills = p.get('kills', 0)
            level = p.get('level', 'low')
            z = p.get('composite_z') or 0.0
            # A feature "flags" in this match when it clears the same gates the
            # per-match scorer uses (≥min kills, real baseline, ≥99th pct).
            flags99 = []
            if kills >= min_kills:
                for feat in p.get('features', []):
                    pct = feat.get('percentile')
                    if pct is not None and pct >= 99 \
                            and feat.get('n_baseline', 0) >= _OW_MIN_BASELINE:
                        flags99.append({'key': feat.get('key', ''),
                                        'label': feat.get('label') or feat.get('key', ''),
                                        'pooled': feat.get('pooled')})
            a['matches'].append({
                'file': stem + '.json', 'stem': stem, 'map': meta['map'],
                'played_epoch': meta['played_epoch'],
                'played_label': meta['played_label'],
                'level': level, 'z': round(z, 2), 'kills': kills,
                'band': p.get('band') or 'unbanded',
                'flags99': [f['label'] for f in flags99],
                'moments': len(p.get('moments') or []),
                'is_pro_match': stem in pro_matches,
            })
            epoch = meta['played_epoch']
            if epoch >= a['name_epoch']:
                a['name_epoch'] = epoch
                a['name'] = p.get('name') or a['name']
            if epoch >= a['band_epoch'] and p.get('band'):
                a['band_epoch'] = epoch
                a['band'] = p['band']
            a['kills'] += kills
            a['levels'][level] = a['levels'].get(level, 0) + 1
            a['z_sum'] += z
            a['z_max'] = max(a['z_max'], z)
            for f in flags99:
                e = a['feat99'].setdefault(f['key'], {'label': f['label'], 'n': 0,
                                                       'pooled': None, 'pooled_epoch': -1})
                e['n'] += 1
                # "Pooled" evidence is cached per-match (computed once inside
                # analyze(), see analysis/overwatch.py) - surface whichever
                # match's snapshot is most recent, same latest-wins pattern
                # as name/band above. Never re-derived here: this dashboard
                # stays a pure reader of the .overwatch.json caches, it never
                # imports analysis/.
                if f.get('pooled') is not None and epoch >= e['pooled_epoch']:
                    e['pooled_epoch'] = epoch
                    e['pooled'] = f['pooled']

    suspects, by_id = [], {}
    for sid, a in players.items():
        n = len(a['matches'])
        a['matches'].sort(key=lambda m: -m['played_epoch'])
        persistent = sorted(({'key': k, 'label': v['label'], 'n': v['n'],
                              'pooled': v.get('pooled')}
                             for k, v in a['feat99'].items() if v['n'] >= 2),
                            key=lambda f: -f['n'])
        lv = a['levels']
        peak = min(lv, key=lambda l: _OW_LEVEL_ORDER.get(l, 9)) if lv else 'low'
        entry = labels.get(sid)
        row = {
            'steam_id': sid, 'name': a['name'],
            'band': a['band'] or 'unbanded',
            'band_label': _ow_band_label(a['band'] or 'unbanded'),
            'band_tier': _ow_band_tier(a['band'] or 'unbanded'),
            'matches': n, 'kills': a['kills'],
            'n_high': lv.get('high', 0), 'n_elevated': lv.get('elevated', 0),
            'peak_level': peak,
            'z_mean': round(a['z_sum'] / n, 2) if n else 0.0,
            'z_max': round(a['z_max'], 2),
            'persistent': persistent,
            'flags99_once': sum(1 for v in a['feat99'].values() if v['n'] == 1),
            'label': entry.get('label') if entry else None,
        }
        # Review priority: cross-match persistence dominates, then per-match
        # HIGH/ELEVATED counts, then anomaly magnitude. Pros pin to the bottom
        # - elite stats are expected, not evidence.
        pro = row['label'] == 'professional' or peak == 'verified_pro'
        row['priority'] = 0.0 if pro else round(
            sum(f['n'] for f in persistent) * 100
            + row['n_high'] * 25 + row['n_elevated'] * 5 + row['z_max'], 2)
        suspects.append(row)
        by_id[sid] = dict(row, match_rows=a['matches'])
    suspects.sort(key=lambda r: (-r['priority'], -r['z_max'], -r['matches']))

    # Label console rows - every labeled SteamID, even ones with no analyzed
    # match in the current corpus (label survives match deletion by design).
    label_rows = []
    for sid, info in labels.items():
        agg = players.get(sid)
        label_rows.append({
            'steam_id': sid,
            'name': (agg or {}).get('name') or info.get('name') or sid,
            'label': info.get('label'),
            'updated': info.get('updated', 0),
            'matches': len(agg['matches']) if agg else 0,
        })
    label_rows.sort(key=lambda r: (r['label'] or '~', -(r['updated'] or 0)))
    label_counts = {}
    for r in label_rows:
        label_counts[r['label']] = label_counts.get(r['label'], 0) + 1

    # Baseline health - how much population each rank band's percentiles rest
    # on. `thin` marks bands below the per-match scorer's flag gate.
    clean = _ow_baseline_pool(_BASELINES_PATH)
    cheat = _ow_baseline_pool(_CHEAT_BASELINES_PATH)
    bands = [{'band': b, 'label': _ow_band_label(b), 'tier': _ow_band_tier(b),
              'samples': v['samples'], 'players': len(v['players']),
              'matches': len(v['matches']),
              'thin': v['samples'] < _OW_MIN_BASELINE}
             for b, v in clean.items()]
    bands.sort(key=lambda x: -x['samples'])
    cheat_players = set()
    for v in cheat.values():
        cheat_players |= v['players']

    total_matches = len(_list_match_files())
    raw_extracted = 0
    try:
        raw_extracted = len({f[:-len('.json.br')] if f.endswith('.json.br')
                             else f[:-len('.json')]
                             for f in os.listdir(OVERWATCH_RAW_FOLDER)
                             if f.endswith(('.json', '.json.br'))})
    except OSError:
        pass

    return {
        'suspects': suspects,
        'by_id': by_id,
        'labels': label_rows,
        'label_counts': label_counts,
        'pro_matches': sorted(pro_matches),
        'baseline': {
            'bands': bands,
            'min_baseline': _OW_MIN_BASELINE,
            'cheat_players': len(cheat_players),
            'cheat_samples': max((v['samples'] for v in cheat.values()), default=0),
            # Option B/C pools (not yet built): show progress toward the
            # ≥5-labeled threshold so the panel explains what unlocks them.
            'suspicious_labeled': label_counts.get('suspicious', 0),
            'clean_labeled': label_counts.get('clean', 0),
        },
        'coverage': {
            'total': total_matches,
            'analyzed': len(analyzed),
            'raw_only': max(0, raw_extracted - len(analyzed)),
            'unanalyzed': max(0, total_matches - len(analyzed)),
        },
    }


def _ow_dashboard_cached():
    key = _ow_dashboard_fingerprint()
    if _OW_DASH['key'] != key:
        _OW_DASH.update(key=key, data=_ow_dashboard_build())
    return _OW_DASH['data']


# Player-profile timing metrics: time-to-shoot/damage/kill (median seconds,
# from first spotting the eventual victim) and spotted-shot accuracy by
# weapon class - see analysis/features.py:compute_timing_features() for the
# derivation and why it needs this match's shots[] alongside overwatch_raw.
# Corpus-wide because the comparison baseline ("average of players who have
# a rank" - a flat average, not a rank-banded percentile) needs everyone's
# numbers, not just the one player's own; built once per raw-file change and
# shared by both _player_timing_features() and _ranked_avg_timing_features().
_TIMING_CORPUS = {'key': None, 'by_player': None}


def _timing_corpus_fingerprint():
    parts = []
    try:
        with os.scandir(OVERWATCH_RAW_FOLDER) as it:
            for e in it:
                if e.name.endswith(('.json', '.json.br')):
                    try:
                        parts.append((e.name, int(e.stat().st_mtime)))
                    except OSError:
                        pass
    except OSError:
        pass
    return tuple(sorted(parts))


def _timing_corpus_build():
    """{steam_id: [per-match timing dict, ...]} for every analyzed match -
    pure reader-and-derive over overwatch_raw + the already-cached
    .slim.json (for shots[]), same cost/caching shape as _ow_dashboard_build().
    Never triggers new Overwatch analysis; a match with no overwatch_raw yet
    just isn't in the result."""
    from analysis.features import compute_timing_features
    by_player = {}
    try:
        names = sorted(os.listdir(OVERWATCH_RAW_FOLDER))
    except OSError:
        names = []
    for fname in names:
        if not fname.endswith(('.json', '.json.br')):
            continue
        stem = fname[:-len('.json.br')] if fname.endswith('.json.br') else fname[:-len('.json')]
        try:
            raw = jsonio.load(os.path.join(OVERWATCH_RAW_FOLDER, fname))
        except (OSError, ValueError):
            continue
        shots = []
        slim_path = _slim_match_path(stem + '.json')
        if slim_path:
            try:
                shots = jsonio.load(slim_path).get('shots', [])
            except (OSError, ValueError):
                shots = []
        try:
            res = compute_timing_features(raw, shots=shots)
        except Exception:
            continue
        for pid, entry in res.items():
            by_player.setdefault(pid, []).append(entry)
    return by_player


def _timing_corpus_cached():
    key = _timing_corpus_fingerprint()
    if _TIMING_CORPUS['key'] != key:
        _TIMING_CORPUS.update(key=key, by_player=_timing_corpus_build())
    return _TIMING_CORPUS['by_player']


def _avg_timing_entries(entries):
    """Average a list of compute_timing_features()-shaped per-match dicts
    into one {'time_to_kill', 'time_to_shoot', 'time_to_damage', 'spotted_acc'}.
    A field absent (None) in a given match - not enough kills that match to
    measure it - is skipped rather than counted as 0, same "absent ≠ zero"
    rule the rest of this app follows."""
    out = {}
    for key in ('time_to_kill', 'time_to_shoot', 'time_to_damage'):
        vals = [e[key]['value'] for e in entries if e.get(key)]
        out[key] = round(statistics.mean(vals), 3) if vals else None
    acc_vals = {}
    for e in entries:
        for cls, v in (e.get('spotted_acc') or {}).items():
            acc_vals.setdefault(cls, []).append(v['value'])
    out['spotted_acc'] = {cls: round(statistics.mean(vs), 1) for cls, vs in acc_vals.items()}
    return out


def _player_timing_features(steam_id):
    """This player's own timing metrics, averaged across their analyzed
    matches. None when they have none yet (never an error - the caller
    degrades to a "not yet analyzed" message)."""
    if not steam_id:
        return None
    entries = _timing_corpus_cached().get(steam_id, [])
    if not entries:
        return None
    avg = _avg_timing_entries(entries)
    avg['matches'] = len(entries)
    return avg


_RANKED_TIMING_AVG = {'key': None, 'data': None}


def _ranked_avg_timing_features():
    """Flat average of the same timing metrics, pooled across every analyzed
    match belonging to a player who carries ANY rank (any rank_type, rank>0
    in _players_aggregate_cached()) - the player page's comparison baseline,
    per the request that prompted it: an average over ranked players, not a
    rank-banded percentile system like the Overwatch tab's."""
    key = _timing_corpus_fingerprint()
    if _RANKED_TIMING_AVG['key'] == key:
        return _RANKED_TIMING_AVG['data']
    by_player = _timing_corpus_cached()
    ranked_ids = {p['steam_id'] for p in _players_aggregate_cached()['list']
                  if p.get('steam_id') and p.get('rank') and (p['rank'].get('rank') or 0) > 0}
    pooled = [entry for pid in ranked_ids for entry in by_player.get(pid, [])]
    data = _avg_timing_entries(pooled) if pooled else None
    _RANKED_TIMING_AVG.update(key=key, data=data)
    return data


@app.route('/overwatch/dashboard')
def overwatch_dashboard():
    """Global Overwatch rollup across every analysed match.

    Pure reader: never imports analysis/, never triggers extraction. A match
    appears here only once its per-match tab (or the upload hook) has computed
    it.
    """
    d = _ow_dashboard_cached()
    return render_template('overwatch.html', dash=d, nav_active='overwatch')


@app.route('/overwatch/dashboard/player')
def overwatch_dashboard_player():
    """One suspect's full cross-match record (per-match rows) - lazy-loaded
    when a dashboard row is expanded (this panel stays inline, unlike the
    standalone /players/<name> page - a suspect card is a quick cross-reference
    while triaging the table, not a destination worth its own URL)."""
    sid = request.args.get('steam_id', '')
    row = _ow_dashboard_cached()['by_id'].get(sid)
    if not row:
        return {'ok': False, 'error': 'not found'}, 404
    return {'ok': True, 'player': row}


@app.route('/delete/<filename>', methods=['POST'])
def delete_match(filename):
    """Delete one match: its document, tick chunks, derived caches and demo map
    entry. POST only.
    """
    filename = os.path.basename(filename)
    if not filename.endswith('.json'):
        return {'error': 'invalid filename'}, 400

    base = os.path.join(PROCESSED_FOLDER, filename)
    found = False
    for p in (base, base + '.br'):
        if os.path.isfile(p):
            os.unlink(p)
            found = True
    if not found:
        return {'error': 'not found'}, 404

    stem = filename[:-5]
    chunks_dir = os.path.join(CHUNKS_FOLDER, stem)
    if os.path.isdir(chunks_dir):
        shutil.rmtree(chunks_dir)

    # Overwatch add-on artifacts for this match (cache + raw extract, either variant)
    for p in (os.path.join(PROCESSED_FOLDER, f'{stem}.overwatch.json'),
              os.path.join(OVERWATCH_RAW_FOLDER, f'{stem}.json'),
              os.path.join(OVERWATCH_RAW_FOLDER, f'{stem}.json.br')):
        if os.path.isfile(p):
            os.unlink(p)

    demo_map = _load_demo_map()
    if demo_map.pop(filename, None) is not None:
        _save_demo_map(demo_map)

    # Drop per-match tags/comment; the summary + players indexes self-prune on
    # their next sweep (deleted files fall out of _list_match_files()).
    with _MATCH_META_LOCK:
        meta = _load_match_meta()
        if meta.pop(filename, None) is not None:
            _save_match_meta(meta)

    return {'ok': True}


if __name__ == '__main__':
    app.run(port=8000, debug=True)
