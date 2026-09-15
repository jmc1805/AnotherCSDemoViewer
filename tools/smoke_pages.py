"""smoke_pages.py - load every page in a real browser and fail on JS errors.

The unit suites cover the pure logic modules under node, and Flask's test
client covers the routes, but neither loads a page the way a browser does. The
classic <script> modules here depend on each other by load order (FmtLogic,
MapsLogic and Match2DLogic are read by the modules that come after them), and a
missing or misordered tag is invisible to both other suites: the server still
returns 200 and the tests still pass, while the page throws on load.

Run:  python tools/smoke_pages.py [--headed] [--shot-dir DIR]

Needs playwright (`pip install playwright && playwright install chromium`) and
a corpus to point at - set CS2VIEWER_DATA_DIR as usual. Starts its own server
on a free port, so nothing needs to be running first.

Exits non-zero, naming the page and the error, if any page logs a console error
or throws. Optionally writes a screenshot per page at two widths, which is how
the layout is checked at half-screen.
"""
import argparse
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Widths the layout is expected to hold at: a QUARTER-screen window, a
# half-screen one, and full HD. 640px is the one that earns its place - every
# overflow this file has ever caught was a stated minimum (a grid track, a flex
# item's min-width, a fixed width in a later media query) that only stops
# fitting down there; 950px passed clean through all of them.
#
# 1368x830 is a Surface-class window (1368x912 CSS px at 200% scaling, minus
# window chrome) - short and wide rather than either of the other two shapes.
# static/fitview.js exists specifically to make the /viewer page fit here with
# no page scroll; nothing before this line ever checked that claim.
#
# The WQHD/4K entries below are two other common desktop sizes, each at full
# screen, half (snapped side by side, height minus ~40px of window chrome),
# and quarter (snapped to one corner, half of that half) - the same three
# shapes as the 1080p set above, just at the larger resolutions a lot of
# monitors actually run at.
VIEWPORTS = [
    (640, 520), (950, 1000), (1368, 830), (1920, 1080),
    (2560, 1440), (1280, 1400), (1280, 700),   # WQHD: full, half, quarter
    (3840, 2160), (1920, 2120), (1920, 1060),  # 4K:   full, half, quarter
]

# Pages/widths/heights that must never need a VERTICAL page scroll either -
# just the replayer, and only where index.html's CSS actually applies
# static/fitview.js's --fit-h (its own `@media (min-width: 901px)` gate - the
# stacked tiers below that ignore --fit-h and fall back to an ordinary,
# expected page scroll by design). Every other page (stats, analyser,
# dashboards) scrolls vertically by design once its content outgrows the
# window regardless of size; only sideways scroll is checked for those.
#
# The height floor is real, not a guess: fitview.logic.js has its own
# documented minSide fallback ("a window too short to fit shows the map
# whole and scrolls") - index.html's limits.minSide=430 (.map-col's own
# min-width) - and measuring the actual transition at 1280px width found
# --fit-h pinned at exactly that 430px floor (genuinely scrolling) through
# 780px tall, clear by 800px. 800 is therefore the shortest height this
# mechanism can promise "no scroll" at, not an arbitrary cutoff.
NO_VERTICAL_SCROLL_MIN_WIDTH = 901
NO_VERTICAL_SCROLL_MIN_HEIGHT = 800
NO_VERTICAL_SCROLL = {'viewer'}

# Console messages that are expected and not a failure. Keep this list short
# and specific - a broad pattern here would hide the failures the file exists
# to catch.
IGNORE = (
    'favicon',                       # no favicon is shipped
    'ERR_INTERNET_DISCONNECTED',     # the Google Fonts <link>, offline
    'fonts.googleapis.com',
    'fonts.gstatic.com',
)


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def wait_for(url, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return True
        except urllib.error.HTTPError:
            return True          # responding, even if this path 404s
        except OSError:
            time.sleep(0.25)
    return False


def pages(first_match):
    """The URLs to load. Match-scoped pages are skipped when the corpus is
    empty, so this still runs as a bare-install smoke test."""
    out = [('dashboard', '/'), ('players', '/players'), ('settings', '/settings'),
           ('overwatch-dashboard', '/overwatch/dashboard'),
           ('analyser', '/multi?scope=all')]
    if first_match:
        out += [('match', f'/match?match={first_match}'),
                ('viewer', f'/viewer?match={first_match}'),
                ('multi-match', f'/multi?match={first_match}')]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--headed', action='store_true', help='show the browser')
    ap.add_argument('--shot-dir', help='write a screenshot per page and width')
    ap.add_argument('--settle', type=float, default=2.5,
                    help='seconds to let a page finish its fetches (default 2.5)')
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright
    import app as flask_app

    matches = flask_app._list_match_files()
    first = matches[0] if matches else None
    print(f'corpus: {len(matches)} match(es)' + (f', using {first}' if first else ''))

    port = free_port()
    base = f'http://127.0.0.1:{port}'
    threading.Thread(
        target=lambda: flask_app.app.run(port=port, threaded=True, use_reloader=False),
        daemon=True).start()
    if not wait_for(base + '/'):
        print('server did not come up', file=sys.stderr)
        return 1

    if args.shot_dir:
        os.makedirs(args.shot_dir, exist_ok=True)

    failures = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        for width, height in VIEWPORTS:
            ctx = browser.new_context(viewport={'width': width, 'height': height})
            for name, path in pages(first):
                page = ctx.new_page()
                problems = []
                page.on('console', lambda m, acc=problems:
                        acc.append(m.text) if m.type == 'error' else None)
                page.on('pageerror', lambda e, acc=problems: acc.append(str(e)))
                page.goto(base + path, wait_until='load', timeout=30000)
                # Most pages paint from a fetch, so errors surface after load.
                page.wait_for_timeout(int(args.settle * 1000))
                if args.shot_dir:
                    page.screenshot(path=os.path.join(args.shot_dir, f'{name}-{width}.png'),
                                    full_page=True)
                real = [t for t in problems if not any(i in t for i in IGNORE)]
                # A page must never scroll the document sideways; wide content
                # scrolls inside its own container instead.
                overflow = page.evaluate(
                    'document.documentElement.scrollWidth > document.documentElement.clientWidth + 2')
                # The replayer additionally must never need a vertical page
                # scroll either - that's the one thing static/fitview.js
                # exists to guarantee (see NO_VERTICAL_SCROLL above).
                v_overflow = (name in NO_VERTICAL_SCROLL and width >= NO_VERTICAL_SCROLL_MIN_WIDTH
                              and height >= NO_VERTICAL_SCROLL_MIN_HEIGHT
                              and page.evaluate(
                    'document.documentElement.scrollHeight > document.documentElement.clientHeight + 2'))
                status = 'ok  '
                if real or overflow or v_overflow:
                    status = 'FAIL'
                    failures.append((name, width, real, overflow or v_overflow))
                print(f'{status}  {name:<20} {width}px  {path}')
                for t in real:
                    print(f'        console: {t[:300]}')
                if overflow:
                    print('        page scrolls horizontally')
                if v_overflow:
                    print('        page scrolls vertically (fitview should prevent this)')
                page.close()
            ctx.close()
        browser.close()

    print()
    if failures:
        print(f'{len(failures)} page/width combination(s) failed')
        return 1
    print('all pages loaded clean at ' + ', '.join(f'{w}px' for w, _ in VIEWPORTS))
    return 0


if __name__ == '__main__':
    sys.exit(main())
