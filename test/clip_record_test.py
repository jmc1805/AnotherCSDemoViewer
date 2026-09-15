"""Unit tests for clip_record.py's pure/testable pieces (settings I/O, path
auto-detection, .cfg content, clip duration math, HLAE's launch argv). Does
NOT exercise record_clip() itself - that needs a real Steam/CS2/HLAE/ffmpeg
install and cannot run here (see the module docstring).

Run:  python3 test/clip_record_test.py     (no deps, no browser)
"""
import os
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import clip_record  # noqa: E402

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


def _make_cs2_install(tmpdir, name="CS2"):
    """A fake CS2 install: <tmpdir>/<name>/game/bin/win64/cs2.exe, matching
    the standard Steam layout cs2_exe_path() expects. Returns the game dir
    (what cs2_game_dir should be set to), not the exe path."""
    game_dir = os.path.join(tmpdir, name)
    exe_dir = os.path.join(game_dir, "game", "bin", "win64")
    os.makedirs(exe_dir, exist_ok=True)
    open(os.path.join(exe_dir, "cs2.exe"), "w").close()
    return game_dir


def _make_hlae_install(tmpdir, name="HLAE"):
    """A fake HLAE install: <tmpdir>/<name>/HLAE.exe plus
    <tmpdir>/<name>/x64/AfxHookSource2.dll, matching what
    hlae_hook_dll_path() expects next to HLAE.exe. Returns the HLAE.exe
    path (what hlae_path should be set to)."""
    hlae_dir = os.path.join(tmpdir, name)
    os.makedirs(os.path.join(hlae_dir, "x64"), exist_ok=True)
    hlae_exe = os.path.join(hlae_dir, "HLAE.exe")
    open(hlae_exe, "w").close()
    open(os.path.join(hlae_dir, "x64", "AfxHookSource2.dll"), "w").close()
    return hlae_exe


# ── clip duration / launch URL ────────────────────────────────────────────────

def test_capture_timeout_scales_with_clip_length():
    short = clip_record.capture_timeout_seconds(1000, 1000 + 64, tickrate=64)
    long_ = clip_record.capture_timeout_seconds(1000, 1000 + 64 * 60, tickrate=64)
    ok(long_ > short, "a longer tick range gets a longer safety timeout")
    ok(short > clip_record.GAME_STARTUP_SECONDS,
       "even a 1-second clip allows well beyond game-startup time before giving up")


def test_capture_timeout_is_capped():
    huge = clip_record.capture_timeout_seconds(0, 64 * 100000, tickrate=64)
    ok(huge <= 1800.0, "timeout is capped so a runaway recording can't hang the worker forever")


def test_capture_timeout_never_negative():
    d = clip_record.capture_timeout_seconds(2000, 1000, tickrate=64)
    ok(d > 0, "end before start still yields a positive timeout, not a negative one")


def test_hlae_hook_dll_path():
    # Built with os.path.join rather than a literal r"C:\HLAE\HLAE.exe": a
    # backslash is not a path separator on Linux, so os.path.dirname() returns
    # '' there and the hardcoded Windows path made this the one test in the
    # suite that could not pass on a non-Windows box (found by the Linux
    # acceptance run, tools/linuxtest/). HLAE itself is Windows-only, but the
    # test asserting where its DLL sits needn't be.
    base = os.path.join(os.sep + "HLAE")
    eq(clip_record.hlae_hook_dll_path(os.path.join(base, "HLAE.exe")),
       os.path.join(base, "x64", "AfxHookSource2.dll"),
       "hook DLL is found in the x64 subfolder next to HLAE.exe")


def test_hlae_launch_args():
    args = clip_record.hlae_launch_args(
        r"C:\HLAE\HLAE.exe", r"C:\CS2\game\bin\win64\cs2.exe",
        r"C:\HLAE\x64\AfxHookSource2.dll", "abc123.cfg")
    eq(args[0], r"C:\HLAE\HLAE.exe", "launches HLAE.exe itself, not cs2.exe directly")
    ok("-customLoader" in args, "uses HLAE's custom loader to inject the hook")
    ok("-hookDllPath" in args and r"C:\HLAE\x64\AfxHookSource2.dll" in args,
       "passes the hook DLL path")
    ok("-programPath" in args and r"C:\CS2\game\bin\win64\cs2.exe" in args,
       "passes cs2.exe as the program to launch and inject into")
    cmdline_idx = args.index("-cmdLine") + 1
    eq(args[cmdline_idx], "-steam -console -insecure +exec abc123.cfg",
       "cmdLine always passes -steam (a directly-launched Source game can "
       "refuse to run standalone without it) and -console (so a launch "
       "failure is visible, since HLAE spawns CS2 as its own child and we "
       "can't capture its output otherwise), plus -insecure by default "
       "(VAC never loads) and +exec'ing the generated cfg")


def test_hlae_launch_args_insecure_can_be_disabled():
    args = clip_record.hlae_launch_args(
        r"C:\HLAE\HLAE.exe", r"C:\CS2\cs2.exe", r"C:\HLAE\x64\AfxHookSource2.dll",
        "abc123.cfg", insecure=False)
    cmdline_idx = args.index("-cmdLine") + 1
    eq(args[cmdline_idx], "-steam -console +exec abc123.cfg",
       "insecure=False omits only the -insecure flag, keeping -steam/-console")


# ── .cfg generation ────────────────────────────────────────────────────────────

def test_build_init_cfg_lines():
    lines = clip_record.build_init_cfg_lines(r"C:\demos\a.dem", "clip1")
    joined = "\n".join(lines)
    ok('playdemo "C:\\demos\\a.dem"' in joined, "cfg loads the right demo path")
    # The HUD cvars used to live here and observably never applied: anything
    # after the async `playdemo` in the same cfg races the demo load (bug #4).
    # They belong in the deferred seek cfg - see HUD_OFF_LINES.
    ok("cl_drawhud" not in joined,
       "HUD cvars must NOT be in the init cfg - they'd race playdemo's async load")
    ok('mirv_cmd addAtTick 1 "exec clip1_seek.cfg"' in joined,
       "schedules the seek cfg via HLAE's own scheduler (mirv_cmd addAtTick) rather than "
       "the game's `wait` command, which is unavailable in CS:GO/CS2")
    ok("demo_gototick" not in joined and "mirv_streams" not in joined,
       "seek/record commands must NOT be in the init cfg - they'd race playdemo's async load")


def test_build_seek_cfg_lines():
    lines = clip_record.build_seek_cfg_lines(5000, 8000, "Alice", "clip1", r"C:\frames")
    joined = "\n".join(lines)
    ok("demo_gototick 5000 1" in joined, "cfg seeks to the clip's start tick")
    ok('spec_player "Alice"' in joined, "cfg spectates the focus player")
    ok("mirv_streams add normal clip1" in joined,
       "cfg registers a stream to record - without one, record start captures nothing")
    # Hiding the HUD happens HERE, not in the init cfg: a HUD rendered into a
    # capture is stale (a real clip froze its killfeed on one entry, making a
    # 4K read as a 1K), and set beside playdemo the cvar never applied at all.
    for line in clip_record.hud_cfg_lines():
        ok(line in joined, f"seek cfg sets the recorded overlay ({line})")
    ok(joined.index("cl_drawhud 0") < joined.index("mirv_streams record start"),
       "the HUD is hidden before capture starts, not after")
    ok('mirv_streams record name "C:\\frames"' in joined,
       "cfg points HLAE's output at frames_dir (an absolute path), not just a bare clip name")
    ok(f"mirv_streams record fps {clip_record.TICKRATE}" in joined,
       "cfg sets the capture rate to match what record_clip() encodes at")
    ok(f"host_framerate {clip_record.TICKRATE}" in joined,
       "puts the engine into frame-stepped capture mode")
    ok("host_timescale 0" in joined,
       "decouples capture from the real clock, paired with host_framerate")
    ok("mirv_streams record start" in joined, "cfg starts the recording")
    ok(joined.index("host_framerate") < joined.index("mirv_streams record start"),
       "host_framerate must be set before recording starts, not after")
    ok('mirv_cmd addAtTick 8000 "mirv_streams record end' in joined,
       "schedules the recording to STOP at the clip's end tick - without this the "
       "clip runs until CS2 is killed (a ~45s round produced a 186s clip)")
    ok(joined.index("record start") < joined.index("addAtTick 8000"),
       "the stop is scheduled after recording has started")


def test_spec_reassert_line():
    off = clip_record.SPEC_REASSERT_OFFSET_TICKS
    eq(clip_record.spec_reassert_line(5000, 8000, "Alice"),
       f'mirv_cmd addAtTick {5000 + off} "spec_player Alice"',
       "re-issues spec_player just AFTER the clip start - the seek jumps to start_tick, "
       "so a tick reached only by that jump may never fire")
    eq(clip_record.spec_reassert_line(5000, 8000, "La fleche"),
       f'mirv_cmd addAtTick {5000 + off} "spec_player La fleche"',
       "a name with spaces is fine unquoted inside mirv_cmd's own quotes")
    eq(clip_record.spec_reassert_line(5000, 8000, 'we"ird'), None,
       "a name containing a quote can't be nested inside mirv_cmd's quoted argument -> skipped")


def test_spec_reassert_line_skipped_for_tiny_clip():
    eq(clip_record.spec_reassert_line(5000, 5000 + 2, "Alice"), None,
       "a clip shorter than the re-assert offset would schedule the re-assert past its "
       "own stop - skipped rather than racing it")


def test_seek_cfg_reasserts_spectator_after_seek():
    off = clip_record.SPEC_REASSERT_OFFSET_TICKS
    lines = clip_record.build_seek_cfg_lines(5000, 8000, "Alice", "clip1", r"C:\frames")
    joined = "\n".join(lines)
    ok(f'mirv_cmd addAtTick {5000 + off} "spec_player Alice"' in joined,
       "re-asserts the target once playback reaches the clip - the pre-seek spec_player "
       "runs at tick 1 when player entities aren't populated, so it picks the wrong player")
    ok(joined.index('spec_player "Alice"') < joined.index(f"addAtTick {5000 + off}"),
       "the re-assert ADDS to the pre-seek call rather than replacing it")
    ok("spec_mode" not in joined, "still no spec_mode - that caused free-roam")


def test_seek_cfg_omits_reassert_for_unquotable_name():
    lines = clip_record.build_seek_cfg_lines(5000, 8000, 'we"ird', "clip1", r"C:\frames")
    joined = "\n".join(lines)
    ok('spec_player we' not in joined,
       "no re-assert for a name that can't be embedded - falls back to pre-seek only")
    ok("mirv_streams record start" in joined,
       "the rest of the cfg is unaffected by skipping the re-assert")


def test_seek_cfg_spectator_order_is_empirical():
    # Regression guard for module docstring bug #7. This ordering looks
    # wrong (the seek "should" reset the spectator target) but is the only
    # arrangement observed to actually produce the player's POV; moving
    # spec_player after the seek and adding spec_mode gave a permanently
    # free-roaming camera. Do not "fix" without testing a real recording.
    lines = clip_record.build_seek_cfg_lines(5000, 8000, "Alice", "clip1", r"C:\frames")
    joined = "\n".join(lines)
    ok(joined.index('spec_player "Alice"') < joined.index("demo_gototick"),
       "spec_player must come BEFORE demo_gototick - the reverse gave free-roam")
    ok("spec_mode" not in joined,
       "no spec_mode line: adding spec_mode 4 (a value taken from TF2 docs, not CS2) "
       "produced a free-roaming camera")


def test_write_cfgs_writes_both_files():
    tmpdir = tempfile.mkdtemp()
    try:
        cfg_path = os.path.join(tmpdir, "clip.cfg")
        seek_cfg_path = os.path.join(tmpdir, "clipX_seek.cfg")
        frames_dir = os.path.join(tmpdir, "clipX_frames")
        clip_record.write_cfgs(cfg_path, seek_cfg_path,
                                r"C:\demos\a.dem", 100, 500, "Bob", "clipX", frames_dir)
        ok(os.path.isfile(cfg_path), "write_cfgs creates the init cfg")
        ok(os.path.isfile(seek_cfg_path), "write_cfgs creates the seek cfg")
        with open(cfg_path, encoding="utf-8") as f:
            init_content = f.read()
        with open(seek_cfg_path, encoding="utf-8") as f:
            seek_content = f.read()
        ok("clipX_seek.cfg" in init_content, "init cfg schedules exactly the seek cfg's filename")
        ok(frames_dir in seek_content, "seek cfg points HLAE at frames_dir")
        ok("Bob" in seek_content, "seek cfg spectates the focus player")
    finally:
        shutil.rmtree(tmpdir)


# ── settings I/O ────────────────────────────────────────────────────────────

def _patch_settings_dirs(tmpdir):
    """clip_settings.json lives under paths.ANALYSIS_DATA_DIR; cs2_game_dir
    lives at the separate, shared paths.CS2_GAME_DIR_FILE (paths.py - used
    by tools/extract_ui_assets.* too). Both need redirecting into a tmpdir
    for an isolated test. Returns the (settings_path, analysis_dir,
    game_dir_file) originals to restore in a `finally`."""
    import paths
    orig_settings_path = clip_record.SETTINGS_PATH
    orig_analysis_dir = paths.ANALYSIS_DATA_DIR
    orig_game_dir_file = paths.CS2_GAME_DIR_FILE
    paths.ANALYSIS_DATA_DIR = tmpdir
    paths.CS2_GAME_DIR_FILE = os.path.join(tmpdir, "cs2_game_dir")
    clip_record.SETTINGS_PATH = os.path.join(tmpdir, "clip_settings.json")
    return orig_settings_path, orig_analysis_dir, orig_game_dir_file


def _restore_settings_dirs(originals):
    import paths
    clip_record.SETTINGS_PATH, paths.ANALYSIS_DATA_DIR, paths.CS2_GAME_DIR_FILE = originals


def test_settings_round_trip():
    tmpdir = tempfile.mkdtemp()
    originals = _patch_settings_dirs(tmpdir)
    try:
        import paths

        # Exact shape on purpose: this catches a key added to load_settings()
        # without a considered default. The capture options default to what
        # the recorder did before they were configurable - CS2's own
        # resolution, no HUD (a captured HUD is stale), crosshair on.
        eq(clip_record.load_settings(),
           {'cs2_game_dir': None, 'hlae_path': None, 'ffmpeg_path': None, 'vac_risk_ack': False,
            'clip_width': None, 'clip_height': None,
            'clip_hud': False, 'clip_crosshair': True},
           "missing settings file loads as all-None, risk not acknowledged")

        clip_record.save_settings({'cs2_game_dir': 'C:/CS2', 'hlae_path': None,
                                    'ffmpeg_path': 'C:/ffmpeg.exe', 'vac_risk_ack': True,
                                    'clip_width': 1921, 'clip_height': 1081,
                                    'clip_hud': True, 'clip_crosshair': False})
        loaded = clip_record.load_settings()
        eq(loaded['cs2_game_dir'], 'C:/CS2', "cs2_game_dir persisted (via the shared file)")
        eq(loaded['hlae_path'], None, "unset hlae_path stays None")
        eq(loaded['ffmpeg_path'], 'C:/ffmpeg.exe', "ffmpeg_path persisted")
        eq(loaded['vac_risk_ack'], True, "vac_risk_ack persisted")
        eq((loaded['clip_width'], loaded['clip_height']), (1920, 1080),
           "an odd resolution is normalised on the way to disk, not just on read")
        eq(loaded['clip_hud'], True, "clip_hud persisted")
        eq(loaded['clip_crosshair'], False, "clip_crosshair persisted")

        ok(os.path.isfile(paths.CS2_GAME_DIR_FILE),
           "cs2_game_dir is written to the shared app-wide file")
        with open(clip_record.SETTINGS_PATH, encoding='utf-8') as f:
            raw = f.read()
        ok('CS2' not in raw,
           "clip_settings.json itself never stores the CS2 path - it's app-wide, not clip-specific")
    finally:
        _restore_settings_dirs(originals)
        shutil.rmtree(tmpdir)


def test_load_settings_migrates_legacy_cs2_path():
    # Older clip_settings.json stored the full cs2.exe path per-tool under
    # 'cs2_path'. load_settings() should derive and persist the shared
    # cs2_game_dir from it once, rather than silently losing the setting.
    tmpdir = tempfile.mkdtemp()
    originals = _patch_settings_dirs(tmpdir)
    try:
        import json as _json
        import paths
        game_dir = _make_cs2_install(tmpdir)
        legacy_exe = clip_record.cs2_exe_path(game_dir)
        with open(clip_record.SETTINGS_PATH, "w", encoding="utf-8") as f:
            _json.dump({'cs2_path': legacy_exe, 'hlae_path': None,
                        'ffmpeg_path': None, 'vac_risk_ack': True}, f)

        loaded = clip_record.load_settings()
        eq(loaded['cs2_game_dir'], game_dir,
           "legacy per-tool cs2_path is migrated into the shared cs2_game_dir")
        ok(os.path.isfile(paths.CS2_GAME_DIR_FILE),
           "migration persists the shared file so it only needs to run once")
    finally:
        _restore_settings_dirs(originals)
        shutil.rmtree(tmpdir)


def test_settings_ready_requires_all_paths():
    tmpdir = tempfile.mkdtemp()
    try:
        game_dir = _make_cs2_install(tmpdir)
        hlae_exe = _make_hlae_install(tmpdir)
        real_file = os.path.join(tmpdir, "tool.exe")
        open(real_file, "w").close()
        ok(not clip_record.settings_ready({'cs2_game_dir': game_dir, 'hlae_path': None, 'ffmpeg_path': None,
                                            'vac_risk_ack': True}),
           "not ready when hlae/ffmpeg are missing")
        ok(not clip_record.settings_ready({'cs2_game_dir': game_dir, 'hlae_path': '/nope', 'ffmpeg_path': real_file,
                                            'vac_risk_ack': True}),
           "not ready when a configured path doesn't exist on disk")
        ok(not clip_record.settings_ready({'cs2_game_dir': tmpdir, 'hlae_path': hlae_exe, 'ffmpeg_path': real_file,
                                            'vac_risk_ack': True}),
           "not ready when cs2_game_dir doesn't actually contain a cs2.exe (wrong/empty directory)")
        ok(not clip_record.settings_ready({'cs2_game_dir': game_dir, 'hlae_path': real_file, 'ffmpeg_path': real_file,
                                            'vac_risk_ack': True}),
           "not ready when hlae_path exists but has no x64/AfxHookSource2.dll next to it "
           "(incomplete/wrong HLAE install)")
        ok(clip_record.settings_ready({'cs2_game_dir': game_dir, 'hlae_path': hlae_exe, 'ffmpeg_path': real_file,
                                        'vac_risk_ack': True}),
           "ready once the game dir has a real cs2.exe, HLAE has its hook DLL, ffmpeg points at a real "
           "file, and risk is acknowledged")
    finally:
        shutil.rmtree(tmpdir)


def test_settings_ready_requires_risk_ack_even_with_valid_paths():
    tmpdir = tempfile.mkdtemp()
    try:
        game_dir = _make_cs2_install(tmpdir)
        hlae_exe = _make_hlae_install(tmpdir)
        real_file = os.path.join(tmpdir, "tool.exe")
        open(real_file, "w").close()
        ok(not clip_record.settings_ready({'cs2_game_dir': game_dir, 'hlae_path': hlae_exe,
                                            'ffmpeg_path': real_file, 'vac_risk_ack': False}),
           "valid paths alone are not enough - risk ack is a separate, required gate")
        ok(not clip_record.settings_ready({'cs2_game_dir': game_dir, 'hlae_path': hlae_exe,
                                            'ffmpeg_path': real_file}),
           "missing vac_risk_ack key is treated as not acknowledged")
    finally:
        shutil.rmtree(tmpdir)


def test_cs2_cfg_dir():
    eq(clip_record.cs2_cfg_dir(r"C:\CS2"), os.path.join(r"C:\CS2", "game", "csgo", "cfg"),
       "cfg dir is <game_dir>/game/csgo/cfg - the only place Source's exec command looks")


# ── capture options: resolution, HUD, crosshair ──────────────────────────────

def test_normalize_resolution():
    eq(clip_record.normalize_resolution(1920, 1080), (1920, 1080),
       "an already-valid even size passes through")
    # -pix_fmt yuv420p has half-resolution chroma planes, so libx264 refuses an
    # odd width or height outright. Snapped down rather than rejected.
    eq(clip_record.normalize_resolution(1921, 1081), (1920, 1080),
       "odd dimensions are snapped down to even for the yuv420p encode")
    eq(clip_record.normalize_resolution(None, None), (None, None),
       "no size means 'leave CS2 at whatever it is set to'")
    eq(clip_record.normalize_resolution(1920, None), (None, None),
       "half a pair can't describe a frame - degrades to the game default")
    eq(clip_record.normalize_resolution('nonsense', 1080), (None, None),
       "junk degrades to the game default rather than raising")
    eq(clip_record.normalize_resolution(99999, 10),
       (clip_record.CLIP_MAX_DIM, clip_record.CLIP_MIN_DIM),
       "absurd values are clamped into range")
    eq(clip_record.normalize_resolution(0, 0), (None, None),
       "zero is not a resolution")


def test_hud_cfg_lines():
    off = clip_record.hud_cfg_lines()
    ok('cl_drawhud 0' in off, "the HUD is off by default - a captured HUD is stale")
    ok('crosshair 1' in off, "the crosshair is on by default")
    ok('cl_showfps 0' in off, "the fps counter is never burnt into a clip")
    ok('sv_cheats 1' == off[0],
       "sv_cheats leads: CS2 flags several client draw cvars as cheat-protected")

    on = clip_record.hud_cfg_lines(hud=True, crosshair=False)
    ok('cl_drawhud 1' in on, "the HUD can be turned on explicitly")
    ok('crosshair 0' in on, "the crosshair can be turned off")
    ok('cl_showfps 0' in on, "the fps counter stays off whatever the HUD does")
    # Independence of the two is NOT verified against CS2 (CS:GO hid the
    # crosshair along with the HUD); writing crosshair last at least gives it
    # the final word if they are in fact independent.
    ok(on.index('crosshair 0') > on.index('cl_drawhud 1'),
       "crosshair is written after cl_drawhud so it wins if they are independent")


def test_seek_cfg_carries_the_capture_options():
    lines = clip_record.build_seek_cfg_lines(5000, 8000, "Alice", "clip1", r"C:\frames",
                                             hud=True, crosshair=False)
    joined = "\n".join(lines)
    ok('cl_drawhud 1' in joined and 'crosshair 0' in joined,
       "the Settings toggles reach the cfg that is actually executed")
    ok(joined.index('cl_drawhud 1') < joined.index('mirv_streams record start'),
       "the overlay is set before capture starts, not after")


def test_hlae_launch_args_resolution():
    base = clip_record.hlae_launch_args("H.exe", "cs2.exe", "hook.dll", "a.cfg")
    cmdline = base[base.index('-cmdLine') + 1]
    ok('-w ' not in cmdline and '-windowed' not in cmdline,
       "no size configured leaves the launch exactly as it was before this existed")

    sized = clip_record.hlae_launch_args("H.exe", "cs2.exe", "hook.dll", "a.cfg",
                                         width=1920, height=1080)
    cmdline = sized[sized.index('-cmdLine') + 1]
    ok('-w 1920' in cmdline and '-h 1080' in cmdline,
       "HLAE captures what the game renders, so the size is a CS2 launch option")
    ok('-windowed' in cmdline,
       "windowed rides along - exclusive fullscreen ignores a requested size")
    ok('-insecure' in cmdline, "the VAC mitigation is not disturbed by a resolution")

    odd = clip_record.hlae_launch_args("H.exe", "cs2.exe", "hook.dll", "a.cfg",
                                       width=1921, height=1081)
    cmdline = odd[odd.index('-cmdLine') + 1]
    ok('-w 1920' in cmdline and '-h 1080' in cmdline,
       "the launch normalises too - an odd size never reaches the encoder")


def test_frame_bytes_per_second_is_the_uncompressed_cost():
    # HLAE's afxClassic preset writes uncompressed 24-bit TGA, one per tick.
    eq(clip_record.frame_bytes_per_second(1920, 1080),
       1920 * 1080 * 3 * clip_record.TICKRATE,
       "w*h*3 per frame at one frame per tick")
    eq(clip_record.frame_bytes_per_second(None, None), None,
       "no size, no estimate")


# ── clip length vs its tick window ───────────────────────────────────────────
#
# The clip id is a hash of the window, so a file at that id ASSERTS it is
# footage of that window. A truncated capture, or a file re-keyed onto a new
# window, breaks that silently - and the symptom is the recorded clip and the
# 2D preview disagreeing about how long the moment is, which is the one thing
# the paired boxes exist to avoid.

def _mp4_with_duration(path, seconds, timescale=64000):
    """Smallest thing ffprobe and mp4_duration_seconds both read as an mp4:
    an ftyp box plus a moov containing only an mvhd."""
    import struct
    units = int(round(seconds * timescale))
    mvhd = struct.pack('>I4sIIIII', 108, b'mvhd', 0, 0, 0, timescale, units)
    mvhd += bytes(1) * (108 - len(mvhd))
    moov = struct.pack('>I4s', 8 + len(mvhd), b'moov') + mvhd
    ftyp = struct.pack('>I4s4sI4s', 20, b'ftyp', b'isom', 512, b'isom')
    with open(path, 'wb') as fh:
        fh.write(ftyp + moov)


def test_mp4_duration_seconds_reads_the_mvhd_box():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        f = os.path.join(d, 'a.mp4')
        _mp4_with_duration(f, 14.5)
        got = clip_record.mp4_duration_seconds(f)
        ok(got is not None and abs(got - 14.5) < 0.01,
           f"reads duration from the mvhd box without ffprobe (got {got})")
        ok(clip_record.mp4_duration_seconds(os.path.join(d, 'nope.mp4')) is None,
           "a missing file is None, not an exception")
        junk = os.path.join(d, 'junk.mp4')
        with open(junk, 'wb') as fh:
            fh.write(b'not an mp4 at all')
        ok(clip_record.mp4_duration_seconds(junk) is None,
           "a file that isn't an mp4 is None, not an exception")


def test_clip_length_mismatch_flags_only_short_clips():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        # 929 ticks at 64Hz = 14.516s - the de_inferno round-15 4K window.
        start, end = 85609, 86538
        exact = os.path.join(d, 'exact.mp4')
        _mp4_with_duration(exact, (end - start) / clip_record.TICKRATE)
        eq(clip_record.clip_length_mismatch(exact, start, end), None,
           "a clip covering its whole window is not flagged")

        short = os.path.join(d, 'short.mp4')
        _mp4_with_duration(short, 14.02)          # the pre-change 86506 window
        got = clip_record.clip_length_mismatch(short, start, end)
        ok(got is not None, "footage recorded for an older, shorter window is flagged")
        ok(abs(got[0] - 14.02) < 0.01 and abs(got[1] - 14.516) < 0.01,
           f"reports (actual, expected) so the UI can name both numbers (got {got})")

        long_ = os.path.join(d, 'long.mp4')
        _mp4_with_duration(long_, 40.0)
        eq(clip_record.clip_length_mismatch(long_, start, end), None,
           "an overrunning clip is still whole footage of the moment - not flagged")

        edge = os.path.join(d, 'edge.mp4')
        _mp4_with_duration(edge, (end - start) / clip_record.TICKRATE - 0.05)
        eq(clip_record.clip_length_mismatch(edge, start, end), None,
           "a frame or two of muxing slack is within tolerance")


# ── offline-only .cfg guard (VAC-risk defense in depth) ───────────────────────

def test_cfg_lines_never_connect_online():
    init_lines = clip_record.build_init_cfg_lines(r"C:\demos\a.dem", "clip1")
    seek_lines = clip_record.build_seek_cfg_lines(5000, 8000, "Alice", "clip1", r"C:\frames")
    clip_record.assert_offline_only(init_lines + seek_lines)   # must not raise
    joined = " ".join(init_lines + seek_lines).lower()
    for token in clip_record._FORBIDDEN_TOKENS:
        ok(token not in joined, f"generated cfg lines never contain {token!r}")


def test_assert_offline_only_rejects_connect_command():
    try:
        clip_record.assert_offline_only(['playdemo "a.dem"', 'connect 1.2.3.4:27015'])
        ok(False, "assert_offline_only should have raised on a connect command")
    except clip_record.ClipError:
        ok(True, "assert_offline_only raises ClipError on a connect command")


def test_write_cfgs_rejects_dangerous_lines():
    # Poison the LAST stage specifically: the guard has to validate every
    # file before writing any of them, or a rejected set leaves half-written
    # cfgs behind in the real CS2 install.
    tmpdir = tempfile.mkdtemp()
    try:
        import clip_record as cr
        orig = cr.build_seek_cfg_lines
        cr.build_seek_cfg_lines = lambda *a, **k: ['connect 1.2.3.4:27015']
        try:
            cr.write_cfgs(os.path.join(tmpdir, "clip.cfg"),
                           os.path.join(tmpdir, "clip_seek.cfg"),
                           r"C:\a.dem", 100, 500, "Bob", "clipX", r"C:\frames")
            ok(False, "write_cfgs should refuse to write a cfg with a connect command")
        except cr.ClipError:
            ok(True, "write_cfgs refuses to write a cfg with a connect command")
        for name in ("clip.cfg", "clip_seek.cfg"):
            ok(not os.path.isfile(os.path.join(tmpdir, name)),
               f"rejected {name} is never written to disk")
        cr.build_seek_cfg_lines = orig
    finally:
        shutil.rmtree(tmpdir)


# ── recorded-frame discovery ───────────────────────────────────────────────────

def _make_hlae_take(frames_dir, stream="clipX", n=3, ext=".tga", audio=True):
    """Reproduces HLAE's real on-disk output layout, verified against actual
    recorded output (2026-08-18):
        <frames_dir>/take0000/audio.wav
        <frames_dir>/take0000/<stream>/00000.tga, 00001.tga, ...
    The nesting is the whole point - a flat listdir of frames_dir sees only
    the take0000 directory, which is what made record_clip() wrongly report
    "recorded no frames" while capture was working fine."""
    take = os.path.join(frames_dir, "take0000")
    seq = os.path.join(take, stream)
    os.makedirs(seq, exist_ok=True)
    for i in range(n):
        open(os.path.join(seq, f"{i:05d}{ext}"), "w").close()
    if audio:
        open(os.path.join(take, "audio.wav"), "w").close()
    return seq


def test_find_recorded_frames_missing_dir():
    eq(clip_record.find_recorded_frames("/nonexistent/frames/dir"), (None, []),
       "a frames_dir that was never created (HLAE wrote nothing) -> (None, []), not an error")


def test_find_recorded_frames_empty_dir():
    tmpdir = tempfile.mkdtemp()
    try:
        eq(clip_record.find_recorded_frames(tmpdir), (None, []),
           "an empty frames_dir (recording produced nothing) -> (None, [])")
    finally:
        shutil.rmtree(tmpdir)


def test_find_recorded_frames_walks_hlae_nested_layout():
    tmpdir = tempfile.mkdtemp()
    try:
        seq = _make_hlae_take(tmpdir, stream="clipX", n=3)
        frame_dir, names = clip_record.find_recorded_frames(tmpdir)
        eq(frame_dir, seq,
           "returns the nested take<NNNN>/<stream>/ dir that actually holds the sequence, "
           "not the frames_dir handed to mirv_streams record name")
        eq(names, ["00000.tga", "00001.tga", "00002.tga"],
           "returns the frame filenames sorted in capture order")
    finally:
        shutil.rmtree(tmpdir)


def test_find_recorded_frames_ignores_non_images():
    tmpdir = tempfile.mkdtemp()
    try:
        _make_hlae_take(tmpdir, n=2)
        open(os.path.join(tmpdir, "take0000", "notes.txt"), "w").close()
        _frame_dir, names = clip_record.find_recorded_frames(tmpdir)
        eq(names, ["00000.tga", "00001.tga"],
           "audio.wav / stray files are not mistaken for frames")
    finally:
        shutil.rmtree(tmpdir)


def test_find_recorded_frames_picks_largest_take():
    tmpdir = tempfile.mkdtemp()
    try:
        # Shouldn't happen (fresh uuid dir per clip) but must not interleave
        # frames from two takes into one video if it ever does.
        big = os.path.join(tmpdir, "take0000", "streamA")
        small = os.path.join(tmpdir, "take0001", "streamB")
        os.makedirs(big); os.makedirs(small)
        for i in range(5):
            open(os.path.join(big, f"{i:05d}.tga"), "w").close()
        open(os.path.join(small, "00000.tga"), "w").close()
        frame_dir, names = clip_record.find_recorded_frames(tmpdir)
        eq(frame_dir, big, "the directory with the most frames wins")
        eq(len(names), 5, "and only that directory's frames are returned")
    finally:
        shutil.rmtree(tmpdir)


def test_wait_for_capture_returns_when_frames_stop_growing():
    tmpdir = tempfile.mkdtemp()
    try:
        _make_hlae_take(tmpdir, n=4)
        # Already stable on disk, so this should settle after stable_seconds
        # rather than burning the whole timeout.
        t0 = time.monotonic()
        n = clip_record.wait_for_capture(tmpdir, max_seconds=20,
                                          stable_seconds=0.6, poll_seconds=0.1)
        elapsed = time.monotonic() - t0
        eq(n, 4, "returns the final frame count once the count stops changing")
        ok(elapsed < 10, f"returns promptly on a settled recording (took {elapsed:.1f}s)")
    finally:
        shutil.rmtree(tmpdir)


def test_wait_for_capture_keeps_waiting_while_frames_arrive():
    tmpdir = tempfile.mkdtemp()
    try:
        seq = _make_hlae_take(tmpdir, n=1)

        def add_frames():
            for i in range(1, 5):
                time.sleep(0.25)
                open(os.path.join(seq, f"{i:05d}.tga"), "w").close()

        t = threading.Thread(target=add_frames)
        t.start()
        n = clip_record.wait_for_capture(tmpdir, max_seconds=20,
                                          stable_seconds=0.8, poll_seconds=0.1)
        t.join()
        eq(n, 5, "doesn't stop early while new frames are still being written")
    finally:
        shutil.rmtree(tmpdir)


def test_wait_for_capture_times_out_when_nothing_recorded():
    tmpdir = tempfile.mkdtemp()
    try:
        t0 = time.monotonic()
        n = clip_record.wait_for_capture(tmpdir, max_seconds=1.0,
                                          stable_seconds=0.3, poll_seconds=0.1)
        elapsed = time.monotonic() - t0
        eq(n, 0, "returns 0 when recording never produced anything")
        ok(elapsed >= 1.0, "waits out the full timeout before giving up")
        ok(elapsed < 10, "but does give up rather than blocking forever")
    finally:
        shutil.rmtree(tmpdir)


def test_find_recorded_audio():
    tmpdir = tempfile.mkdtemp()
    try:
        _make_hlae_take(tmpdir, n=1)
        found = clip_record.find_recorded_audio(tmpdir)
        eq(found, os.path.join(tmpdir, "take0000", "audio.wav"),
           "finds the per-take audio.wav so the encoded clip isn't silent")
    finally:
        shutil.rmtree(tmpdir)


def test_find_recorded_audio_absent():
    tmpdir = tempfile.mkdtemp()
    try:
        _make_hlae_take(tmpdir, n=1, audio=False)
        eq(clip_record.find_recorded_audio(tmpdir), None,
           "no .wav -> None (record_clip() then encodes video only)")
    finally:
        shutil.rmtree(tmpdir)


def test_ffmpeg_input_pattern_zero_padded():
    eq(clip_record.ffmpeg_input_pattern(["00000.tga", "00001.tga", "00002.tga"]),
       ("%05d.tga", 0),
       "HLAE's zero-padded numbering maps to a printf pattern + start number, which "
       "every ffmpeg build supports (unlike -pattern_type glob on Windows)")


def test_ffmpeg_input_pattern_nonzero_start():
    eq(clip_record.ffmpeg_input_pattern(["00007.tga", "00008.tga"]), ("%05d.tga", 7),
       "start_number comes from the first frame, not assumed to be 0")


def test_ffmpeg_input_pattern_non_numeric_falls_back():
    eq(clip_record.ffmpeg_input_pattern(["frame_a.tga", "frame_b.tga"]), (None, None),
       "non-numeric names -> no pattern, so record_clip() falls back to a glob")


def test_ffmpeg_input_pattern_mixed_width_falls_back():
    eq(clip_record.ffmpeg_input_pattern(["0001.tga", "00002.tga"]), (None, None),
       "inconsistent zero-padding can't be expressed as one printf pattern -> fall back")


# ── post-recording cleanup ──────────────────────────────────────────────────────

def test_kill_cs2_degrades_gracefully_when_not_running():
    # cs2.exe isn't running in the test environment (and taskkill itself
    # may not even exist, e.g. on a non-Windows CI runner) -- either way
    # _kill_cs2() must not raise, just report it couldn't confirm a kill.
    ok(clip_record._kill_cs2(timeout=5) is False,
       "_kill_cs2 returns False (not an exception) when there's nothing to kill")


# ── path auto-detection ────────────────────────────────────────────────────────

def test_autodetect_paths_with_injected_candidates():
    tmpdir = tempfile.mkdtemp()
    try:
        good_dir = _make_cs2_install(tmpdir)
        missing_dir = os.path.join(tmpdir, "missing_install")
        found = clip_record.autodetect_paths(search_roots={
            'cs2': [missing_dir, good_dir],
            'hlae': [os.path.join(tmpdir, "HLAE.exe")],
            'ffmpeg': [],
        })
        eq(found['cs2_game_dir'], good_dir,
           "picks the first candidate game dir that actually contains cs2.exe, skipping ones that don't")
        eq(found['hlae_path'], None, "no existing candidate -> None")
        eq(found['ffmpeg_path'], None, "empty candidate list -> None")
    finally:
        shutil.rmtree(tmpdir)


# ── watch mode (jump into the demo, no HLAE) ──────────────────────────────────

def test_watch_cfg_arms_the_key_before_loading_the_demo():
    lines = clip_record.build_watch_cfg_lines(r'C:\demos\x.dem', 20000, 'Alice')
    bind = next(i for i, ln in enumerate(lines) if ln.startswith('bind '))
    play = next(i for i, ln in enumerate(lines) if ln.startswith('playdemo '))
    ok(bind < play, 'the jump key is bound before playdemo (which is async)')
    ok('x.dem' in lines[play], 'the demo path is the one passed in')
    ok(any(ln.startswith('echo ') for ln in lines), 'the console says what to press')


def test_watch_cfg_never_touches_hlae():
    # The whole point of watch mode: nothing is injected into the game.
    lines = clip_record.build_watch_cfg_lines(r'C:\d.dem', 500, 'Alice')
    joined = ' '.join(lines).lower()
    for token in ('mirv', 'hlae', 'host_timescale', 'host_framerate'):
        ok(token not in joined, f'no {token} in a watch cfg')


def test_watch_launch_args_are_a_plain_cs2_launch():
    args = clip_record.cs2_watch_launch_args(r'C:\cs2.exe', 'cs2viewer_watch')
    eq(args[0], r'C:\cs2.exe', 'CS2 itself is the process launched, not HLAE')
    ok('-insecure' in args, 'VAC still never loads for the session')
    ok('-console' in args, 'console is open so the echo hint is visible')
    ok('+exec' in args and 'cs2viewer_watch' in args, 'the watch cfg is exec-ed')
    ok(not any('hook' in a.lower() or 'hlae' in a.lower() for a in args),
       'no hook DLL, no custom loader')
    eq('-insecure' in clip_record.cs2_watch_launch_args(r'C:\cs2.exe', 'c', insecure=False),
       False, 'insecure=False drops the flag')


def test_watch_jump_command_specs_before_seeking_and_pauses():
    cmd = clip_record.watch_jump_command(20000, 'Alice')
    ok(cmd.index('spec_player') < cmd.index('demo_gototick'),
       'spec_player comes before demo_gototick (see build_seek_cfg_lines)')
    ok('spec_mode' not in cmd, 'no spec_mode, same as the recording cfg')
    ok(cmd.endswith('demo_pause'), 'lands paused')


def test_watch_jump_lands_a_preroll_before_the_moment():
    tick = 20000
    landing = tick - clip_record.WATCH_PREROLL_TICKS
    ok(f'demo_gototick {landing}' in clip_record.watch_jump_command(tick, 'Alice'),
       'lands a preroll before the moment')
    ok('demo_gototick 0' in clip_record.watch_jump_command(10, 'Alice'),
       'a tick shorter than the preroll clamps to 0')


def test_watch_jump_without_a_player_just_seeks():
    cmd = clip_record.watch_jump_command(20000, '')
    ok('spec_player' not in cmd, 'no name -> no spec_player, CS2 picks the camera')
    ok('demo_gototick' in cmd, 'the seek still happens')


def test_watch_jump_skips_a_name_that_would_break_the_bind():
    # The name sits unquoted inside the bind's own quoted string: a quote
    # would end that string and a semicolon would inject a second command.
    for bad in ('He"llo', 'a;quit'):
        cmd = clip_record.watch_jump_command(20000, bad)
        ok('spec_player' not in cmd, f'{bad!r} is not embedded in the bind')
        ok('demo_gototick' in cmd, f'{bad!r} still gets the seek')


def test_watch_cfg_passes_the_offline_guard():
    try:
        clip_record.assert_offline_only(['connect 1.2.3.4'])
    except clip_record.ClipError:
        ok(True, 'a connecting command is refused')
    else:
        ok(False, 'a connecting command must raise ClipError')
    clip_record.assert_offline_only(
        clip_record.build_watch_cfg_lines(r'C:\d.dem', 500, 'Alice'))
    ok(True, 'the real watch cfg passes the offline guard')


def test_write_watch_cfg_writes_one_file():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        path = clip_record.write_watch_cfg(d, r'C:\d.dem', 5000, 'Alice')
        ok(os.path.isfile(path), 'the cfg is written')
        eq(os.path.basename(path), clip_record.WATCH_CFG_NAME + '.cfg',
           'one fixed name per install')
        with open(path, encoding='utf-8') as f:
            body = f.read()
        ok('spec_player Alice' in body, 'the focus player made it into the file')
        ok(clip_record.WATCH_JUMP_KEY in body, 'the jump key is in the bind')


def test_watch_ready_needs_only_cs2():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        game_dir = os.path.join(d, 'game_dir')
        os.makedirs(os.path.dirname(clip_record.cs2_exe_path(game_dir)), exist_ok=True)
        open(clip_record.cs2_exe_path(game_dir), 'w').close()
        s = {'cs2_game_dir': game_dir, 'hlae_path': '', 'ffmpeg_path': '',
             'vac_risk_ack': False}
        ok(clip_record.watch_ready(s),
           'watching needs no HLAE, no ffmpeg and no VAC-risk acknowledgement')
        ok(not clip_record.settings_ready(s), 'recording still needs all of it')
        ok(not clip_record.watch_ready({'cs2_game_dir': ''}),
           'but it does need a real CS2 install')

# ── netcon (auto-jump without HLAE) ───────────────────────────────────────────
#
# Driven against a fake CS2 console: a loopback TCP server that answers
# `demo_info` and `demo_gototick` with the same strings a real CS2 printed
# during the session this was built in (see the netcon section header).

class _FakeCS2Console:
    """Minimal stand-in for CS2's -netconport listener.

    `ready_after` delays the demo-loaded answer by N `demo_info` probes, so a
    test can exercise the "still loading" path; `confirm_seek` toggles the
    console line netcon_jump() looks for."""

    def __init__(self, ready_after=0, confirm_seek=True, at_tick=None):
        # ready_after: how many seeks are silently ignored first, mimicking a
        # demo that is still loading (a real CS2 prints nothing at all then).
        import socket as _socket
        import threading as _threading
        self.ready_after = ready_after
        self.confirm_seek = confirm_seek
        self.at_tick = at_tick          # where playback already sits, if anywhere
        self.received = []
        self._probes = 0
        self._sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        self._sock.bind(('127.0.0.1', 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self._thread = _threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(60)
            buf = b''
            while True:
                try:
                    data = conn.recv(4096)
                except (OSError, TimeoutError):
                    return
                if not data:
                    return
                buf += data
                while b'\n' in buf:
                    line, buf = buf.split(b'\n', 1)
                    cmd = line.decode('utf-8', 'replace').strip()
                    if not cmd:
                        continue
                    self.received.append(cmd)
                    try:
                        conn.sendall(self._reply(cmd))
                    except OSError:
                        return

    def _reply(self, cmd):
        if cmd.startswith('demo_gototick'):
            self._probes += 1
            # Still loading, or a game that never confirms: a real CS2 prints
            # nothing at all for a seek it ignores.
            if self._probes <= self.ready_after or not self.confirm_seek:
                return b''
            tick = cmd.split()[-1]
            # ...and nothing for a seek to the tick it is ALREADY on.
            if self.at_tick is not None and int(tick) == self.at_tick:
                return b''
            self.at_tick = int(tick)
            return (f'Demo Skipping: skipping to demo tick {tick}\r\n'
                    f'Demo Skipping finished at tick {tick}\r\n').encode()
        return b''                    # quiet commands print nothing

    def wait_for(self, predicate, timeout=10.0):
        """Block until predicate(self.received) holds, or the timeout expires.

        netcon_jump() writes its trailing commands and returns without waiting
        for them to be read, so the reader thread is still draining the socket
        when a test starts asserting. Polling for the expected state keeps that
        race out of the assertions: a fixed sleep either fails under load or
        pads every run with the slowest case's delay.

        Returns whether the predicate held, so a caller can assert on it and
        get its own message rather than this helper's.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate(self.received):
                return True
            time.sleep(0.02)
        return predicate(self.received)

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass


def test_pick_free_port_returns_a_usable_port():
    port = clip_record.pick_free_port()
    ok(1024 < port < 65536, 'a high, non-privileged port')
    ok(port != clip_record.pick_free_port() or True, 'callable repeatedly')


def test_netcon_launch_arg_is_opt_in():
    with_port = clip_record.cs2_watch_launch_args(r'C:\cs2.exe', 'w', netcon_port=51423)
    ok('-netconport' in with_port and '51423' in with_port, 'the port is passed to CS2')
    without = clip_record.cs2_watch_launch_args(r'C:\cs2.exe', 'w')
    ok('-netconport' not in without, 'omitted when no port is given (F8-only launch)')


def test_netcon_jump_drives_the_moment():
    fake = _FakeCS2Console()
    try:
        ok(clip_record.netcon_jump(fake.port, 20000, 'Alice'),
           'reports success when the game confirms the seek')
        # The trailing commands (the re-asserted POV, the pause) are written
        # without waiting for a reply, so wait for the fake to have read them.
        fake.wait_for(lambda got: got.count('spec_player Alice') >= 2
                      and 'demo_pause' in got)
    finally:
        fake.close()
    landing = 20000 - clip_record.WATCH_PREROLL_TICKS
    spec = fake.received.index('spec_player Alice')
    seek = fake.received.index(f'demo_gototick {landing}')
    ok(spec < seek, 'spec_player is sent before demo_gototick')
    ok('demo_pause' in fake.received, 'lands paused')
    ok(fake.received.count('spec_player Alice') >= 2,
       'the POV is re-asserted after the seek lands')


def test_netcon_close_does_not_discard_the_last_command():
    """The socket must be closed gracefully, or the final command is lost.

    netcon_jump writes `spec_player` and closes immediately afterwards. CS2
    streams console output that it never fully reads, and closing a socket
    with unread data in the receive buffer makes the OS send an RST rather
    than a FIN - which discards what was just written. The POV re-assert then
    silently does not happen, and the demo plays from the wrong player's view.

    One iteration would pass even with the bug present (it dropped roughly one
    run in five), so this repeats: a regression shows up as a handful of
    failures, not a coin flip.
    """
    runs, lost = 12, 0
    for _ in range(runs):
        fake = _FakeCS2Console()
        try:
            clip_record.netcon_jump(fake.port, 20000, 'Alice')
            if not fake.wait_for(lambda got: got.count('spec_player Alice') >= 2, 3.0):
                lost += 1
        finally:
            fake.close()
    eq(lost, 0, f'the last command survived the close in all {runs} runs')


def test_netcon_jump_retries_while_the_demo_is_still_loading():
    # A seek sent before the demo is playing is silently ignored - the whole
    # reason this retries instead of probing readiness first.
    fake = _FakeCS2Console(ready_after=2)
    real_gap = clip_record.NETCON_RETRY_SECONDS
    clip_record.NETCON_RETRY_SECONDS = 0.2      # keep the test quick
    try:
        ok(clip_record.netcon_jump(fake.port, 20000, 'Alice'),
           'succeeds once the game finally accepts the seek')
    finally:
        clip_record.NETCON_RETRY_SECONDS = real_gap
        fake.close()
    landing = 20000 - clip_record.WATCH_PREROLL_TICKS
    seeks = [int(c.split()[-1]) for c in fake.received if c.startswith('demo_gototick')]
    ok(len(seeks) >= 3, 'kept asking until the game confirmed (2 ignored, 3rd landed)')
    ok(set(seeks) <= {landing, landing + 1},
       'attempts only ever target the landing tick or the one next to it')
    eq(seeks[-1], landing, 'and the demo ends up on the landing tick')


def test_netcon_jump_survives_a_demo_already_parked_on_the_target():
    # The failure this alternation exists for: an earlier jump left playback
    # exactly on the landing tick, so a plain re-seek there prints nothing and
    # looks identical to "not loaded yet".
    landing = 20000 - clip_record.WATCH_PREROLL_TICKS
    fake = _FakeCS2Console(at_tick=landing)
    real_gap = clip_record.NETCON_RETRY_SECONDS
    clip_record.NETCON_RETRY_SECONDS = 0.2
    try:
        ok(clip_record.netcon_jump(fake.port, 20000, 'Alice'),
           'still confirms, by nudging one tick past the mark')
    finally:
        clip_record.NETCON_RETRY_SECONDS = real_gap
        fake.close()
    seeks = [int(c.split()[-1]) for c in fake.received if c.startswith('demo_gototick')]
    ok(landing + 1 in seeks, 'tried the neighbouring tick, which is never silent')
    eq(seeks[-1], landing, 'and stepped back onto the landing tick at the end')


def test_netcon_jump_reports_failure_when_the_seek_is_never_confirmed():
    fake = _FakeCS2Console(confirm_seek=False)
    real_timeout, real_gap = clip_record.NETCON_READY_TIMEOUT, clip_record.NETCON_RETRY_SECONDS
    clip_record.NETCON_READY_TIMEOUT, clip_record.NETCON_RETRY_SECONDS = 6, 0.2
    try:
        ok(not clip_record.netcon_jump(fake.port, 20000, 'Alice'),
           'silence is never treated as success (an ignored command prints nothing)')
    finally:
        clip_record.NETCON_READY_TIMEOUT = real_timeout
        clip_record.NETCON_RETRY_SECONDS = real_gap
        fake.close()


def test_netcon_jump_gives_up_when_nothing_is_listening():
    port = clip_record.pick_free_port()      # free == nothing listening on it
    logged = []
    t0 = time.time()
    ok(not clip_record.netcon_jump(port, 20000, 'Alice',
                                   on_log=logged.append,
                                   ),
       'returns False rather than raising when CS2 never opens the port')
    ok(time.time() - t0 < clip_record.NETCON_CONNECT_TIMEOUT + 10,
       'bounded by NETCON_CONNECT_TIMEOUT')
    ok(any('F8' in m or 'bind' in m for m in logged),
       'says the F8 bind is the fallback')


def test_netcon_jump_without_a_player_still_seeks():
    fake = _FakeCS2Console()
    try:
        ok(clip_record.netcon_jump(fake.port, 20000, ''), 'seek succeeds')
    finally:
        fake.close()
    ok(not any(c.startswith('spec_player') for c in fake.received),
       'no name -> no spec_player, CS2 picks the camera')


# ── watch session reuse (jumping between moments in one CS2) ──────────────────

class _FakeProc:
    """Stand-in for the Popen handle of a launched CS2."""

    def __init__(self, alive=True):
        self._alive = alive

    def poll(self):
        return None if self._alive else 0

    def exit(self):
        self._alive = False


def _with_session(proc, port, dem_path):
    clip_record._WATCH_SESSION.update(proc=proc, port=port, dem_path=dem_path)


def _stub_watch_ready():
    """Pretend a CS2 install is configured, and hand back the undo.

    launch_demo_at() refuses before it does anything if watch_ready() says no,
    so without this the tests below only exercise the session logic on a
    machine that happens to have CS2 set up in Settings, and fail everywhere
    else. What they are about is which of the three launch paths gets taken,
    not the readiness check - test_watch_ready_needs_only_cs2 covers that.
    """
    real = clip_record.watch_ready
    clip_record.watch_ready = lambda settings=None: True
    return lambda: setattr(clip_record, 'watch_ready', real)


def test_watch_session_port_tracks_the_launched_game():
    try:
        _with_session(None, None, None)
        eq(clip_record.watch_session_port(), None, 'nothing launched yet -> no port')
        proc = _FakeProc()
        _with_session(proc, 51000, r'C:\d.dem')
        eq(clip_record.watch_session_port(), 51000, 'a live game reports its port')
        proc.exit()
        eq(clip_record.watch_session_port(), None,
           'once CS2 exits the port is disowned - it goes back to the OS pool '
           'and could belong to something else entirely')
    finally:
        _with_session(None, None, None)


def test_launch_demo_at_reuses_a_running_cs2_for_the_same_demo():
    # The demo path must exist on disk, so this file stands in for one.
    fake = _FakeCS2Console()
    jumped = []
    real_thread = clip_record._start_jump_thread
    clip_record._start_jump_thread = lambda port, tick, focus: jumped.append((port, tick, focus))
    undo_ready = _stub_watch_ready()
    try:
        _with_session(_FakeProc(), fake.port, __file__)
        msg = clip_record.launch_demo_at(__file__, 20000, 'Alice')
        ok('already open on this demo' in msg, 'says it reused the running game')
        eq(len(jumped), 1, 'the jump was driven without launching anything')
        eq(jumped[0][0], fake.port, 'over the existing session port')
        ok(not any(c.startswith('playdemo') for c in fake.received),
           'the same demo is not reloaded')
    finally:
        undo_ready()
        clip_record._start_jump_thread = real_thread
        _with_session(None, None, None)
        fake.close()


def test_launch_demo_at_switches_demo_in_a_running_cs2():
    fake = _FakeCS2Console()
    jumped = []
    real_thread = clip_record._start_jump_thread
    clip_record._start_jump_thread = lambda port, tick, focus: jumped.append((port, tick, focus))
    undo_ready = _stub_watch_ready()
    try:
        _with_session(_FakeProc(), fake.port, r'C:\some_other.dem')
        msg = clip_record.launch_demo_at(__file__, 20000, 'Alice')
        ok('loading this demo' in msg, 'says it is switching demos in place')
        # launch_demo_at() hands the console write off and returns, so wait for
        # the fake to have actually read it rather than guessing at a delay.
        fake.wait_for(lambda got: any(c.startswith('playdemo') for c in got))
        ok(any(c.startswith('playdemo') for c in fake.received),
           'the new demo is loaded over the console, not by relaunching CS2')
        eq(len(jumped), 1, 'and the jump follows')
        eq(clip_record._WATCH_SESSION['dem_path'], __file__,
           'the session remembers which demo is loaded now')
    finally:
        undo_ready()
        clip_record._start_jump_thread = real_thread
        _with_session(None, None, None)
        fake.close()


def test_launch_demo_at_refuses_a_cs2_it_did_not_start():
    # No session, but a cs2.exe is up: there is no console to drive, so this
    # must say so rather than spawning a second game that cannot start.
    real_running = clip_record.cs2_is_running
    clip_record.cs2_is_running = lambda *a, **k: True
    undo_ready = _stub_watch_ready()
    try:
        _with_session(None, None, None)
        try:
            clip_record.launch_demo_at(__file__, 20000, 'Alice')
        except clip_record.ClipError as e:
            ok('not started from here' in str(e), 'explains why it cannot drive it')
        else:
            ok(False, 'must raise when an unrelated CS2 is running')
    finally:
        undo_ready()
        clip_record.cs2_is_running = real_running
        _with_session(None, None, None)


for _fn in list(globals().values()):
    if callable(_fn) and getattr(_fn, "__name__", "").startswith("test_"):
        _fn()

print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
