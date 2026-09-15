# desktop.spec - PyInstaller recipe for the desktop build (desktop.py).
#
# Run through tools/build_desktop.py, which builds the stripped Go binaries
# first and checks the result for Valve content afterwards. `pyinstaller
# desktop.spec` alone works too once bin/ is built.
#
# The payload is listed explicitly, never globbed from the repo root:
# static/assets/ and static/map/ are Valve content that must never reach a
# redistributed installer, and data/ is the developer's own matches.
import os
import sys

ROOT = os.path.abspath(SPECPATH)  # noqa: F821 - injected by PyInstaller
WIN = sys.platform == 'win32'


def tree(src, dest, skip_top=()):
    """(file, dest_dir) pairs for everything under src, minus top-level dirs
    named in skip_top and any __pycache__."""
    out = []
    for dirpath, dirnames, filenames in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        if rel == '.':
            dirnames[:] = [d for d in dirnames if d not in skip_top]
        dirnames[:] = [d for d in dirnames if d != '__pycache__']
        target = dest if rel == '.' else os.path.join(dest, rel)
        out += [(os.path.join(dirpath, f), target) for f in filenames]
    return out


datas = []
datas += tree(os.path.join(ROOT, 'templates'), 'templates')
datas += tree(os.path.join(ROOT, 'static'), 'static', skip_top=('assets', 'map'))
# Committed zone-callout seed data (paths.MAP_ZONES_SEED_DIR) - small measured
# numbers, not Valve art/textures, so unlike static/assets/static/map this is
# meant to ship. Gives an installed build zone data on first launch with no
# extraction step; analysis/mapzones.py prefers a fresher install-local
# extraction over this when both exist.
datas += tree(os.path.join(ROOT, 'map_zones'), 'map_zones')
# The extractors only extract; tint + index is assetindex.py, bundled as a module.
for name in ('extract_ui_assets.ps1', 'extract_ui_assets.sh', 'extract_ui_assets.bat'):
    datas.append((os.path.join(ROOT, 'tools', name), 'tools'))

# Only this platform's Go binaries (a Windows installer carrying the Linux ones
# would be 33 MB of dead weight). Shipped as data, not `binaries`, so PyInstaller
# doesn't try to dependency-scan static Go executables.
for name in ('parser', 'overwatch'):
    p = os.path.join(ROOT, 'bin', name + ('.exe' if WIN else ''))
    if not os.path.isfile(p):
        raise SystemExit(f'{p} is missing - build the Go binaries first '
                         '(tools/build_desktop.py does)')
    datas.append((p, 'bin'))

a = Analysis(  # noqa: F821
    [os.path.join(ROOT, 'desktop.py')],
    pathex=[ROOT],
    datas=datas,
    excludes=['tkinter'],
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='CS2Viewer',
    # Windows: a window app, no console. Linux: the build falls back to the
    # browser, and the terminal is how the user sees and stops it.
    console=not WIN,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name='CS2Viewer', upx=False)  # noqa: F821
