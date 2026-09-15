"""paths.py - the single source of truth for on-disk locations.

Three roots:
  • the *source* tree (this repo)                         → ROOT
  • the read-only payload (templates, static, bin/)       → BUNDLE_ROOT
  • all mutable/generated runtime data                    → DATA_DIR

In a checkout BUNDLE_ROOT is ROOT. In the frozen desktop build (PyInstaller,
see desktop.py) it is the unpacked bundle, which is read-only once installed -
so DATA_DIR then defaults to the per-user data folder instead of ``<ROOT>/data``,
and ASSETS_DIR (written at runtime by the extractor and the manual import) moves
under DATA_DIR with it.

DATA_DIR is overridable with ``CS2VIEWER_DATA_DIR`` and ASSETS_DIR with
``CS2VIEWER_ASSETS_DIR`` in either mode. Every generated artifact - uploaded
demos, parsed matches, tick chunks, Overwatch data, analysis state, the
demo→filename map - lives under DATA_DIR. Compiled Go binaries live under
``<BUNDLE_ROOT>/bin`` (with a root fallback).

app.py and the analysis/ modules both import from here so there is exactly one
definition of where things go - previously app.py and analysis/steam_*.py
disagreed once CS2VIEWER_DATA_DIR was set.
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))

# ── Frozen desktop build ──────────────────────────────────────────────────────
# `sys.frozen` / `sys._MEIPASS` are set by PyInstaller's bootloader. Read them
# explicitly rather than trusting ROOT to land in the bundle - it does, today,
# but only as a side effect of where PyInstaller puts the module.
FROZEN = bool(getattr(sys, 'frozen', False))
BUNDLE_ROOT = getattr(sys, '_MEIPASS', None) or ROOT


def _default_data_dir():
    """<ROOT>/data in a checkout; the per-user data folder when frozen (an
    installed app's own directory is not writable)."""
    if not FROZEN:
        return os.path.join(ROOT, 'data')
    if os.name == 'nt':
        base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
        return os.path.join(base, 'CS2Viewer')
    base = (os.environ.get('XDG_DATA_HOME')
            or os.path.join(os.path.expanduser('~'), '.local', 'share'))
    return os.path.join(base, 'cs2viewer')


# ── Runtime data root ─────────────────────────────────────────────────────────
DATA_DIR = os.environ.get('CS2VIEWER_DATA_DIR') or _default_data_dir()

PROCESSED_DIR     = os.path.join(DATA_DIR, 'processed_matches')
DEMOS_DIR         = os.path.join(DATA_DIR, 'demos')
CHUNKS_DIR        = os.path.join(DATA_DIR, 'chunks')          # was static/chunks/
OVERWATCH_RAW_DIR = os.path.join(DATA_DIR, 'overwatch_raw')
ANALYSIS_DATA_DIR = os.path.join(DATA_DIR, 'analysis_data')
DEMO_MAP_PATH     = os.path.join(DATA_DIR, 'demo_map.json')
CLIPS_DIR         = os.path.join(DATA_DIR, 'clips')   # real in-game clip recordings (clip_record.py)
LOGS_DIR          = os.path.join(DATA_DIR, 'logs')    # desktop build's log file (desktop.py)

# CS2 game install directory - ONE shared setting used by every tool that
# needs a real CS2 install: clip_record.py (derives cs2.exe's path for its
# readiness check) and tools/extract_ui_assets.* (locates
# game/csgo/pak01_dir.vpk). A single plain-text file so there's exactly one
# CS2 path to configure app-wide, not a separate one per tool/feature.
CS2_GAME_DIR_FILE = os.path.join(ANALYSIS_DATA_DIR, 'cs2_game_dir')

# ── Source-tree assets (served directly) ──────────────────────────────────────
STATIC_DIR   = os.path.join(BUNDLE_ROOT, 'static')
TEMPLATE_DIR = os.path.join(BUNDLE_ROOT, 'templates')

# ── Extracted CS2 UI image assets (tools/extract_ui_assets.*) ─────────────────
# Skill-group icons, map icons, overhead maps and equipment icons pulled out of
# csgo/pak_01_dir.vpk with Source2Viewer. Valve content → gitignored, sourced
# locally; the directory's *presence* is the feature switch (missing → the app
# falls back to its text labels / CSS chips). Served by app.py's /assets/ route
# (never Flask's /static/, so the folder can live outside static/ in the frozen
# build); `manifest.json` (kind → {logical key: relative file}) is the index the
# frontend `static/assets.js` fetches.
ASSETS_DIR          = (os.environ.get('CS2VIEWER_ASSETS_DIR')
                       or (os.path.join(DATA_DIR, 'assets') if FROZEN
                           else os.path.join(STATIC_DIR, 'assets')))
ASSETS_MANIFEST     = os.path.join(ASSETS_DIR, 'manifest.json')
ASSET_KINDS         = ('skillgroups', 'map_icons', 'overheadmaps', 'equipment',
                       'deathnotice', 'premier')

# 2D radar PNGs are the extracted `overheadmaps` kind, served by /radar/ under
# Valve's export names (`de_mirage_radar_psd.png` - see maps.radar_filenames).
MAP_IMG_DIR         = os.path.join(ASSETS_DIR, 'overheadmaps')


def assets_present():
    """True when the extracted asset manifest exists (feature-gate probe)."""
    return os.path.isfile(ASSETS_MANIFEST)

# ── Map zone seed data (committed, read-only) ─────────────────────────────────
# Valve's env_cs_place callout boxes (LongA, MidDoors, ...) for every calibrated
# map, pre-extracted and shipped in the repo/bundle: <repo>/map_zones/<map>.json,
# small measured numbers rather than Valve art/textures - the same "committed
# transcription" precedent as MAP_LIBRARY's radar calibration, not the
# gitignored static/assets/ treatment. Read directly from BUNDLE_ROOT so a
# frozen build has zone data on first launch with no extraction step; a copy an
# install later extracts itself into ANALYSIS_DATA_DIR/map_zones/ (fresher, e.g.
# after a map update) takes precedence - see analysis/mapzones.py.
MAP_ZONES_SEED_DIR = os.path.join(BUNDLE_ROOT, 'map_zones')

# ── Compiled Go binaries ──────────────────────────────────────────────────────
def _find_binary(name):
    """Locate a Go binary: prefer <BUNDLE_ROOT>/bin, fall back to <BUNDLE_ROOT>.
    Platform-appropriate extension first, so a stray cross-built ``parser.exe``
    on a Linux dev box isn't picked over the native ``parser`` (a real Windows
    binary can't run on Linux)."""
    exts = [name + '.exe', name] if os.name == 'nt' else [name, name + '.exe']
    for d in (os.path.join(BUNDLE_ROOT, 'bin'), BUNDLE_ROOT):
        for exe in exts:
            p = os.path.join(d, exe)
            if os.path.isfile(p):
                return p
    return os.path.join(BUNDLE_ROOT, 'bin', name)   # not built yet → canonical target

PARSER_BIN    = _find_binary('parser')
OVERWATCH_BIN = _find_binary('overwatch')


def binary_missing_hint():
    """What to tell a user whose Go binary isn't there. A checkout builds it; an
    installed app has no Go toolchain, so 'run build.bat' would be nonsense."""
    if FROZEN:
        return 'The installation looks incomplete - reinstall CS2 Demo Viewer.'
    return 'Run build.bat / build.sh to produce it.'


def ensure_dirs():
    """Create every writable data dir (idempotent) - called once at startup so a
    fresh/empty DATA_DIR comes up clean instead of erroring on first write."""
    for d in (PROCESSED_DIR, DEMOS_DIR, CHUNKS_DIR, OVERWATCH_RAW_DIR, ANALYSIS_DATA_DIR, CLIPS_DIR):
        os.makedirs(d, exist_ok=True)
