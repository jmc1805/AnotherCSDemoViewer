"""Unit tests for paths.py's two modes - a checkout and the frozen desktop build.

In a checkout every root is the repo and nothing moves. Frozen (PyInstaller
sets sys.frozen / sys._MEIPASS), the read-only payload comes from the bundle
while DATA_DIR and ASSETS_DIR - both written at runtime - move to the per-user
data folder. The env overrides win in both modes.

Also covers procutil.NO_WINDOW, the flag set every helper subprocess takes.

Run:  python3 test/paths_test.py     (no deps)
"""
import importlib
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
import paths  # noqa: E402
import procutil  # noqa: E402

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


_ENV_KEYS = ("CS2VIEWER_DATA_DIR", "CS2VIEWER_ASSETS_DIR", "LOCALAPPDATA", "XDG_DATA_HOME")


def reload_with(env, frozen=False, meipass=None):
    """Re-import paths under the given env and frozen state, then restore both.
    Returns the reloaded module's namespace as a plain dict."""
    saved_env = {k: os.environ.get(k) for k in _ENV_KEYS}
    had_frozen, had_meipass = hasattr(sys, "frozen"), hasattr(sys, "_MEIPASS")
    try:
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        os.environ.update(env)
        if frozen:
            sys.frozen = True
        if meipass:
            sys._MEIPASS = meipass
        mod = importlib.reload(paths)
        ns = dict(vars(mod))
        ns["hint"] = mod.binary_missing_hint()   # reads FROZEN at call time
        return ns
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if frozen and not had_frozen:
            del sys.frozen
        if meipass and not had_meipass:
            del sys._MEIPASS
        importlib.reload(paths)


def test_checkout_mode_keeps_everything_in_the_repo():
    p = reload_with({})
    eq(p["FROZEN"], False, "a plain import is not frozen")
    eq(p["BUNDLE_ROOT"], p["ROOT"], "bundle root is the repo")
    eq(p["DATA_DIR"], os.path.join(p["ROOT"], "data"), "data under the repo")
    eq(p["STATIC_DIR"], os.path.join(p["ROOT"], "static"), "static under the repo")
    eq(p["ASSETS_DIR"], os.path.join(p["ROOT"], "static", "assets"), "assets stay in static/")
    eq(p["MAP_IMG_DIR"], os.path.join(p["ASSETS_DIR"], "overheadmaps"), "radars follow assets")


def test_env_overrides_win_in_a_checkout():
    d = tempfile.mkdtemp()
    a = os.path.join(d, "icons")
    p = reload_with({"CS2VIEWER_DATA_DIR": d, "CS2VIEWER_ASSETS_DIR": a})
    eq(p["DATA_DIR"], d, "CS2VIEWER_DATA_DIR wins")
    eq(p["DEMO_MAP_PATH"], os.path.join(d, "demo_map.json"), "data children follow")
    eq(p["ASSETS_DIR"], a, "CS2VIEWER_ASSETS_DIR wins")
    eq(p["ASSETS_MANIFEST"], os.path.join(a, "manifest.json"), "manifest follows assets")


def test_frozen_mode_reads_the_bundle_and_writes_the_user_folder():
    bundle = tempfile.mkdtemp()
    home = tempfile.mkdtemp()
    p = reload_with({"LOCALAPPDATA": home, "XDG_DATA_HOME": home},
                    frozen=True, meipass=bundle)
    want_data = os.path.join(home, "CS2Viewer" if os.name == "nt" else "cs2viewer")
    eq(p["FROZEN"], True, "sys.frozen is read")
    eq(p["BUNDLE_ROOT"], bundle, "bundle root is _MEIPASS")
    eq(p["STATIC_DIR"], os.path.join(bundle, "static"), "static comes from the bundle")
    eq(p["TEMPLATE_DIR"], os.path.join(bundle, "templates"), "templates come from the bundle")
    eq(p["DATA_DIR"], want_data, "data goes to the per-user folder")
    eq(p["ASSETS_DIR"], os.path.join(want_data, "assets"),
       "assets are written at runtime, so they leave the read-only bundle")
    eq(p["MAP_IMG_DIR"], os.path.join(want_data, "assets", "overheadmaps"), "radars follow")
    eq(p["LOGS_DIR"], os.path.join(want_data, "logs"), "logs under data")
    eq(p["PARSER_BIN"].startswith(os.path.join(bundle, "bin")), True,
       "the parser is looked for in the bundle")
    ok("reinstall" in p["hint"], "frozen hint says reinstall, not build.bat")


def test_frozen_mode_still_honours_the_data_override():
    bundle = tempfile.mkdtemp()
    d = tempfile.mkdtemp()
    p = reload_with({"CS2VIEWER_DATA_DIR": d}, frozen=True, meipass=bundle)
    eq(p["DATA_DIR"], d, "CS2VIEWER_DATA_DIR wins when frozen")
    eq(p["ASSETS_DIR"], os.path.join(d, "assets"), "assets follow the overridden data dir")


def test_reload_restores_checkout_mode():
    eq(paths.FROZEN, False, "the helper leaves paths in checkout mode")
    eq(paths.BUNDLE_ROOT, paths.ROOT, "and the bundle root back on the repo")


def test_no_window_is_windows_only():
    if os.name == "nt":
        eq(set(procutil.NO_WINDOW), {"creationflags"}, "Windows gets creationflags")
    else:
        eq(procutil.NO_WINDOW, {}, "no flags off Windows")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)
