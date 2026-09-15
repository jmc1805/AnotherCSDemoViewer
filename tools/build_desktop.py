"""tools/build_desktop.py - build the desktop app for this platform.

    python tools/build_desktop.py               Go binaries, bundle, package
    python tools/build_desktop.py --no-go       reuse bin/ as it is
    python tools/build_desktop.py --no-package  stop at dist/CS2Viewer/

Run it with the desktop venv (requirements-desktop.txt) made from a python.org
CPython - PyInstaller refuses Microsoft Store Python. PyInstaller does not
cross-compile, so each platform's release is built on that platform, and with
no CI a release is a local build.

  1. go build -ldflags="-s -w" into bin/ (stripped: ~25-30% smaller)
  2. pyinstaller desktop.spec -> dist/CS2Viewer/
  3. refuse to go on if the bundle holds Valve content or developer data
  4. package. Windows: an Inno Setup installer (installer/cs2viewer.iss) that
     carries Microsoft's WebView2 bootstrapper, when Inno Setup 6 is installed;
     otherwise dist/CS2Viewer/ is left as a runnable folder. Linux: a .tar.gz
     plus a script that adds a menu entry.

The WebView2 bootstrapper is downloaded once into build/ - a build-time fetch;
the app itself still makes no outbound calls.
"""
import argparse
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIN = os.name == 'nt'
DIST = os.path.join(ROOT, 'dist')
BUILD = os.path.join(ROOT, 'build')
APP_DIR = os.path.join(DIST, 'CS2Viewer')
WEBVIEW2_URL = 'https://go.microsoft.com/fwlink/p/?LinkId=2124703'
WEBVIEW2_NAME = 'MicrosoftEdgeWebview2Setup.exe'   # installer/cs2viewer.iss runs this name


def run(argv):
    print('>', ' '.join(argv), flush=True)
    subprocess.run(argv, cwd=ROOT, check=True)


def version():
    try:
        out = subprocess.run(['git', 'describe', '--tags', '--always', '--dirty'], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        out = ''
    return out or 'dev'


def build_go():
    os.makedirs(os.path.join(ROOT, 'bin'), exist_ok=True)
    ext = '.exe' if WIN else ''
    for name in ('parser', 'overwatch'):
        run(['go', 'build', '-ldflags=-s -w', '-o', os.path.join('bin', name + ext),
             './cmd/' + name])


def build_bundle():
    run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
         '--distpath', DIST, '--workpath', os.path.join(BUILD, 'pyinstaller'),
         'desktop.spec'])
    if not WIN:   # data files may lose the exec bit on the way in
        for dirpath, _, filenames in os.walk(APP_DIR):
            if os.path.basename(dirpath) == 'bin':
                for f in filenames:
                    p = os.path.join(dirpath, f)
                    os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def payload_problems(app_dir):
    """Paths in the bundle that must never ship: Valve content (the extracted
    asset tree, the old radar folder, radar PNGs, VPK/texture files) and
    developer data (demos, parsed matches). -> list of relative paths."""
    bad = []
    data_dirs = {'processed_matches', 'demos', 'chunks', 'overwatch_raw', 'clips'}
    for dirpath, dirnames, filenames in os.walk(app_dir):
        rel = os.path.relpath(dirpath, app_dir).replace(os.sep, '/')
        for d in dirnames:
            if (os.path.basename(dirpath) == 'static' and d in ('assets', 'map')) or d in data_dirs:
                bad.append(f'{rel}/{d}/')
        for f in filenames:
            low = f.lower()
            if (low.endswith(('.vpk', '.vtex_c', '.dem'))
                    or ('_radar' in low and low.endswith('.png'))):
                bad.append(f'{rel}/{f}')
    return bad


def find_iscc():
    cands = [shutil.which('iscc')]
    for base in (os.environ.get('ProgramFiles(x86)'), os.environ.get('ProgramFiles'),
                 os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs')):
        if base:
            cands.append(os.path.join(base, 'Inno Setup 6', 'ISCC.exe'))
    return next((c for c in cands if c and os.path.isfile(c)), None)


def package_windows(ver):
    iscc = find_iscc()
    if not iscc:
        print('\nInno Setup 6 not found, so no installer was built. dist/CS2Viewer/ '
              'runs as is (CS2Viewer.exe).\nInstall Inno Setup from '
              'https://jrsoftware.org/isinfo.php and re-run with --no-go to package.')
        return
    boot = os.path.join(BUILD, WEBVIEW2_NAME)
    if not os.path.isfile(boot):
        os.makedirs(BUILD, exist_ok=True)
        print(f'Downloading the WebView2 bootstrapper -> {boot}')
        urllib.request.urlretrieve(WEBVIEW2_URL, boot)
    run([iscc, f'/DAppVersion={ver}', f'/DSourceDir={APP_DIR}',
         f'/DWebView2Bootstrapper={boot}', f'/O{DIST}',
         os.path.join('installer', 'cs2viewer.iss')])


LINUX_MENU_SCRIPT = """#!/bin/sh
# Adds CS2 Demo Viewer to your application menu. Run once from the extracted
# folder; run again if you move the folder.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$APPS"
cat > "$APPS/cs2viewer.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=CS2 Demo Viewer
Comment=2D replays and stats for Counter-Strike 2 demos
Exec="$HERE/CS2Viewer"
Path=$HERE
Terminal=true
Categories=Game;Utility;
EOF
echo "Added: $APPS/cs2viewer.desktop"
"""


def package_linux(ver):
    script = os.path.join(APP_DIR, 'install-menu-entry.sh')
    with open(script, 'w', encoding='utf-8', newline='\n') as f:
        f.write(LINUX_MENU_SCRIPT)
    os.chmod(script, 0o755)
    out = os.path.join(DIST, f'CS2Viewer-{ver}-linux-x64.tar.gz')
    with tarfile.open(out, 'w:gz') as t:
        t.add(APP_DIR, arcname='CS2Viewer')
    print(f'\nWrote {out}')


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--no-go', action='store_true', help='reuse bin/ as it is')
    ap.add_argument('--no-package', action='store_true', help='stop at dist/CS2Viewer/')
    args = ap.parse_args()

    if not args.no_go:
        build_go()
    build_bundle()
    bad = payload_problems(APP_DIR)
    if bad:
        print('\nREFUSING TO PACKAGE - the bundle contains files that must not ship:')
        for b in bad:
            print('   ', b)
        sys.exit(1)
    print('\nPayload check passed: no Valve content, no match data.')
    if args.no_package:
        return
    ver = version()
    (package_windows if WIN else package_linux)(ver)


if __name__ == '__main__':
    main()
