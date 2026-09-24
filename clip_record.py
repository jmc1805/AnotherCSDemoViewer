r"""CS2 client control: opening a demo at a moment, and recording it.

Two features share this module because they share the same settings, the same
CS2 path helpers, and the same generated-cfg machinery:

  watch mode      Opens the original .dem in CS2 at a tick, spectating a player,
                  paused. Injects NOTHING - CS2 is launched normally and steered
                  over its own TCP console (-netconport). Entry: launch_demo_at().
  clip recording  Launches CS2 *through* HLAE, captures a frame sequence and
                  muxes it to data/clips/<match>/<clip_id>.mp4. Entry:
                  record_clip(). This one does inject a hook DLL.

Layout of this file: settings + path resolution, then .cfg generation (pure and
unit-tested), then the netcon console client, then the launch/record flows.

Raw docstring on purpose: the text quotes Windows paths, and `\x64` in HLAE's
`-hookDllPath ...\x64\AfxHookSource2.dll` is a valid hex escape that silently
renders the folder name as `d`.

Three rules that are not obvious from the code and have each cost a real bug:

  1. Close a console socket with _netcon_close(), NEVER sock.close(). Unread
     data in the receive buffer turns the close into an RST, which discards
     data still in flight - including the spec_player written just before it.
  2. `spec_player` goes BEFORE `demo_gototick`, and there is no `spec_mode`
     line. The opposite arrangement was tried on theory, unverified, and was
     strictly worse. Do not change it without testing a real recording.
  3. Nothing here may emit a command that contacts a server. cfg generation is
     playdemo-only and assert_offline_only() re-checks every line before it is
     written; record_clip() additionally requires settings['vac_risk_ack'].

Verification status: the pure helpers are covered by test/clip_record_test.py
and run anywhere. The live launch/record/mux path needs Steam, CS2, HLAE and a
GPU, so it is the least-proven code in the repo.
"""

import hashlib
import struct
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time

import paths
import procutil  # NO_WINDOW - never for the CS2/HLAE launches, which must be seen

SETTINGS_PATH = os.path.join(paths.ANALYSIS_DATA_DIR, 'clip_settings.json')

TICKRATE = 64

# Heuristic wait for CS2 to launch (via HLAE's custom loader), inject the
# hook, and load the demo up to the target tick before record_clip() starts
# counting the actual clip duration. HLAE gives no "ready" signal to poll
# for, so this is a guess, not a measured figure - increase it if recordings
# keep coming up with zero captured frames.
GAME_STARTUP_SECONDS = 15

# Where the CS2 executable lives under a CS2 install directory - the standard
# Steam layout. Used both to sanity-check that the configured game directory
# really is a CS2 install, and (on Windows) as -programPath for HLAE's custom
# loader (see hlae_launch_args()) - record_clip() launches CS2 through HLAE,
# never this path directly.
#
# The Linux depot ships an EMPTY bin/win64 beside bin/linuxsteamrt64, so the
# Windows path must not be the one probed there: watch_ready() would say "no
# CS2" on a perfectly good install. _CS2_EXE_SUFFIX_WIN stays as its own name
# because load_settings() migrates an old *Windows* cs2_path with it.
IS_LINUX = sys.platform.startswith('linux')
_CS2_EXE_SUFFIX_WIN = os.path.join('game', 'bin', 'win64', 'cs2.exe')
_CS2_EXE_SUFFIX_LINUX = os.path.join('game', 'bin', 'linuxsteamrt64', 'cs2')
_CS2_EXE_SUFFIX = _CS2_EXE_SUFFIX_LINUX if IS_LINUX else _CS2_EXE_SUFFIX_WIN


def cs2_exe_path(game_dir):
    return os.path.join(game_dir, _CS2_EXE_SUFFIX)


def cs2_cfg_dir(game_dir):
    """Where Source's `exec` command actually looks for .cfg files -
    "//<path id>/cfg/<filename>", never an arbitrary path (Valve Developer
    Wiki's `exec` page). The generated .cfg has to be written here, not into
    this app's own clip_dir, or +exec silently finds nothing and none of
    build_cfg_lines()'s commands ever run - see the module docstring's bug
    #3 note."""
    return os.path.join(game_dir, 'game', 'csgo', 'cfg')


def load_cs2_game_dir():
    """CS2 install directory, read from the single file shared by every tool
    in this app that needs one (paths.CS2_GAME_DIR_FILE - see its docstring):
    tools/extract_ui_assets.* already reads this exact file, and the
    Settings page's CS2 Installation card writes it. One canonical path
    instead of a separate one per feature."""
    if not os.path.isfile(paths.CS2_GAME_DIR_FILE):
        return None
    with open(paths.CS2_GAME_DIR_FILE, encoding='utf-8') as f:
        return f.read().strip() or None


def save_cs2_game_dir(value):
    os.makedirs(paths.ANALYSIS_DATA_DIR, exist_ok=True)
    value = (value or '').strip()
    if value:
        tmp = paths.CS2_GAME_DIR_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(value + '\n')
        os.replace(tmp, paths.CS2_GAME_DIR_FILE)
    else:
        try:
            os.remove(paths.CS2_GAME_DIR_FILE)
        except OSError:
            pass

RISK_NOTICE = (
"HLAE injects a DLL into CS2 to capture frames. While used safely here "
"for offline demo review, this workflow isn't officially sanctioned by "
"Valve. CS2 force-closes after recording as a safeguard, but verify it "
"is completely closed before joining VAC-secured servers."
)

POST_RECORD_WARNING = (
    "Could not confirm CS2 closed automatically - fully close it yourself "
    "now and restart it (without -insecure) before joining any official/"
    "VAC-secured match."
)

POST_RECORD_CLOSED_CONFIRMATION = (
    "CS2 was automatically closed after recording. Safe to relaunch "
    "normally (without -insecure) for official/VAC-secured play."
)

# Commands that would make the generated .cfg do anything other than local,
# offline demo playback. assert_offline_only() rejects a cfg containing any
# of these - see the VAC-risk note above.
_FORBIDDEN_TOKENS = ('connect ', 'connect_', 'matchmaking', 'mm_', 'retry')

# Common install locations probed by autodetect_paths(). Always
# user-overridable in Settings - these are just a convenience first guess.
_CS2_CANDIDATES = [
    r"C:\Program Files (x86)\Steam\steamapps\common\Counter-Strike Global Offensive",
]
_CS2_STEAM_DIRNAME = 'Counter-Strike Global Offensive'
_LINUX_STEAM_ROOTS = (
    '~/.local/share/Steam',
    '~/.steam/steam',
    '~/.var/app/com.valvesoftware.Steam/.local/share/Steam',  # Flatpak
)


def linux_cs2_candidates(home=None):
    """CS2 install dirs under every Steam library on this Linux user's account:
    the three usual Steam roots, plus any extra library listed in each root's
    libraryfolders.vdf (a second drive is where CS2's ~40 GB usually ends up).
    Order preserved, duplicates dropped."""
    import re
    roots = [os.path.expanduser(r) if home is None else os.path.join(home, r[2:])
             for r in _LINUX_STEAM_ROOTS]
    libs = list(roots)
    for root in roots:
        vdf = os.path.join(root, 'steamapps', 'libraryfolders.vdf')
        try:
            with open(vdf, encoding='utf-8', errors='replace') as f:
                text = f.read()
        except OSError:
            continue
        libs += re.findall(r'"path"\s+"([^"]+)"', text)
    seen, out = set(), []
    for lib in libs:
        cand = os.path.join(lib, 'steamapps', 'common', _CS2_STEAM_DIRNAME)
        if cand not in seen:
            seen.add(cand)
            out.append(cand)
    return out


_HLAE_CANDIDATES = [
    r"C:\Program Files\HLAE\HLAE.exe",
    r"C:\HLAE\HLAE.exe",
]
_FFMPEG_CANDIDATES = [
    r"C:\ffmpeg\bin\ffmpeg.exe",
    r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
]


# ── Capture options (resolution, HUD, crosshair) ─────────────────────────────
#
# All three are recorded-output choices rather than plumbing, so they live in
# clip_settings.json beside the tool paths and are edited on the Settings page.
#
# RESOLUTION IS A LAUNCH OPTION, NOT A CVAR. HLAE captures whatever the game
# renders, so the only lever is what CS2 opens at: -w/-h. They are passed
# together with -windowed, because an exclusive-fullscreen client generally
# ignores a requested size and uses the desktop mode instead. "Game default"
# passes none of the three, which is exactly the behaviour before this
# existed - so it is always the way back if a custom size misbehaves.
#
# EVEN DIMENSIONS ARE MANDATORY, not tidiness: record_clip() encodes with
# -pix_fmt yuv420p, whose chroma planes are half-resolution, and libx264
# refuses an odd width or height outright ("height not divisible by 2").
# A custom size is snapped down to even rather than rejected.
CLIP_MIN_DIM = 320
CLIP_MAX_DIM = 7680

# Offered on the Settings page. Aspect is in the label because that is the
# part a viewer actually notices; the numbers are the part ffmpeg needs.
CLIP_RESOLUTION_PRESETS = (
    ('Game default', None, None),
    ('1280 x 720 (16:9)', 1280, 720),
    ('1600 x 900 (16:9)', 1600, 900),
    ('1920 x 1080 (16:9)', 1920, 1080),
    ('2560 x 1440 (16:9)', 2560, 1440),
    ('1280 x 800 (16:10)', 1280, 800),
    ('1680 x 1050 (16:10)', 1680, 1050),
    ('1024 x 768 (4:3)', 1024, 768),
    ('1280 x 960 (4:3)', 1280, 960),
)


def normalize_resolution(width, height):
    """(width, height) as even ints inside [CLIP_MIN_DIM, CLIP_MAX_DIM], or
    (None, None) meaning "leave CS2 at whatever it is set to".

    Both or neither: a width with no height cannot describe a frame, so a
    half-filled pair degrades to the game default rather than guessing the
    other half from an aspect ratio the user never stated."""
    try:
        w, h = int(width), int(height)
    except (TypeError, ValueError):
        return (None, None)
    if w <= 0 or h <= 0:
        return (None, None)
    w = max(CLIP_MIN_DIM, min(CLIP_MAX_DIM, w))
    h = max(CLIP_MIN_DIM, min(CLIP_MAX_DIM, h))
    return (w - (w % 2), h - (h % 2))


def frame_bytes_per_second(width, height, tickrate=TICKRATE):
    """Rough on-disk cost of the intermediate frame sequence, for the
    Settings page to show before someone picks 2560x1440.

    HLAE's default afxClassic preset writes UNCOMPRESSED 24-bit TGA, so this
    is simply w*h*3 per frame at one frame per tick - and it is not a detail:
    at 1920x1080 a 15 s clip is about 5.6 GB of TGA on the way to a ~13 MB
    .mp4. The frames are deleted after a successful encode, but they all
    exist at once first."""
    if not width or not height:
        return None
    return width * height * 3 * tickrate


# ── Settings I/O (small mutable plain JSON, same tier as labels.json) ────────

def load_settings():
    """hlae_path/ffmpeg_path/vac_risk_ack live in clip_settings.json;
    cs2_game_dir is read from the shared paths.CS2_GAME_DIR_FILE (see
    load_cs2_game_dir()) rather than duplicated in this file, so there's one
    CS2 path for every tool, not a clip-recording-specific copy."""
    data = {}
    if os.path.isfile(SETTINGS_PATH):
        with open(SETTINGS_PATH, encoding='utf-8') as f:
            data = json.load(f)

    game_dir = load_cs2_game_dir()
    legacy_cs2_path = data.get('cs2_path')
    if not game_dir and legacy_cs2_path and legacy_cs2_path.endswith(_CS2_EXE_SUFFIX_WIN):
        # One-time migration: this key used to store the full cs2.exe path
        # per-tool. Derive the shared game directory from it so existing
        # configuration isn't silently lost by the switch to a single
        # app-wide cs2_game_dir setting.
        candidate = legacy_cs2_path[:-len(_CS2_EXE_SUFFIX_WIN)].rstrip('\\/')
        if os.path.isfile(cs2_exe_path(candidate)):
            save_cs2_game_dir(candidate)
            game_dir = candidate

    width, height = normalize_resolution(data.get('clip_width'),
                                        data.get('clip_height'))
    return {
        'cs2_game_dir': game_dir,
        'hlae_path': data.get('hlae_path'),
        'ffmpeg_path': data.get('ffmpeg_path'),
        'vac_risk_ack': bool(data.get('vac_risk_ack')),
        # Capture options. Defaults reproduce the behaviour that existed
        # before they were configurable: CS2's own resolution, no HUD (see
        # hud_cfg_lines), crosshair left on.
        'clip_width': width,
        'clip_height': height,
        'clip_hud': bool(data.get('clip_hud', False)),
        'clip_crosshair': bool(data.get('clip_crosshair', True)),
    }


def save_settings(settings):
    """Writes hlae_path/ffmpeg_path/vac_risk_ack to clip_settings.json.
    cs2_game_dir, if present in `settings`, is routed to the shared
    save_cs2_game_dir() instead - it's an app-wide setting, not
    clip-recording-specific, so it doesn't belong in this JSON file."""
    if 'cs2_game_dir' in settings:
        save_cs2_game_dir(settings.get('cs2_game_dir'))
    # Normalised on the way in as well as the way out, so a hand-edited
    # clip_settings.json with an odd or absurd size can't reach ffmpeg.
    width, height = normalize_resolution(settings.get('clip_width'),
                                         settings.get('clip_height'))
    os.makedirs(paths.ANALYSIS_DATA_DIR, exist_ok=True)
    tmp = SETTINGS_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({
            'hlae_path': settings.get('hlae_path') or None,
            'ffmpeg_path': settings.get('ffmpeg_path') or None,
            # Persisted separately from the paths on purpose: re-detecting or
            # editing a path must never silently re-arm recording - the user
            # has to consciously re-check this box (settings_ready() below).
            'vac_risk_ack': bool(settings.get('vac_risk_ack')),
            'clip_width': width,
            'clip_height': height,
            'clip_hud': bool(settings.get('clip_hud', False)),
            'clip_crosshair': bool(settings.get('clip_crosshair', True)),
        }, f, indent=2)
    os.replace(tmp, SETTINGS_PATH)


def settings_ready(settings=None):
    """True once every path needed to actually record (not just spectate)
    is configured and points at a real file/install, AND the user has
    explicitly acknowledged the VAC-risk notice (RISK_NOTICE) - having valid
    paths alone is deliberately not sufficient to arm the feature. Also
    checks hlae_hook_dll_path(hlae_path) exists (AfxHookSource2.dll next to
    HLAE.exe) - an incomplete/wrong HLAE install would otherwise only be
    caught at record_clip() time instead of showing as "not ready" upfront."""
    s = settings or load_settings()
    if not s.get('vac_risk_ack'):
        return False
    game_dir = s.get('cs2_game_dir')
    if not game_dir or not os.path.isfile(cs2_exe_path(game_dir)):
        return False
    hlae_path = s.get('hlae_path')
    if not hlae_path or not os.path.isfile(hlae_path):
        return False
    if not os.path.isfile(hlae_hook_dll_path(hlae_path)):
        return False
    ffmpeg_path = s.get('ffmpeg_path')
    return bool(ffmpeg_path) and os.path.isfile(ffmpeg_path)


def autodetect_paths(search_roots=None):
    """Best-guess paths for CS2/HLAE/ffmpeg by probing common install
    locations (or, for tests, an injected list of candidates). CS2
    candidates are game *directories* - a candidate counts as found when
    cs2_exe_path(candidate) exists, matching cs2_game_dir's shape. Never
    overwrites explicit user settings - callers merge this in only for keys
    the user hasn't already set."""
    def first_existing_file(cands):
        for c in cands:
            if os.path.isfile(c):
                return c
        return None

    def first_existing_cs2_dir(cands):
        for c in cands:
            if os.path.isfile(cs2_exe_path(c)):
                return c
        return None

    if search_roots is not None:
        # Test hook: search_roots is {'cs2': [...], 'hlae': [...], 'ffmpeg': [...]}
        # - cs2 entries are game-directory candidates, not exe paths.
        return {
            'cs2_game_dir': first_existing_cs2_dir(search_roots.get('cs2', [])),
            'hlae_path': first_existing_file(search_roots.get('hlae', [])),
            'ffmpeg_path': first_existing_file(search_roots.get('ffmpeg', [])),
        }
    return {
        'cs2_game_dir': first_existing_cs2_dir(
            linux_cs2_candidates() if IS_LINUX else _CS2_CANDIDATES),
        'hlae_path': first_existing_file(_HLAE_CANDIDATES),
        'ffmpeg_path': first_existing_file(_FFMPEG_CANDIDATES),
    }


# ── Clip identity / persistence ───────────────────────────────────────────────
#
# Clip ids are derived from what the clip *is* (match + tick window + focus
# player) rather than being random, so the same highlight always maps to the
# same <clip_id>.mp4 on disk. That's what makes an already-recorded clip
# findable on a later page load: recording a clip used to be discoverable
# only through the in-memory job dict in app.py, which dies with the process,
# so the .mp4 survived on disk but nothing could ever match it back to its
# highlight again.

def clip_id_for(match_id, start_tick, end_tick, focus_player):
    """Stable 12-hex id for one highlight's clip. Same inputs -> same id, so
    existing_clip_file() can find a previously recorded clip and re-recording
    overwrites in place rather than piling up near-duplicate files."""
    key = f'{match_id}|{int(start_tick)}|{int(end_tick)}|{focus_player}'
    return hashlib.sha1(key.encode('utf-8')).hexdigest()[:12]


def clip_file_path(match_id, clip_id):
    return os.path.join(paths.CLIPS_DIR, match_id, f'{clip_id}.mp4')


def existing_clip_file(match_id, start_tick, end_tick, focus_player):
    """'<clip_id>.mp4' if this exact clip has already been recorded and is
    still on disk, else None. Cheap enough (one stat) to call per highlight
    row on every page load."""
    clip_id = clip_id_for(match_id, start_tick, end_tick, focus_player)
    path = clip_file_path(match_id, clip_id)
    return f'{clip_id}.mp4' if os.path.isfile(path) else None


# ── Does the footage actually match the window it is filed under? ────────────
#
# THE CLIP ID IS CONTENT-ADDRESSED, AND THAT IS THE ONLY THING GUARANTEEING
# THE TWO BOXES ARE THE SAME LENGTH. `clip_id_for()` hashes the tick window,
# so under normal operation a file existing at that id *is* footage of that
# window: `record_clip()` is the only writer and it captures exactly
# end_tick - start_tick frames at TICKRATE (bug #6).
#
# Two things break the invariant, and both are silent:
#
#   - A capture that ended early. `wait_for_capture()` returns whatever frame
#     count it saw when the count stopped growing or the timeout expired, and
#     `record_clip()` muxes that - a truncated clip is written under the full
#     window's id with nothing to distinguish it.
#   - Re-keying an old file onto a new window. Done once, on 2026-09-10, to
#     preserve six recordings when the round-end clamp changed; it kept the
#     files but made the app assert footage matched a window it was never
#     recorded for, which is exactly the "no parity in clip length" this
#     function now answers. Renaming a content-addressed file is a lie about
#     its content - don't do it again without this check in place.
#
# So the length is measured rather than assumed. Reading it is cheap: an mp4
# carries its duration in the `mvhd` box inside `moov`, a few dozen bytes,
# with no ffprobe subprocess and no decode.
CLIP_LENGTH_TOLERANCE = 0.15   # seconds; a frame or two of muxing slack


def mp4_duration_seconds(path):
    """Duration of an .mp4 in seconds from its `mvhd` box, or None if the
    file can't be read or isn't shaped like one. Pure stdlib, reads only the
    box headers - never the media payload."""
    try:
        with open(path, 'rb') as fh:
            return _mvhd_duration(fh, 0, os.path.getsize(path))
    except (OSError, ValueError, struct.error):
        return None


def _mvhd_duration(fh, start, end, _depth=0):
    """Walk the ISO-BMFF box tree from `start` to `end` looking for
    moov/mvhd. Recurses only into `moov`, so this reads a handful of headers
    rather than scanning the file."""
    if _depth > 2:
        return None
    pos = start
    while pos + 8 <= end:
        fh.seek(pos)
        header = fh.read(8)
        if len(header) < 8:
            return None
        size, kind = struct.unpack('>I4s', header)
        body = pos + 8
        if size == 1:                      # 64-bit extended size
            size = struct.unpack('>Q', fh.read(8))[0]
            body += 8
        elif size == 0:                    # box runs to end of file
            size = end - pos
        if size < 8 or pos + size > end:
            return None
        if kind == b'moov':
            found = _mvhd_duration(fh, body, pos + size, _depth + 1)
            if found is not None:
                return found
        elif kind == b'mvhd':
            fh.seek(body)
            version = fh.read(1)[0]
            fh.seek(body + 4)              # skip version+flags
            if version == 1:
                fh.read(16)                # 64-bit creation/modification
                timescale, duration = struct.unpack('>IQ', fh.read(12))
            else:
                fh.read(8)                 # 32-bit creation/modification
                timescale, duration = struct.unpack('>II', fh.read(8))
            return duration / timescale if timescale else None
        pos += size
    return None


def clip_length_mismatch(path, start_tick, end_tick, tickrate=TICKRATE):
    """(actual_seconds, expected_seconds) when the file on disk is shorter
    than its tick window by more than CLIP_LENGTH_TOLERANCE, else None.

    Only *short* counts. A clip that overruns is the pre-bug-#6 shape and is
    still watchable footage of the moment; one that is short is missing the
    end of the play, which is usually the whole point of the clip."""
    expected = max(0.0, (int(end_tick) - int(start_tick)) / float(tickrate))
    actual = mp4_duration_seconds(path)
    if actual is None or expected <= 0:
        return None
    if expected - actual > CLIP_LENGTH_TOLERANCE:
        return (actual, expected)
    return None


# ── Launch / paths ────────────────────────────────────────────────────────────
#
# There used to be a clip_duration_seconds() here, used by record_clip() to
# sleep for "however long the clip should take". It's gone: the recording
# now stops at its own scheduled end tick (build_seek_cfg_lines()) and
# record_clip() waits on the actual frame count instead (wait_for_capture()),
# because host_timescale 0 means wall-clock time doesn't correspond to
# captured demo time at all. Sleeping a computed duration was exactly what
# produced a 186s clip for a ~45s round.

def hlae_hook_dll_path(hlae_exe_path):
    """AfxHookSource2.dll - the CS2 hook - lives in HLAE's x64/ subfolder,
    next to HLAE.exe itself (github.com/advancedfx/advancedfx wiki:
    "select the AfxHookSource2.dll located in the x64 sub-folder for
    HookDLL"). Not configurable separately in Settings - it's always found
    relative to hlae_path."""
    return os.path.join(os.path.dirname(hlae_exe_path), 'x64', 'AfxHookSource2.dll')


def hlae_launch_args(hlae_exe, cs2_exe, hook_dll, cfg_name, insecure=True,
                     width=None, height=None):
    """argv for launching CS2 *through* HLAE's "custom loader", which is
    what actually injects hook_dll into the game process before its main
    thread resumes - doc.hlae.site's "Command Line Interfaces" guide and the
    AfxHookSource2 introduction page. Launching cs2.exe directly (e.g. via a
    steam:// URL) starts a plain, un-hooked CS2 that has none of the
    mirv_streams console commands build_cfg_lines() relies on. That failure is
    silent - the game runs, the cfg execs, and recording simply captures zero
    frames - so the launch has to go through HLAE, not merely alongside it.

    insecure=True (the default, and what record_clip() always uses) passes
    -insecure to CS2 in -cmdLine - HLAE's own docs state CS2 requires it to
    run hooked at all, and it's also this module's VAC-risk mitigation (see
    the module docstring): a client that never initializes VAC can't be
    actioned by it, and can't join VAC-secured servers until restarted
    without the flag.

    -steam and -console are unconditional. -steam matches the one confirmed
    example in doc.hlae.site's launch guide (for CS:GO, but the only working
    reference found) - a Source engine game launched directly (bypassing
    Steam's own launcher, as -programPath does) can refuse to run standalone
    without it and exit immediately. That is what happens here in practice
    without the flag, verified on a real install (2026-08-18): CS2's window
    appears and closes again within a second or two, before any frames are
    captured. -console opens CS2's own console window so a launch/exec
    failure is visible on screen instead of the game just vanishing with no
    diagnostics reaching this process (HLAE spawns CS2 as its own child, not
    ours, so we can't capture its output any other way)."""
    width, height = normalize_resolution(width, height)

    flags = ['-steam', '-console']
    if insecure:
        flags.append('-insecure')
    if width and height:
        # HLAE captures whatever the game renders, so the recorded
        # resolution is decided here and nowhere else. -windowed rides along
        # because an exclusive-fullscreen client generally ignores a
        # requested size and uses the desktop mode instead. Passing none of
        # the three (the "Game default" setting) is exactly the behaviour
        # that existed before this was configurable.
        flags += ['-windowed', '-w', str(width), '-h', str(height)]
    cmdline = ' '.join(flags) + f' +exec {cfg_name}'
    return [
        hlae_exe,
        '-customLoader', '-noGui', '-autoStart',
        '-hookDllPath', hook_dll,
        '-programPath', cs2_exe,
        '-cmdLine', cmdline,
    ]


class ClipError(Exception):
    pass


# ── .cfg generation ───────────────────────────────────────────────────────────
#
# THREE .cfg files, not one, because two separate CS2 operations are
# asynchronous and each needs the next step deferred until it has actually
# landed:
#
#   init.cfg  playdemo …                      -> defers to seek.cfg at tick 1
#   seek.cfg  demo_gototick (start - preroll) -> defers to rec.cfg at start
#   rec.cfg   spec POV + mirv_streams record start
#
# (1) `playdemo` is asynchronous. Commands after it in the *same* exec'd cfg
#     raced the load and ran before the demo was playing at all - see the
#     module docstring's bug #4.
# (2) `demo_gototick` is likewise asynchronous, AND the seek resets the
#     spectator target. Setting spec_player before the seek (as this module
#     originally did) therefore worked only sometimes: whether the POV stuck
#     depended on the seek finishing first - bug #7.
#
# The obvious fix for both - a `wait` between steps - doesn't work: Source's
# `wait` console command is documented as unavailable in CS:GO/CS2. HLAE's own
# `mirv_cmd addAtTick <tick> "<command>"` (confirmed for Source2/CS2:
# github.com/advancedfx/advancedfx/wiki/Source2:Commands lists mirv_cmd as
# "Execute commands on a specific time or tick") only fires once playback
# actually *reaches* a tick, which can't happen before the preceding async
# step has completed. So each stage schedules the next by tick.

# NOTE - do not "improve" the spectator handling in build_seek_cfg_lines()
# without testing against a real recording. See the module docstring's bug #7:
# `spec_player` before `demo_gototick`, with no `spec_mode` line at all, is the
# empirically working arrangement. Every theory-driven change to it (moving
# spec_player after the seek so the seek couldn't "reset" it, adding
# `spec_mode 4` for first person, seeking to a preroll tick first) made the POV
# strictly worse - a permanently free-roaming camera.


# Hiding the in-game HUD. These belong in the SEEK cfg, not beside `playdemo`.
#
# They used to sit in the init cfg, and observably never applied: a clip
# recorded 2026-09-10 has the full HUD drawn - scoreboard, radar, killfeed and
# CS2's own demo playback bar. That is bug #4 of the module docstring wearing a
# different hat. `playdemo` is asynchronous, so anything after it in the same
# exec'd cfg races the demo load; every other post-load command was moved into
# the deferred seek cfg when that was found, and these two were left behind in
# the racing one.
#
# It matters more than "the clip has a HUD on it", which is why it is worth its
# own note: the HUD rendered into a capture is STALE. In that same clip the
# killfeed showed exactly one entry, appearing ~7s after the kill it described
# and never updating again, while the scoreboard held every player at 100 HP
# for the full 14s - including players who were already dead. A viewer counting
# frags off that killfeed reads a 4K as a 1K, which is precisely how this was
# reported. The world itself is correct (verified frame by frame: demo time
# advances exactly 1.0s per 1.0s of video, POV stays on the focus player, all
# four kills are in frame) - it is only the HUD layer that freezes, and the
# prime suspect is the unproven `host_timescale 0` in build_seek_cfg_lines().
# Hiding the HUD sidesteps that entirely; leaving it visible but frozen is the
# one outcome that actively misinforms.
#
# `sv_cheats 1` first because CS2 flags several client draw cvars as cheat-
# protected. It is inert if `cl_drawhud` is not one of them, it touches no
# network (demo playback is local, and `-insecure` is already passed for the
# session), and it is not in _FORBIDDEN_TOKENS.
# `cl_showfps 0` is unconditional - a frame counter burnt into a highlight is
# never wanted, whatever the HUD setting says.
#
# CROSSHAIR IS EMITTED SEPARATELY AND ITS INDEPENDENCE IS NOT VERIFIED. In
# CS:GO `cl_drawhud 0` took the crosshair with it, and whether CS2 still does
# is untested here - so `crosshair` is written AFTER `cl_drawhud`, giving it
# the last word if the two are in fact independent, and the Settings page says
# plainly that the crosshair toggle may not survive turning the HUD off. This
# module has a documented history (bug #7) of "fixing" spectator behaviour on
# theory and making it worse; stating the uncertainty beats asserting either
# answer.
def hud_cfg_lines(hud=False, crosshair=True):
    """Console lines that set what the recorded frames show.

    `sv_cheats 1` leads because CS2 flags several client draw cvars as
    cheat-protected; it is inert if `cl_drawhud` is not one of them, and it
    touches no network (demo playback is local, `-insecure` is already on).

    A visible HUD in a capture is STALE - see the note above this function's
    section: the killfeed freezes and the scoreboard stops updating, which is
    what made a 4K read as a 1K. Turning it on is therefore an explicit
    choice, defaulting to off."""
    return (
        'sv_cheats 1',
        'cl_drawhud 1' if hud else 'cl_drawhud 0',
        'crosshair 1' if crosshair else 'crosshair 0',
        'cl_showfps 0',
    )


def build_init_cfg_lines(dem_path, clip_name):
    """Runs immediately at CS2 launch: loads the demo and schedules the
    seek/record .cfg (build_seek_cfg_lines()) to run once playback reaches
    tick 1 - guaranteed to be after the demo has loaded, since ticks can't
    advance before that. See this section's header comment for why a
    single-cfg/`wait`-based approach doesn't work here.

    Deliberately carries NOTHING but those two lines: anything else here
    races the async `playdemo`. The HUD-hiding commands used to live here
    and silently never applied - see HUD_OFF_LINES."""
    return [
        f'playdemo "{dem_path}"',
        f'mirv_cmd addAtTick 1 "exec {clip_name}_seek.cfg"',
    ]


# How far past the clip's start tick to re-assert the spectator target.
# Deliberately NOT exactly start_tick: demo_gototick *jumps* to that tick, and
# it's unclear whether mirv_cmd addAtTick fires for a tick arrived at by a
# seek or only for one reached by normal playback. A few ticks later is
# reached by playback either way. The end-of-clip stop is scheduled the same
# way and is known to fire, but that tick is always reached by playback, so
# it doesn't answer the question. ~0.12s of possibly-wrong POV at the very
# head of the clip is a cheap price for the re-assert firing reliably.
SPEC_REASSERT_OFFSET_TICKS = 8


def spec_reassert_line(start_tick, end_tick, focus_player):
    """A `mirv_cmd addAtTick` line that re-issues spec_player just after
    playback reaches the clip, or None when the name can't be expressed
    safely or there's no room to schedule it.

    mirv_cmd takes the command to run as a quoted string, and a Source
    config has no escape for a quote inside a quoted string - so a name
    containing `"` can't be embedded at all. Names containing spaces are
    fine unquoted here: spec_player treats the rest of the line as the name
    (and CS2 name matching is prefix-based anyway), and the surrounding
    mirv_cmd quotes keep it one argument.

    Returning None simply means this clip relies on the pre-seek
    spec_player alone, exactly as before - never a failure."""
    if '"' in focus_player:
        return None
    tick = int(start_tick) + SPEC_REASSERT_OFFSET_TICKS
    if tick >= int(end_tick):
        # Clip too short to re-assert inside it without racing the stop.
        return None
    return f'mirv_cmd addAtTick {tick} "spec_player {focus_player}"'


def build_seek_cfg_lines(start_tick, end_tick, focus_player, clip_name, frames_dir,
                         hud=False, crosshair=True):
    """Scheduled by build_init_cfg_lines() (via mirv_cmd addAtTick) to run
    only once the demo is confirmed actively playing: points the camera at
    the focus player, seeks to the clip's start tick, then starts an HLAE
    frame-sequence recording into frames_dir and schedules its stop.

    ORDER IS LOAD-BEARING AND EMPIRICAL: `spec_player` must come *before*
    `demo_gototick`, and no `spec_mode` line may be added. This looks wrong
    - the seek "should" reset the spectator target - but it is the only
    arrangement observed to actually produce the player's POV. Reordering it
    on that theory, and adding `spec_mode 4` for first person, produced a
    permanently free-roaming camera instead (module docstring bug #7).

    Command names follow the official advancedfx wiki
    (github.com/advancedfx/advancedfx/wiki/Source2:mirv_streams,
    doc.hlae.site's "Your first recording" guide) rather than guesswork -
    but see the module docstring's verification caveat:

      - hud_cfg_lines() sets what the frames show (HUD, crosshair). It
        lives in this cfg, not the init one, because a cvar set beside
        `playdemo` is lost to the same async load that bug #4 describes -
        measured: a real clip came out with the full HUD drawn and its
        killfeed frozen on a single stale entry, which made a 4K read as a
        1K. Both toggles come from Settings (clip_settings.json).
      - `mirv_streams add normal <name>` registers a stream to record -
        without one, `mirv_streams record start` has nothing to capture.
      - `mirv_streams record name "<path>"` takes an absolute path (or one
        relative to the CS2 install dir); passing frames_dir here is what
        makes HLAE actually write frames where record_clip() looks for them
        afterward, instead of its undocumented default (the CS2 install
        dir's game/bin/win64).
      - `mirv_streams record fps <n>` sets the capture rate to TICKRATE so
        it matches the framerate record_clip() muxes the frames at - without
        it the capture rate is whatever HLAE defaults to, and the encoded
        clip would play back at the wrong speed.
      - `host_framerate <n>` / `host_timescale 0` put the engine into
        frame-stepped capture - advance exactly one simulation frame per
        rendered/captured frame, driven by HLAE rather than the real clock,
        so capture is deterministic instead of dependent on render speed.
        Pairing host_framerate with `mirv_streams record start` is the
        documented practice in both reference sources: a real working
        community HLAE .cfg (github.com/abandonedpools/hlae-cfg-csgo) and an
        HLAE setup guide's recommended bind. NOTE: these two lines were
        added while chasing what turned out to be a frame-*discovery* bug
        (module docstring #5) and are unproven here - real captures
        succeeded without them.
      - The scheduled `mirv_streams record end` bounds the clip at its real
        end tick. Without it recording only ever stopped when record_clip()
        killed CS2, so the length was "however much demo time got rendered
        during a fixed wall-clock sleep" - and since host_timescale 0 lets
        playback outrun real time, that badly overshot: a ~45s round
        produced a 186s clip (11902 frames). Stopping by *tick* makes the
        length exactly end_tick-start_tick regardless of render speed.
        host_framerate 0 restores normal engine timing.

    The output image format (HLAE's default "afxClassic" preset writes an
    uncompressed TGA sequence) and the frame numbering (zero-padded
    00000.tga, 00001.tga …, nested under take<NNNN>/<stream_name>/) are
    confirmed against real output, but aren't published in the docs - so
    record_clip() discovers whatever HLAE actually wrote rather than
    hardcoding that layout. See find_recorded_frames()."""
    lines = [
        f'spec_player "{focus_player}"',
        f'demo_gototick {int(start_tick)} 1',
    ]
    # Re-assert the spectator target once playback has actually reached the
    # clip. The spec_player above runs at demo tick 1, moments after
    # playdemo, when the player entities almost certainly aren't populated
    # yet - so the name can't resolve and CS2 falls back to a default
    # target, which is why recordings followed *a* player but the wrong one.
    # Scheduling by tick guarantees the demo is really there, with players
    # present, when the name is looked up.
    #
    # This is an *addition*, not a replacement: the pre-seek call is what
    # establishes first-person-on-someone in the first place, and removing
    # it (or adding spec_mode) is what produced a free-roaming camera -
    # module docstring bug #7. Only names without a double quote are
    # re-asserted, since mirv_cmd's argument is itself a quoted string and
    # Source configs give no way to nest quotes inside it.
    reassert = spec_reassert_line(start_tick, end_tick, focus_player)
    if reassert:
        lines.append(reassert)
    lines += [
        *hud_cfg_lines(hud=hud, crosshair=crosshair),
        f'mirv_streams add normal {clip_name}',
        f'mirv_streams record name "{frames_dir}"',
        f'mirv_streams record fps {TICKRATE}',
        f'host_framerate {TICKRATE}',
        'host_timescale 0',
        'mirv_streams record start',
        f'mirv_cmd addAtTick {int(end_tick)} '
        f'"mirv_streams record end; host_framerate 0"',
    ]
    return lines


def assert_offline_only(cfg_lines):
    """Defense-in-depth guard against a future edit accidentally introducing
    a command that would connect CS2 to a live server while HLAE is loaded
    (see the module docstring's VAC-risk note). Raises ClipError - never
    silently strips the offending line, since that could mask a real bug."""
    lowered = [ln.lower() for ln in cfg_lines]
    for token in _FORBIDDEN_TOKENS:
        if any(token in ln for ln in lowered):
            raise ClipError(
                f'Refusing to write a clip .cfg containing {token!r} - this '
                'module must only ever drive offline demo playback.')


def write_cfgs(init_cfg_path, seek_cfg_path, dem_path, start_tick,
               end_tick, focus_player, clip_name, frames_dir,
               hud=False, crosshair=True):
    """Writes both .cfg files (see this section's header comment for why
    there are two). seek_cfg_path's basename must be
    f'{clip_name}_seek.cfg' - build_init_cfg_lines() hardcodes that name in
    its scheduled `exec`, since a mirv_cmd addAtTick command can't reference
    a Python variable.

    Nothing is written unless every line of every file passes
    assert_offline_only(), so a rejected cfg can't leave a half-written set
    behind in the CS2 install."""
    init_lines = build_init_cfg_lines(dem_path, clip_name)
    seek_lines = build_seek_cfg_lines(start_tick, end_tick, focus_player,
                                      clip_name, frames_dir,
                                      hud=hud, crosshair=crosshair)
    assert_offline_only(init_lines + seek_lines)
    for path, lines in ((init_cfg_path, init_lines),
                        (seek_cfg_path, seek_lines)):
        with open(path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
    return init_cfg_path, seek_cfg_path


# ── Watch mode: jump into the demo in CS2 ─────────────────────────────────────
#
# The "open this moment in CS2" buttons (kill log, Highlights tab, Overwatch
# moments, the 2D replayer) land here. Unlike record_clip() this deliberately
# does NOT involve HLAE: nothing is injected into the game, because nothing
# here needs HLAE's mirv_* commands. HLAE exists in this module for one job -
# frame-accurate capture - and watching a moment isn't it. CS2 is launched
# directly, plain, with -insecure.
#
# The cost of dropping HLAE is the loss of `mirv_cmd addAtTick`, the only way
# to defer a console command until the demo has actually loaded (`playdemo` is
# asynchronous and Source's `wait` doesn't exist in CS2 - see the module
# docstring's bug #4). So the seek can't fire by itself. Instead the cfg binds
# the whole jump - spectate the player, seek to the moment, pause - to one key
# (WATCH_JUMP_KEY): the demo opens at the start, the user presses it once, and
# lands exactly where the button meant. That is the entire trade: one keypress
# instead of a hook DLL in the game process.
#
# Consequently watch_ready() needs neither HLAE nor ffmpeg - only a CS2 install
# - and there is no VAC-risk acknowledgement gate here, since the acknowledged
# risk (RISK_NOTICE) is specifically about HLAE injection. -insecure still
# means VAC never loads for this session, so CS2 must be restarted normally
# before playing an official match; launch_demo_at()'s return message says so.

WATCH_CFG_NAME = 'cs2viewer_watch'      # <name>.cfg in game/csgo/cfg
WATCH_JUMP_KEY = 'F8'

# How far before the moment to land, so it has some run-up instead of starting
# on the kill itself. Same idea as the Rounds & Kills "Open in replay" link,
# which rewinds 2s.
WATCH_PREROLL_TICKS = TICKRATE * 5

# A name is embedded unquoted inside the bind's own quoted string (a Source
# config has no escape for a nested quote), so a name containing either of
# these would break the bind or inject a second command into it. Such a player
# simply gets the seek without the POV - never a broken cfg.
_UNBINDABLE_NAME_CHARS = ('"', ';')


def watch_ready(settings=None):
    """True when a demo can be opened in CS2 - just a real CS2 install.

    Deliberately weaker than settings_ready(): watching needs no ffmpeg (no
    encoding), no HLAE (nothing is hooked - see this section's header), and no
    VAC-risk acknowledgement (RISK_NOTICE covers HLAE injection, which watch
    mode doesn't do)."""
    s = settings or load_settings()
    game_dir = s.get('cs2_game_dir')
    return bool(game_dir) and os.path.isfile(cs2_exe_path(game_dir))


def cs2_is_running(timeout=10):
    """True when a CS2 process is already running. A second CS2 can't start
    while one is up, so the launch would fail with nothing on screen to explain
    why - better to say so first. Returns False when the process lister isn't
    available (e.g. the dev container), so this can only ever add a clearer
    error, never block a launch that would have worked."""
    if IS_LINUX:
        try:
            # -x: exact process name, so `cs2viewer`/`cs2.sh` never match.
            result = subprocess.run(['pgrep', '-x', 'cs2'], capture_output=True,
                                    text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0
    try:
        result = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq cs2.exe'],
                                capture_output=True, text=True, timeout=timeout,
                                **procutil.NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return 'cs2.exe' in (result.stdout or '').lower()


def watch_landing_tick(tick):
    """Where a jump actually lands: WATCH_PREROLL_TICKS before the moment, so
    it has some run-up instead of opening on the kill itself."""
    return max(0, int(tick) - WATCH_PREROLL_TICKS)


def watch_jump_command(tick, focus_player, offset=0):
    """The one console command line that jumps to the moment: spectate the
    player, seek, pause. Bound to a key rather than executed at startup,
    because it can only work once the demo has finished loading.

    `spec_player` before `demo_gototick` and no `spec_mode` line - the same
    empirically-established order build_seek_cfg_lines() documents at length
    (module docstring bug #7). Paused on arrival so the moment doesn't run
    away while the user is still reading the screen; they press space to play.

    `offset` nudges the seek target by a tick and exists only for netcon_jump()
    - see _netcon_try_jump() for why. The key bind always uses offset 0.
    """
    landing = watch_landing_tick(tick) + offset
    parts = []
    if focus_player and not any(c in focus_player for c in _UNBINDABLE_NAME_CHARS):
        # Unquoted on purpose: spec_player takes the rest of its arguments as
        # the name (and CS2 matches names by prefix), and a nested quote can't
        # be expressed inside the bind's quoted string anyway.
        parts.append(f'spec_player {focus_player}')
    parts.append(f'demo_gototick {landing}')
    parts.append('demo_pause')
    return '; '.join(parts)


def build_watch_cfg_lines(dem_path, tick, focus_player, jump_key=WATCH_JUMP_KEY):
    """The whole watch cfg: arm the jump key, load the demo, and print what to
    press. Bind first, playdemo second - `playdemo` is asynchronous, so any
    line after it may run while the demo is still loading; a bind is instant
    and session-wide, so arming it up front is what makes the ordering safe."""
    jump = watch_jump_command(tick, focus_player)
    landing = max(0, int(tick) - WATCH_PREROLL_TICKS)
    who = f' as {focus_player}' if focus_player else ''
    return [
        f'bind {jump_key} "{jump}"',
        f'playdemo "{dem_path}"',
        f'echo "[CS2 Demo Viewer] Press {jump_key} to jump to tick {landing}{who}."',
    ]


def write_watch_cfg(cfg_dir, dem_path, tick, focus_player, cfg_name=WATCH_CFG_NAME):
    """Writes the watch .cfg into CS2's cfg dir (Source `exec` only resolves
    there - see cs2_cfg_dir()). One fixed name per install rather than a
    per-moment name: a watch cfg is consumed immediately at launch and has no
    artifact to trace back to, unlike a clip's cfg.

    Nothing is written unless every line passes assert_offline_only()."""
    lines = build_watch_cfg_lines(dem_path, tick, focus_player)
    assert_offline_only(lines)
    path = os.path.join(cfg_dir, f'{cfg_name}.cfg')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    return path


# ── Netcon: driving the jump without a keypress ───────────────────────────────
#
# CS2 still supports Source's TCP console (`-netconport <port>`) - verified
# empirically on a real install (2026-08-18): the socket accepted a connection
# ~1s after launch, `spec_player X; demo_gototick N; demo_pause` executed, and
# the game's own console stream came back with
#   "Demo Skipping: skipping to demo tick 19680 …"
#   "Demo Skipping finished at tick 19680"
#   "CGameRules - paused on tick 22243"
#
# That is what replaces HLAE's `mirv_cmd addAtTick` here, and it's strictly
# better: mirv_cmd guesses a tick to fire on, whereas netcon lets us watch the
# console and act when the demo has ACTUALLY loaded and the seek has ACTUALLY
# finished. No injection, no hook DLL - just a socket.
#
# Two things learned in that same session, both designed around below:
#   - An unknown command produces NO output on the netcon stream, so silence
#     never proves success - the jump is retried until the game prints its own
#     "Demo Skipping finished" line (netcon_jump's loop explains why probing
#     for readiness first doesn't work).
#   - The listener binds 0.0.0.0, not loopback, and CS2 offers no way to
#     restrict it - so it's opened on a random high port, only for the life of
#     the CS2 process, and the F8 bind stays in the cfg as a fallback for when
#     netcon can't be reached at all.

NETCON_CONNECT_TIMEOUT = 90     # s to wait for CS2 to open the port at all
NETCON_READY_TIMEOUT   = 240    # s to keep retrying the jump while the demo loads
NETCON_RETRY_SECONDS   = 4      # gap between attempts
NETCON_CLOSE_DRAIN     = 2      # s spent draining the socket before closing it
_NETCON_SEEK_DONE      = 'demo skipping finished'


def _netcon_close(sock, drain_seconds=NETCON_CLOSE_DRAIN):
    """Close a console socket without throwing away what was just written.

    This is not tidiness, it is the difference between the last command
    arriving and being discarded. CS2 streams console output continuously and
    the functions here read only the lines they need, so there is essentially
    always unread data sitting in our receive buffer. Closing a socket in that
    state makes the OS abort the connection with an RST instead of a graceful
    FIN, and an RST **discards data still in flight** - so the final
    `spec_player`, written a microsecond before the close, never reaches the
    game. Measured on loopback against a stand-in console, that lost the POV
    re-assert in roughly one run in five: exactly the "the recording followed
    the wrong player" symptom the re-assert exists to prevent.

    The graceful sequence is half-close (flushes our side and sends EOF), then
    read until the peer closes back or the deadline passes, then close.
    """
    try:
        sock.shutdown(socket.SHUT_WR)
    except OSError:
        pass                    # already gone; there is nothing left to flush
    try:
        sock.settimeout(0.2)
        deadline = time.time() + drain_seconds
        while time.time() < deadline:
            if not sock.recv(4096):
                break           # peer closed: everything we sent was accepted
    except OSError:
        pass                    # a reset at this point costs nothing
    try:
        sock.close()
    except OSError:
        pass


def pick_free_port():
    """An ephemeral port the OS says is free right now. CS2's console listener
    binds 0.0.0.0 (see this section's header), so a fixed well-known port would
    be both guessable and prone to colliding with whatever else is running -
    this is asked for fresh per launch and handed straight to CS2."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _netcon_connect(port, timeout=NETCON_CONNECT_TIMEOUT):
    """Connect to CS2's console, retrying until it exists. Returns the socket,
    or None if CS2 never opened it (wrong build, blocked port, launch failed)
    - in which case the caller falls back to the F8 bind."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            sock = socket.create_connection(('127.0.0.1', port), timeout=3)
            sock.settimeout(2)
            return sock
        except OSError:
            time.sleep(2)
    return None


def _netcon_drain(sock, seconds):
    """Read whatever the game has printed for up to `seconds`, lowercased.
    The console stream is chatty and asynchronous, so callers match on
    substrings rather than expecting a reply to line up with a request."""
    end = time.time() + seconds
    chunks = []
    while time.time() < end:
        try:
            data = sock.recv(8192)
            if not data:
                break
            chunks.append(data)
        except socket.timeout:
            pass
        except OSError:
            break
    return b''.join(chunks).decode('utf-8', 'replace').lower()


def _netcon_try_jump(sock, tick, focus_player, offset=0):
    """Send the jump once and report whether the game confirmed the seek.

    Confirmation is the game's own "Demo Skipping finished" line, not silence:
    an ignored or too-early command prints nothing at all on this channel (see
    the section header), so silence can only ever mean "not yet".

    `offset` alternates the target between the landing tick and one tick past
    it, because CS2 also prints NOTHING for a seek to the tick it is already
    sitting on - indistinguishable from a seek ignored because the demo hadn't
    loaded. Observed on a real run (2026-08-18): once an earlier attempt had
    parked the demo on the target, every later attempt looked like a failure
    and the loop spun until it timed out. Alternating guarantees at least one
    of any two consecutive attempts is a real move."""
    for line in watch_jump_command(tick, focus_player, offset).split('; '):
        sock.sendall(line.encode('utf-8') + b'\n')
    return _NETCON_SEEK_DONE in _netcon_drain(sock, 5)


def netcon_jump(port, tick, focus_player, on_log=None):
    """Connect to a launched CS2 and drive it to the moment. Returns True when
    the seek was confirmed by the game's own console output.

    Runs on a background thread (launch_demo_at) because CS2 takes tens of
    seconds to reach the demo, and the HTTP request that started it must not
    wait for that. Failure is never fatal - the F8 bind in the watch cfg is
    still armed, so the user can always jump manually."""
    def log(msg):
        if on_log:
            on_log(msg)

    sock = _netcon_connect(port)
    if not sock:
        log(f'netcon: CS2 never opened port {port}; leaving the F8 bind to do the job')
        return False
    try:
        # Retry until the game confirms, rather than trying to detect "the
        # demo is ready" first. Detecting readiness turned out to be the hard
        # part: `demo_info` answers from the demo's FILE HEADER while the map
        # is still loading, so an early probe reports playback data and the
        # seek that follows is silently ignored - observed on a real run
        # (2026-08-18): the jump was sent, nothing happened, nothing printed.
        # Seeking to the same tick is idempotent, so simply asking again every
        # few seconds until "Demo Skipping finished" comes back is both
        # simpler and strictly more reliable than any readiness heuristic.
        deadline = time.time() + NETCON_READY_TIMEOUT
        attempts, offset, seeked = 0, 0, False
        while time.time() < deadline and not seeked:
            attempts += 1
            offset = attempts % 2          # 1, 0, 1, 0 … (see _netcon_try_jump)
            seeked = _netcon_try_jump(sock, tick, focus_player, offset)
            if not seeked:
                time.sleep(NETCON_RETRY_SECONDS)
        if seeked and offset:
            # Landed one tick past the mark on an odd attempt - step back onto
            # it. This one is always a real move, so it is never silent.
            sock.sendall(f'demo_gototick {watch_landing_tick(tick)}\n'.encode('utf-8'))
            sock.sendall(b'demo_pause\n')
        # Re-assert the POV now that the seek has landed: this is the moment
        # the HLAE path can only approximate with addAtTick, and the reason
        # its recordings sometimes followed the wrong player.
        if seeked and focus_player and not any(
                c in focus_player for c in _UNBINDABLE_NAME_CHARS):
            sock.sendall(f'spec_player {focus_player}\n'.encode('utf-8'))
            sock.sendall(b'demo_pause\n')
        log(f'netcon: jumped to the moment (attempt {attempts})' if seeked
            else 'netcon: the game never confirmed the seek; press '
                 f'{WATCH_JUMP_KEY} in game to jump manually')
        return seeked
    except OSError as e:
        log(f'netcon: connection lost ({e})')
        return False
    finally:
        _netcon_close(sock)


def _cs2_game_flags(cfg_name, netcon_port, insecure, steam_flag):
    """The flags CS2 itself receives, shared by both launch routes. -steam is
    for a binary started directly, so it is left out when Steam does the
    launching."""
    flags = ['-console']
    if steam_flag:
        flags.insert(0, '-steam')
    if insecure:
        flags.append('-insecure')
    if netcon_port:
        flags += ['-netconport', str(netcon_port)]
    return flags + ['+exec', cfg_name]


def cs2_watch_launch_args(cs2_exe, cfg_name, netcon_port=None, insecure=True):
    """argv for launching CS2 directly - no HLAE, no hook DLL, no injection.

    -steam is what hlae_launch_args() passes too: a Source engine binary
    started directly (rather than by Steam's launcher) can refuse to run
    without it. -console opens the game console so the cfg's echo - the
    "press F8" hint - is visible, and so a failed exec is diagnosable.
    -insecure keeps VAC out of the session entirely; it isn't required for
    plain demo playback the way it is for a hooked client, but it costs
    nothing here and keeps this feature's posture identical to the
    recorder's. +exec runs the watch cfg written by write_watch_cfg().

    -netconport opens the TCP console netcon_jump() drives to perform the
    jump itself; omit it (netcon_port=None) and the cfg's F8 bind is the only
    way to reach the moment."""
    return [cs2_exe] + _cs2_game_flags(cfg_name, netcon_port, insecure, True)


CS2_APP_ID = '730'


def steam_client_argv(game_dir):
    """argv prefix that talks to the user's Steam client, or None if there is
    none to be found. Linux only.

    Linux CS2 cannot be started by running its binary: cs2.sh (and so the
    game) aborts with "not launched within the Steam for Linux sniper runtime
    environment" unless Steam started it. So the launch goes through the
    client, which forwards everything after the app id to the game as launch
    options - `steam -applaunch 730 -console +exec x` is what a Steam launch
    option field would do. A Flatpak Steam is recognised by where the game
    lives, since that install is invisible to a plain `steam` on PATH."""
    if '.var/app/com.valvesoftware.Steam' in game_dir.replace(os.sep, '/'):
        flatpak = shutil.which('flatpak')
        return [flatpak, 'run', 'com.valvesoftware.Steam'] if flatpak else None
    steam = shutil.which('steam')
    if steam:
        return [steam]
    # No launcher on PATH: the client's own script sits beside steamapps/.
    root = game_dir.replace(os.sep, '/').split('/steamapps/')[0]
    script = os.path.join(root, 'steam.sh')
    return [script] if os.path.isfile(script) else None


def cs2_steam_launch_args(game_dir, cfg_name, netcon_port=None, insecure=True):
    """argv that has Steam start CS2 with the watch cfg. See steam_client_argv()
    for why. Raises ClipError when there is no Steam client to ask."""
    client = steam_client_argv(game_dir)
    if not client:
        raise ClipError(
            'Could not find the Steam client to launch CS2 with. Start Steam '
            'yourself, or make sure `steam` is on your PATH. CS2 on Linux '
            'cannot be started without it.')
    return client + ['-applaunch', CS2_APP_ID] + _cs2_game_flags(
        cfg_name, netcon_port, insecure, False)


def _child_env():
    """The environment for a helper process this app did not build. A frozen
    PyInstaller app points LD_LIBRARY_PATH at its own bundle and keeps the
    user's original in LD_LIBRARY_PATH_ORIG; leaving the bundle's on Steam
    makes it load our libraries instead of its own."""
    env = dict(os.environ)
    if 'LD_LIBRARY_PATH_ORIG' in env:
        env['LD_LIBRARY_PATH'] = env.pop('LD_LIBRARY_PATH_ORIG')
        if not env['LD_LIBRARY_PATH']:
            del env['LD_LIBRARY_PATH']
    return env


# ── The live watch session ────────────────────────────────────────────────────
#
# The netcon socket stays usable for as long as CS2 is up, so a second 🎮 click
# does NOT need a fresh game: it reuses the running one. Same demo -> just
# seek again (a second or two). Different demo -> `playdemo` the new file over
# the console and seek once it has loaded. Only when nothing of ours is running
# does a click actually launch CS2.
#
# The session is keyed on the Popen handle, not just the port: once CS2 exits,
# its ephemeral port goes back to the pool and could later belong to something
# else entirely - reconnecting to that and firing console commands at it would
# be both useless and rude. `proc.poll()` is the authoritative "is our game
# still there" check.

# On Linux the handle is Steam's launcher, which exits within a second once it
# has passed the request on; the game is then a process nobody here owns. The
# session records that (`via_steam`) and is alive while a `cs2` process exists,
# with a grace period after the launch because the game takes a while to appear.
STEAM_STARTUP_GRACE = 120  # seconds

_WATCH_SESSION = {'proc': None, 'port': None, 'dem_path': None,
                  'via_steam': False, 'launched_at': 0.0}
_WATCH_LOCK = threading.Lock()


def _session_alive():
    proc = _WATCH_SESSION.get('proc')
    if proc is None:
        return False
    if proc.poll() is None:
        return True
    if not _WATCH_SESSION.get('via_steam'):
        return False
    if cs2_is_running():
        return True
    return time.monotonic() - _WATCH_SESSION.get('launched_at', 0.0) < STEAM_STARTUP_GRACE


def watch_session_port():
    """The netcon port of the CS2 this app launched, if it is still running -
    else None (nothing launched yet, or the user closed the game)."""
    return _WATCH_SESSION.get('port') if _session_alive() else None


def _reset_watch_session():
    _WATCH_SESSION.update(proc=None, port=None, dem_path=None,
                          via_steam=False, launched_at=0.0)


def netcon_switch_demo(port, dem_path, on_log=None):
    """Tell an already-running CS2 to load a different demo. Returns False if
    the console can't be reached; the caller then falls back to launching a
    fresh game."""
    sock = _netcon_connect(port, timeout=5)
    if not sock:
        if on_log:
            on_log(f'netcon: console on port {port} did not answer')
        return False
    try:
        sock.sendall(f'playdemo "{dem_path}"\n'.encode('utf-8'))
        return True
    except OSError:
        return False
    finally:
        # Same reason as in netcon_jump: closing on top of unread console
        # output would reset the connection and discard the playdemo, leaving
        # the previous demo loaded with nothing to say so.
        _netcon_close(sock)


# stderr + flush, not a bare print: the server's stdout is block-buffered when
# redirected to a log file, which silently swallowed the jump thread's progress
# the first time this was tested against a real game.
def _watch_log(msg):
    print(f'[demo watch] {msg}', file=sys.stderr, flush=True)


def _start_jump_thread(port, tick, focus_player):
    """Drive the jump on a daemon thread: CS2 needs tens of seconds to reach a
    demo and the HTTP request that started it must not wait for that. Daemon,
    because a stuck game must never keep the server alive."""
    threading.Thread(target=netcon_jump, args=(port, tick, focus_player),
                     kwargs={'on_log': _watch_log}, daemon=True).start()


def launch_demo_at(dem_path, tick, focus_player):
    """Put CS2 on `dem_path` at `tick`, from `focus_player`'s point of view,
    and return as soon as the work is handed to a background thread.

    Three cases, in order of preference - the point being that clicking a
    second moment should never mean closing and reopening the game:

      1. Our CS2 is up on this same demo -> just seek again (a second or two).
      2. Our CS2 is up on a different demo -> `playdemo` the new file over the
         console, then seek once it has loaded.
      3. Nothing of ours is running -> write the cfg and launch CS2.

    CS2 is left running either way - nothing here force-closes it, unlike
    record_clip(). Returns a short status message; raises ClipError with a
    user-facing message for every failure."""
    settings = load_settings()
    if not watch_ready(settings):
        raise ClipError(
            'Not configured - set the CS2 game directory in Settings first '
            '(CS2 Installation card).')
    if not os.path.isfile(dem_path):
        raise ClipError(f'Original demo not found: {dem_path}')

    landing = max(0, int(tick) - WATCH_PREROLL_TICKS)
    who = f" from {focus_player}'s POV" if focus_player else ''

    with _WATCH_LOCK:
        port = watch_session_port()
        if port:
            same_demo = os.path.normcase(_WATCH_SESSION.get('dem_path') or '') \
                == os.path.normcase(dem_path)
            if same_demo:
                _start_jump_thread(port, tick, focus_player)
                return (f'CS2 is already open on this demo - jumping to tick '
                        f'{landing}{who}. It lands paused; press space to play.')
            if netcon_switch_demo(port, dem_path, on_log=_watch_log):
                _WATCH_SESSION['dem_path'] = dem_path
                _start_jump_thread(port, tick, focus_player)
                return (f'CS2 is already open - loading this demo and jumping '
                        f'to tick {landing}{who}. It lands paused; press space '
                        'to play.')
            # The console stopped answering (game closing, or something ate
            # the port): fall through and start a fresh one.
            _reset_watch_session()

        if cs2_is_running():
            raise ClipError(
                'CS2 is already running, but it was not started from here so '
                'there is no console to drive - close it and click again, or '
                'load the demo yourself in game.')

        cfg_dir = cs2_cfg_dir(settings['cs2_game_dir'])
        os.makedirs(cfg_dir, exist_ok=True)
        cfg_path = write_watch_cfg(cfg_dir, dem_path, tick, focus_player)

        cs2_exe = cs2_exe_path(settings['cs2_game_dir'])
        # Source's `exec` takes the cfg name without its extension.
        cfg_name = os.path.splitext(os.path.basename(cfg_path))[0]
        port = pick_free_port()
        via_steam = IS_LINUX
        try:
            if via_steam:
                launch_args = cs2_steam_launch_args(
                    settings['cs2_game_dir'], cfg_name, netcon_port=port)
                # New session + no inherited pipes: the game must outlive this
                # server, and Steam is chatty on stdout.
                proc = subprocess.Popen(
                    launch_args, env=_child_env(), start_new_session=True,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL)
            else:
                launch_args = cs2_watch_launch_args(cs2_exe, cfg_name, netcon_port=port)
                proc = subprocess.Popen(launch_args, cwd=os.path.dirname(cs2_exe))
        except OSError as e:
            raise ClipError(f'Could not launch CS2: {e}')
        _WATCH_SESSION.update(proc=proc, port=port, dem_path=dem_path,
                              via_steam=via_steam, launched_at=time.monotonic())
        _start_jump_thread(port, tick, focus_player)

    return (f'CS2 is starting with the demo - it will jump to tick {landing}'
            f'{who} by itself once loaded, and land paused (press space to '
            f'play). If it does not, press {WATCH_JUMP_KEY} in game to jump '
            'manually. Later clicks reuse this same CS2 instead of restarting '
            'it. CS2 was launched with -insecure, so close and restart it '
            'normally before playing an official match.')


# ── Recording orchestration ───────────────────────────────────────────────────

def _kill_cs2(timeout=15):
    """Force-close CS2 via taskkill (no extra dependency like psutil needed
    for a Windows-only, single-purpose kill). Best-effort cleanup called
    from record_clip()'s `finally` block so the HLAE-injected process is
    never left running after a recording session - see the module
    docstring's VAC-risk note. Kills every running cs2.exe, since HLAE's
    custom loader hands off to a new process without giving us its PID;
    that's fine for this offline-review workflow.

    Returns True if taskkill reports it killed a process, False otherwise
    (nothing was running under that name, or taskkill itself is unavailable
    - e.g. in this dev container, which has no Windows tasklist/taskkill)."""
    try:
        result = subprocess.run(
            ['taskkill', '/IM', 'cs2.exe', '/F'],
            capture_output=True, text=True, timeout=timeout, **procutil.NO_WINDOW)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


# Image extensions HLAE's normal-stream sequence recorder is known to use.
# "afxClassic" (the default preset per the advancedfx wiki) writes TGA -
# confirmed against real output; the rest are listed in case a different
# preset is active.
_FRAME_EXTENSIONS = ('.tga', '.png', '.bmp', '.jpg', '.jpeg')


def find_recorded_frames(frames_dir):
    """Locate the image sequence HLAE actually wrote somewhere under
    frames_dir. Returns (frame_dir, [sorted filenames]) - or (None, []) if
    nothing was captured.

    HLAE does NOT write frames directly into the directory handed to
    `mirv_streams record name`. Verified against real output (2026-08-18),
    the layout is:

        <frames_dir>/take0000/audio.wav
        <frames_dir>/take0000/<stream_name>/00000.tga, 00001.tga, …

    i.e. a per-take directory, with the image sequence one level deeper in
    a per-stream subdirectory. This nesting is what made the original flat
    os.listdir(frames_dir) report "no frames recorded" for five straight
    rounds of debugging while recording was in fact working perfectly - the
    only entry it saw was the `take0000` *directory*, which has no image
    extension. Hence: walk, don't list.

    Multiple takes/streams shouldn't happen (record_clip() uses a fresh
    uuid-named frames_dir per clip), but if they do, the directory holding
    the most images wins rather than silently interleaving frames from
    different takes into one video."""
    if not os.path.isdir(frames_dir):
        return None, []
    by_dir = {}
    for root, _dirs, files in os.walk(frames_dir):
        names = sorted(n for n in files
                       if os.path.splitext(n)[1].lower() in _FRAME_EXTENSIONS)
        if names:
            by_dir[root] = names
    if not by_dir:
        return None, []
    frame_dir = max(by_dir, key=lambda d: len(by_dir[d]))
    return frame_dir, by_dir[frame_dir]


def find_recorded_audio(frames_dir):
    """The .wav HLAE writes alongside the image sequence (per-take, e.g.
    <frames_dir>/take0000/audio.wav - see find_recorded_frames()). Returned
    so record_clip() can mux real game audio into the .mp4 instead of
    producing a silent clip. None when the recording has no audio track."""
    if not os.path.isdir(frames_dir):
        return None
    for root, _dirs, files in os.walk(frames_dir):
        for n in sorted(files):
            if n.lower().endswith('.wav'):
                return os.path.join(root, n)
    return None


def ffmpeg_input_pattern(frame_names):
    """ffmpeg -i pattern for a discovered frame sequence, as
    (pattern, start_number). HLAE numbers frames as zero-padded integers
    (00000.tga, 00001.tga … - confirmed against real output), so this
    prefers a printf-style '%05d.tga' pattern with an explicit
    -start_number: unlike '-pattern_type glob' it's supported by every
    ffmpeg build (glob notably isn't guaranteed on Windows builds).

    Falls back to (None, None) when the names aren't purely numeric, which
    tells record_clip() to use a glob instead."""
    stems = [os.path.splitext(n)[0] for n in frame_names]
    if not stems or not all(s.isdigit() for s in stems):
        return None, None
    width = len(stems[0])
    if not all(len(s) == width for s in stems):
        return None, None
    ext = os.path.splitext(frame_names[0])[1]
    return f'%0{width}d{ext}', int(stems[0])


def wait_for_capture(frames_dir, max_seconds, on_tick=None,
                     stable_seconds=6.0, poll_seconds=2.0):
    """Block until HLAE's frame count under frames_dir stops growing, i.e.
    the scheduled `mirv_streams record end` (build_seek_cfg_lines()) has
    fired. Returns the final frame count (0 if nothing was ever captured).

    Replaces a fixed `time.sleep(clip_duration)`: with host_timescale 0 the
    demo renders as fast as the machine can manage rather than at
    real-time, so wall-clock time says nothing about how much of the clip
    has been captured - it can finish early on a fast machine or still be
    going on a slow one. Polling the actual output is the only reliable
    signal available (HLAE exposes no "recording finished" hook to us).

    max_seconds is a safety net for the case where recording never starts
    or never ends; stable_seconds guards against mistaking a brief I/O
    stall for the end of the recording."""
    deadline = time.monotonic() + max_seconds
    count = 0
    last_change = time.monotonic()
    while time.monotonic() < deadline:
        _dir, names = find_recorded_frames(frames_dir)
        if len(names) != count:
            count = len(names)
            last_change = time.monotonic()
            if on_tick:
                on_tick(count)
        elif count > 0 and time.monotonic() - last_change >= stable_seconds:
            return count
        time.sleep(poll_seconds)
    return count


def capture_timeout_seconds(start_tick, end_tick, tickrate=TICKRATE):
    """Upper bound for wait_for_capture() on one clip: the clip's own
    length plus generous slack for game startup and for a machine that
    renders slower than real-time (writing ~2.3 MB uncompressed TGA per
    frame can be disk-bound). Deliberately loose - it only exists so a
    recording that never ends can't hang the worker thread forever."""
    clip_seconds = max(0.0, (end_tick - start_tick) / tickrate)
    return min(1800.0, GAME_STARTUP_SECONDS + 60.0 + clip_seconds * 6.0)


def record_clip(match_id, dem_path, start_tick, end_tick, focus_player,
                 on_progress=None):
    """Blocking end-to-end recording - intended to run in a background
    thread (see app.py's /clip/record, same _JOBS pattern as _parse_worker).
    Returns {'clip_file': '<clip_id>.mp4', 'warning': <str>,
    'cs2_closed': <bool>} on success - 'cs2_closed' reflects whether the
    post-recording _kill_cs2() actually confirmed a kill, and 'warning' is
    picked accordingly (POST_RECORD_CLOSED_CONFIRMATION vs
    POST_RECORD_WARNING). Raises ClipError with a user-facing message on any
    failure - including "not configured"/"risk not acknowledged", which is
    the expected outcome everywhere except a real Windows machine with CS2 +
    HLAE + ffmpeg installed and the risk notice acknowledged in Settings.
    CS2 is force-closed on the way out of this function regardless of
    whether recording succeeded or ffmpeg failed (see the `finally` below)."""
    settings = load_settings()
    if not settings.get('vac_risk_ack'):
        raise ClipError(
            'Clip recording requires acknowledging the VAC-risk notice in '
            'Settings first (HLAE injects into the running CS2 process - '
            'see the Clip Recording card).')
    if not settings_ready(settings):
        raise ClipError(
            'Clip recording is not configured - set the CS2 game directory '
            '(CS2 Installation card) and the HLAE/ffmpeg paths in Settings '
            'first.')
    if not os.path.isfile(dem_path):
        raise ClipError(f'Original demo not found: {dem_path}')

    def progress(msg):
        if on_progress:
            on_progress(msg)

    # Derived from the clip's identity, not random - see clip_id_for(). A
    # re-record of the same highlight overwrites its own .mp4 instead of
    # leaving an orphan nobody can map back to a highlight.
    clip_id = clip_id_for(match_id, start_tick, end_tick, focus_player)
    clip_dir = os.path.join(paths.CLIPS_DIR, match_id)
    os.makedirs(clip_dir, exist_ok=True)
    frames_dir = os.path.join(clip_dir, f'{clip_id}_frames')
    # A previous attempt's frames would otherwise be picked up by
    # find_recorded_frames() and encoded instead of this run's footage.
    shutil.rmtree(frames_dir, ignore_errors=True)
    os.makedirs(frames_dir, exist_ok=True)

    # Source's `exec` only resolves relative to <game>/cfg/ (see
    # cs2_cfg_dir()'s docstring) - the .cfg has to be written there, not
    # into this app's own clip_dir, or CS2 finds nothing to exec.
    cfg_dir = cs2_cfg_dir(settings['cs2_game_dir'])
    os.makedirs(cfg_dir, exist_ok=True)
    cfg_path = os.path.join(cfg_dir, f'{clip_id}.cfg')
    seek_cfg_path = os.path.join(cfg_dir, f'{clip_id}_seek.cfg')

    write_cfgs(cfg_path, seek_cfg_path, dem_path, start_tick,
               end_tick, focus_player, clip_id, frames_dir,
               hud=settings.get('clip_hud', False),
               crosshair=settings.get('clip_crosshair', True))
    progress('Launching CS2 through HLAE…')

    cs2_exe = cs2_exe_path(settings['cs2_game_dir'])
    hook_dll = hlae_hook_dll_path(settings['hlae_path'])
    if not os.path.isfile(hook_dll):
        # settings_ready() already checks this, but re-check here too since
        # settings could theoretically change between the two calls.
        raise ClipError(
            f'HLAE hook DLL not found at {hook_dll} - expected '
            'AfxHookSource2.dll in the x64 subfolder next to HLAE.exe.')

    # hlae_launch_args() defaults to -insecure, so VAC never loads for this
    # session in the first place - layered on top of the offline-only .cfg
    # (assert_offline_only(), called by write_cfg() above). Launching
    # through HLAE's custom loader (rather than CS2 directly) is what
    # actually injects the hook DLL - see the module docstring's bug #2 note.
    launch_args = hlae_launch_args(settings['hlae_path'], cs2_exe, hook_dll,
                                    os.path.basename(cfg_path),
                                    width=settings.get('clip_width'),
                                    height=settings.get('clip_height'))
    try:
        # cwd matters: HLAE.exe (like many portable Windows GUI apps) loads
        # side-by-side DLLs relative to its own folder - without this, the
        # process inherits whatever directory the Flask server happens to be
        # running from and can fail to start correctly.
        hlae_proc = subprocess.Popen(launch_args, cwd=os.path.dirname(settings['hlae_path']))
    except OSError as e:
        raise ClipError(f'Could not launch HLAE: {e}')

    # HLAE's custom loader can fail fast (bad flags, broken install, missing
    # dependency) - catch that immediately instead of silently sleeping
    # through the whole clip and only reporting "no frames" at the very end.
    time.sleep(3)
    if hlae_proc.poll() is not None:
        raise ClipError(
            f'HLAE exited immediately (code {hlae_proc.returncode}) instead '
            'of staying running to launch CS2 - check the HLAE install is '
            'intact and that -customLoader/-noGui/-autoStart are supported '
            'by this HLAE version (see hlae_launch_args()).')

    # From here on CS2 (and the HLAE DLL it has loaded) is actually running,
    # so every exit path - success or ffmpeg failure - must still try to
    # force-close it. Hence `finally` rather than a call at the tail end.
    try:
        # Wait for the capture itself rather than for a fixed wall-clock
        # duration: the recording stops at its own scheduled end tick (see
        # build_seek_cfg_lines()), and with host_timescale 0 the demo can
        # render faster or slower than real-time, so only the frame count
        # on disk actually says when it's done.
        progress('Waiting for CS2 to load…')
        captured = wait_for_capture(
            frames_dir,
            max_seconds=capture_timeout_seconds(start_tick, end_tick),
            on_tick=lambda n: progress(f'Recording… ({n} frames)'))
        if captured:
            progress(f'Recording finished ({captured} frames).')

        # Walks frames_dir - HLAE nests the sequence under
        # take<NNNN>/<stream>/ rather than writing into frames_dir itself.
        # See find_recorded_frames()'s docstring.
        frame_dir, frame_files = find_recorded_frames(frames_dir)
        if not frame_files:
            raise ClipError(
                f'HLAE recorded no frames anywhere under {frames_dir} - '
                'recording never produced output. Check that CS2 actually '
                'launched with HLAE loaded and that its console shows '
                'mirv_streams recording (see build_seek_cfg_lines() for the '
                'commands sent, and mirv_cmd addAtTick in '
                'build_init_cfg_lines() for when they fire).')

        progress(f'Encoding clip ({len(frame_files)} frames)…')
        out_path = os.path.join(clip_dir, f'{clip_id}.mp4')

        pattern, start_number = ffmpeg_input_pattern(frame_files)
        if pattern:
            in_args = ['-start_number', str(start_number),
                       '-i', os.path.join(frame_dir, pattern)]
        else:
            ext = os.path.splitext(frame_files[0])[1]
            in_args = ['-pattern_type', 'glob',
                       '-i', os.path.join(frame_dir, f'*{ext}')]

        # HLAE writes the take's audio as a sibling .wav - mux it in so the
        # clip isn't silent.
        audio_path = find_recorded_audio(frames_dir)
        audio_in = ['-i', audio_path] if audio_path else []
        audio_out = ['-c:a', 'aac', '-shortest'] if audio_path else []

        result = subprocess.run(
            [settings['ffmpeg_path'], '-y', '-framerate', str(TICKRATE)]
            + in_args + audio_in
            + ['-c:v', 'libx264', '-pix_fmt', 'yuv420p'] + audio_out
            + [out_path],
            capture_output=True, text=True, timeout=600, **procutil.NO_WINDOW)
        if result.returncode != 0 or not os.path.isfile(out_path):
            # ffmpeg's stderr leads with a long version/build banner - the
            # actual error is at the end, so tail it rather than head it
            # (a fixed [:400] head previously only ever showed the banner).
            raise ClipError(
                f'ffmpeg encode failed ({len(frame_files)} frames captured '
                f'in {frame_dir}): {result.stderr.strip()[-1500:]}')
        clip_file = f'{clip_id}.mp4'

        # The raw sequence is uncompressed TGA - ~2.3 MB per frame, so a
        # few GB per clip against a ~13 MB .mp4. Drop it now that it's
        # encoded. Only on success: if the encode failed, the frames are
        # the one copy of the footage and the evidence for diagnosing why.
        progress('Cleaning up raw frames…')
        shutil.rmtree(frames_dir, ignore_errors=True)
    finally:
        progress('Closing CS2…')
        closed = _kill_cs2()
        # Best-effort: remove the .cfg files we dropped into the real CS2
        # install's cfg folder (cs2_cfg_dir()) - not load-bearing, so a
        # failure here (e.g. CS2 still has one open) is swallowed rather
        # than masking whatever the try block's own outcome was.
        for p in (cfg_path, seek_cfg_path):
            try:
                os.remove(p)
            except OSError:
                pass

    warning = POST_RECORD_CLOSED_CONFIRMATION if closed else POST_RECORD_WARNING
    return {'clip_file': clip_file, 'warning': warning, 'cs2_closed': closed}
