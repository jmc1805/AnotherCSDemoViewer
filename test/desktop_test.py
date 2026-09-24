"""Unit tests for desktop.py's plumbing and the app changes it relies on.

The window itself (pywebview) needs a display and is not exercised here; the
parts that decide whether it comes up right are: the single-instance lock, the
port hand-off, the port fallback, where a file picker opens, print() capture in
a console-less build, and that the JS bridge exposes nothing but its methods.

With flask available it also checks the /assets/ route (the frontend's only way
to reach the extracted icons, wherever ASSETS_DIR lives) and that
_save_demo_map is atomic. Without flask those two report a skip.

Run:  python3 test/desktop_test.py

The data and asset dirs are redirected to a temp directory before anything is
imported, so the run never touches a real checkout's data/.
"""
import logging
import os
import socket
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

_TMP = tempfile.mkdtemp(prefix="cs2viewer_desktop_test_")
os.environ["CS2VIEWER_DATA_DIR"] = _TMP
os.environ["CS2VIEWER_ASSETS_DIR"] = os.path.join(_TMP, "assets")

import desktop  # noqa: E402

# The port-fallback test triggers desktop's own warning on purpose; keep it off
# stderr so the summary line stays last (run_all.mjs prints the last line).
logging.getLogger("cs2viewer.desktop").addHandler(logging.NullHandler())
logging.getLogger("cs2viewer.desktop").propagate = False

_passed = 0
_failed = 0
_skipped = []


def ok(cond, msg):
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
        print("  x FAIL:", msg)


def eq(a, b, msg):
    ok(a == b, f"{msg} (got {a!r}, want {b!r})")


def test_instance_lock_is_exclusive_and_released_on_close():
    p = os.path.join(_TMP, "t.lock")
    first = desktop.acquire_instance_lock(p)
    ok(first is not None, "the first launch gets the lock")
    second = desktop.acquire_instance_lock(p)
    ok(second is None, "a second launch is refused while it is held")
    first.close()
    third = desktop.acquire_instance_lock(p)
    ok(third is not None, "closing the holder releases it (as the OS does on a crash)")
    third.close()


def test_port_file_round_trip():
    p = os.path.join(_TMP, "t.port")
    eq(desktop.read_port(p), None, "no file -> no port")
    desktop.write_port(4321, p)
    eq(desktop.read_port(p), 4321, "the written port reads back")
    ok(not os.path.exists(p + ".tmp"), "written atomically, no .tmp left behind")
    with open(p, "w", encoding="utf-8") as f:
        f.write("not a port")
    eq(desktop.read_port(p), None, "garbage -> no port, not a crash")


def test_bind_keeps_the_port_when_free_and_falls_back_when_taken():
    holder = desktop.bind_socket(0)
    taken = holder.getsockname()[1]
    try:
        fallback = desktop.bind_socket(taken)
        ok(fallback.getsockname()[1] != taken, "a taken port falls back to a free one")
        fallback.close()
    finally:
        holder.close()
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    free = probe.getsockname()[1]
    probe.close()
    s = desktop.bind_socket(free)
    eq(s.getsockname()[1], free, "a free port is kept (localStorage is per origin)")
    eq(s.getsockname()[0], "127.0.0.1", "loopback only")
    s.close()


def test_picker_opens_where_the_field_points():
    d = tempfile.mkdtemp(dir=_TMP)
    f = os.path.join(d, "HLAE.exe")
    open(f, "w").close()
    eq(desktop._start_dir(d), d, "a folder opens on itself")
    eq(desktop._start_dir(f), d, "a file opens on its folder")
    eq(desktop._start_dir(f'"{f}"'), d, "quotes pasted from Explorer are ignored")
    eq(desktop._start_dir(os.path.join(_TMP, "nope", "x.exe")), "", "a dead path opens the default")
    eq(desktop._start_dir(""), "", "empty opens the default")


def test_linux_without_gtk_or_qt_goes_straight_to_the_browser():
    none = lambda mod: None  # noqa: E731
    only_gi = lambda mod: object() if mod == "gi" else None  # noqa: E731
    only_qt = lambda mod: object() if mod == "qtpy" else None  # noqa: E731

    def broken(mod):
        raise ValueError("bad spec")

    eq(desktop.gui_backend_available("linux", none), False, "frozen Linux build: no toolkit")
    eq(desktop.gui_backend_available("linux", only_gi), True, "GTK bindings are enough")
    eq(desktop.gui_backend_available("linux", only_qt), True, "Qt bindings are enough")
    eq(desktop.gui_backend_available("linux", broken), False, "a broken probe is 'no toolkit'")
    eq(desktop.gui_backend_available("win32", none), True, "Windows always has WebView2 to try")
    eq(desktop.gui_backend_available("darwin", none), True, "macOS always has WKWebView")


def test_js_bridge_exposes_only_its_methods():
    api = desktop.DesktopApi()
    public = [k for k in vars(api) if not k.startswith("_")]
    eq(public, [], "no public attributes (pywebview would serialise the window object)")
    ok(callable(api.pick_file) and callable(api.pick_folder), "the two pickers exist")


def test_log_stream_turns_prints_into_records():
    records = []

    class Grab(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger = logging.getLogger("cs2viewer.test.stream")
    logger.propagate = False
    logger.addHandler(Grab())
    logger.setLevel(logging.INFO)
    s = desktop._LogStream(logger, logging.INFO)
    print("[demo watch] seek sent", file=s)
    s.write("partial")
    eq(records, ["[demo watch] seek sent"], "one record per completed line")
    s.flush()
    eq(records[-1], "partial", "flush emits the unterminated tail")
    ok(not s.isatty(), "not a tty")


def _app():
    try:
        import app  # noqa: F401
        return app
    except ModuleNotFoundError as exc:   # pragma: no cover - environment, not logic
        _skipped.append(f"app-level tests ({exc.name} not installed)")
        return None


def test_assets_route_serves_assets_dir():
    app = _app()
    if not app:
        return
    import paths
    eq(paths.ASSETS_DIR, os.path.join(_TMP, "assets"), "ASSETS_DIR follows the override")
    os.makedirs(paths.ASSETS_DIR, exist_ok=True)
    with open(paths.ASSETS_MANIFEST, "w", encoding="utf-8") as f:
        f.write('{"premier": {}}')
    client = app.app.test_client()
    r = client.get("/assets/manifest.json")
    eq(r.status_code, 200, "the manifest is served from ASSETS_DIR")
    eq(r.get_data(as_text=True), '{"premier": {}}', "with its content")
    r.close()
    r = client.get("/assets/../demo_map.json")
    ok(r.status_code in (400, 404), f"nothing outside ASSETS_DIR (got {r.status_code})")
    r.close()


def test_terminal_command_wraps_the_app_and_holds_the_window_open():
    have = {'gnome-terminal': '/usr/bin/gnome-terminal', 'xterm': '/usr/bin/xterm'}
    which = have.get
    cmd = desktop.terminal_command(['/opt/CS2Viewer'], {'DISPLAY': ':0'}, which)
    eq(cmd[:2], ['/usr/bin/gnome-terminal', '--'], 'uses the first terminal it knows')
    eq(cmd[-1], '/opt/CS2Viewer', 'the app is the last argument, for "$0"')
    ok('Press Enter to close' in cmd[cmd.index('-c') + 1],
       'the window stays open after the app exits, so "stopped" is visible')
    eq(desktop.terminal_command(['x'], {'DISPLAY': ':0', 'TERMINAL': 'xterm'}, which)[:2],
       ['/usr/bin/xterm', '-e'], '$TERMINAL is preferred when known')
    eq(desktop.terminal_command(['x'], {'DISPLAY': ':0', 'TERMINAL': 'weirdterm'}, which)[0],
       '/usr/bin/gnome-terminal', 'an unknown $TERMINAL is skipped, not guessed at')
    eq(desktop.terminal_command(['x'], {}, which), None, 'no display -> no terminal')
    eq(desktop.terminal_command(['x'], {'DISPLAY': ':0'}, {}.get), None,
       'no emulator installed -> None, the app just runs as before')


def test_relaunch_in_terminal_only_when_frozen_linux_without_a_tty():
    real_frozen, real_which = desktop.paths.FROZEN, desktop.shutil.which
    real_popen, real_stdin, real_stderr = desktop.subprocess.Popen, sys.stdin, sys.stderr
    class _NoTty:
        def isatty(self):
            return False
    launched = []
    desktop.shutil.which = lambda n: '/usr/bin/xterm' if n == 'xterm' else None
    desktop.subprocess.Popen = lambda cmd, **kw: launched.append((cmd, kw))
    sys.stdin = sys.stderr = _NoTty()
    env = {'DISPLAY': ':0'}
    try:
        desktop.paths.FROZEN = False
        ok(not desktop.relaunch_in_terminal(['a'], env), 'a checkout run is left alone')
        desktop.paths.FROZEN = True
        ok(desktop.relaunch_in_terminal(['a'], env) == sys.platform.startswith('linux'),
           'frozen Linux with no terminal relaunches itself')
        if sys.platform.startswith('linux'):
            eq(launched[-1][1]['env'][desktop.TERMINAL_GUARD_ENV], '1',
               'the child is marked so it cannot relaunch again')
            ok(not desktop.relaunch_in_terminal(['a'], dict(env, **{desktop.TERMINAL_GUARD_ENV: '1'})),
               'the guard stops a relaunch loop')
            ok(not desktop.relaunch_in_terminal(['a'], dict(env, CS2VIEWER_NO_TERMINAL='1')),
               'the opt-out is honoured')
            n = len(launched)
            sys.stdin = sys.stderr = type('T', (), {'isatty': lambda self: True})()
            ok(not desktop.relaunch_in_terminal(['a'], env) and len(launched) == n,
               'already in a terminal -> nothing to do')
    finally:
        desktop.paths.FROZEN, desktop.shutil.which = real_frozen, real_which
        desktop.subprocess.Popen, sys.stdin, sys.stderr = real_popen, real_stdin, real_stderr


def test_save_demo_map_is_atomic():
    app = _app()
    if not app:
        return
    app._save_demo_map({"m1": "a.dem"})
    eq(app._load_demo_map(), {"m1": "a.dem"}, "round-trips")
    ok(not os.path.exists(app.DEMO_MAP_PATH + ".tmp"), "no .tmp left behind")

    class Unserialisable:
        pass

    try:
        app._save_demo_map({"m1": Unserialisable()})
    except TypeError:
        pass
    eq(app._load_demo_map(), {"m1": "a.dem"},
       "a failed write leaves the previous map intact instead of truncating it")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    for s in dict.fromkeys(_skipped):
        print("  - skipped:", s)
    print(f"{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)
