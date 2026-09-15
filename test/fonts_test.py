"""Unit tests for the typography setup - the rules that keep it from drifting back.

The app had three font families doing two jobs, loaded from
fonts.googleapis.com by a tool whose whole premise is that it runs locally, and
monospace on ~90% of its declarations. All three are structural mistakes that
re-appear the moment someone writes a font-family literal into a page, so they
are asserted rather than described:

  1. No page may reach the network for a font.
  2. No page may name a font family directly; the three --font-* tokens in
     _shell.html are the only spelling, or the fallback chain (and therefore
     what a Cyrillic name renders as) differs per page.
  3. Monospace stays a minority - it is for columns of digits, not for words.

Run:  python3 test/fonts_test.py     (no deps)
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(ROOT, 'templates')
STATIC = os.path.join(ROOT, 'static')

_passed = 0
_failed = 0


def ok(cond, msg):
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
        print(f'  x FAIL: {msg}')


def eq(a, b, msg):
    ok(a == b, f'{msg} (got {a!r}, want {b!r})')


def _sources():
    """Every file that can carry styling, excluding the generated fonts.css."""
    out = []
    for base in (TEMPLATES, STATIC):
        for name in sorted(os.listdir(base)):
            if not name.endswith(('.html', '.css', '.js')):
                continue
            if name == 'fonts.css':
                continue
            out.append(os.path.join(base, name))
    return out


def _read(p):
    with open(p, encoding='utf-8') as f:
        return f.read()


def test_no_remote_font_loads():
    for p in _sources():
        body = _read(p)
        for host in ('fonts.googleapis.com', 'fonts.gstatic.com'):
            ok(host not in body,
               f'{os.path.basename(p)} loads fonts from {host} - the app must render offline')


def test_no_font_family_literals():
    # The families are named in exactly three places: the generated fonts.css
    # (@font-face), _shell.html (where the --font-* tokens are DEFINED, guarded
    # by test_tokens_are_defined_once), and fmt.logic.js (canvas ctx.font, which
    # cannot read a CSS custom property). Anywhere else is a stack that will
    # drift out of step with the others.
    allowed = {'fmt.logic.js', '_shell.html'}
    for p in _sources():
        name = os.path.basename(p)
        if name in allowed:
            continue
        for fam in ('Archivo', 'Roboto Mono', 'Share Tech Mono', 'Rajdhani', 'Barlow'):
            ok(fam not in _read(p),
               f'{name} names the font family {fam!r} directly - use a --font-* token')


def test_tokens_are_defined_once():
    shell = _read(os.path.join(TEMPLATES, '_shell.html'))
    for tok in ('--font-ui', '--font-narrow', '--font-mono'):
        eq(len(re.findall(re.escape(tok) + r'\s*:', shell)), 1,
           f'{tok} is defined exactly once, in _shell.html')
    ok('system-ui' in shell,
       'the stacks fall back to system-ui - Archivo is Latin-only and Steam names are not')


def test_mono_is_a_minority():
    counts = {'ui': 0, 'narrow': 0, 'mono': 0}
    for p in _sources():
        body = _read(p)
        for k in counts:
            counts[k] += len(re.findall(r'var\(--font-%s\)' % k, body))
    total = sum(counts.values())
    ok(total > 100, f'the tokens are actually in use ({total} declarations)')
    # Mono is for aligned digits. It was 90% before; if it climbs back over
    # half, the terminal look is creeping back in.
    ok(counts['mono'] < total / 2,
       f"monospace is a minority: {counts['mono']}/{total} declarations")
    ok(counts['ui'] > counts['mono'],
       f"text beats mono: ui={counts['ui']} vs mono={counts['mono']}")


def test_vendored_fonts_present():
    css = os.path.join(STATIC, 'fonts.css')
    ok(os.path.isfile(css), 'static/fonts.css exists (tools/fetch_fonts.py)')
    body = _read(css)
    for fam in ('Archivo', 'Archivo Narrow', 'Roboto Mono'):
        ok(f"font-family: '{fam}'" in body, f'{fam} has @font-face rules')
    refs = set(re.findall(r"url\('fonts/([^']+)'\)", body))
    ok(len(refs) >= 9, f'font files are referenced locally ({len(refs)} files)')
    for r in sorted(refs):
        ok(os.path.isfile(os.path.join(STATIC, 'fonts', r)),
           f'static/fonts/{r} is committed alongside the CSS')
    # Roboto Mono is the only one covering Cyrillic/Greek; numeric columns are
    # the one place a non-Latin string must not change face mid-column.
    ok(any('roboto-mono-cyrillic' in r for r in refs),
       'the Cyrillic subset of Roboto Mono is vendored')


for _fn in list(globals().values()):
    if callable(_fn) and getattr(_fn, '__name__', '').startswith('test_'):
        _fn()

print(f'\n{_passed} passed, {_failed} failed')
sys.exit(1 if _failed else 0)
