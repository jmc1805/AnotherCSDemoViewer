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
