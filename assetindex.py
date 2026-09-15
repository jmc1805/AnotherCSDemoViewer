"""assetindex.py - the last step of getting CS2 UI images into the app: tint
the Premier banner, then write the manifest the frontend looks images up in.

Replaces tools/make_assets_manifest.mjs and tools/tint_premier_banner.mjs,
which made Node.js a requirement of every icon feature - the installed desktop
app included, which ships no Node. Stdlib only. app.py runs it in-process after
an extraction, after a manual import and for "Rebuild index" (`finish()`); the
extractor scripts call it as a CLI only when run by hand:

    python assetindex.py [<assets dir>]      tint + index; default paths.ASSETS_DIR

Two outputs, both under the assets dir:

  premier/premier_tier0..6.svg + premier_none.svg
      CS2 ships no per-rating Premier badge - one grey parallelogram
      (premier_rating_bg_large.svg) that the game colours at runtime, in
      panorama/styles/rating_emblem.vcss_c:
          .tier-N .premier-rating .rating__bg { wash-color: color-csrating-tier-N; }
      `wash-color` is Panorama's per-channel multiply. Flattening it here rather
      than reproducing it in CSS keeps the art's contrast: the near-white
      chevrons (#E6E6E6) multiply to the vivid tier colour while the plate
      (#6B6A6A) multiplies to a deep one, and a mask over a flat colour loses
      exactly that. The grey sources stay beside the output so a rebuild can
      re-tint; the manifest never indexes them.

  manifest.json
      {version: 1, generated, kinds: {kind: {key: "kind/file"}}}, read by
      static/assets.js. Keys come from derive_key(); aliasing lives in
      static/assets.logic.js so it stays testable.

TIER_COLORS and the tier boundary are transcriptions of the game's own files
(rating_emblem.vcss_c's @define block; GetClampedRating() in rating_emblem.vts_c
is clamp(floor(rating/5000), 0, 6)). Do not eyeball them - re-extract after a
game update instead.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone

import paths

# What the manifest indexes. assetimport.IMAGE_EXTS routes the same set.
IMAGE_EXTS = frozenset({'.png', '.jpg', '.jpeg', '.webp', '.svg'})

# deathnotice VPK filenames -> the logical condition key the killfeed looks up
# (static/killfeed.js). An explicit table because the raw names share no one
# stemming rule (icon_ prefix, _kill suffix, or neither).
DEATHNOTICE_KEYS = {
    'icon_headshot': 'headshot',
    'penetrate': 'wallbang',
    'noscope': 'noscope',
    'smoke_kill': 'smoke',
    'blind_kill': 'blind',
    'inairkill': 'air',
    'icon_suicide': 'suicide',
    'smokegrenade_impact': 'smokegrenade_impact',
}

# @define color-csrating-tier-0..6 in panorama/styles/rating_emblem.vcss_c.
TIER_COLORS = (
    '#b0c3d9',  # 0      - 4999
    '#8cc6ff',  # 5000   - 9999
    '#6a7dff',  # 10000  - 14999
    '#c166ff',  # 15000  - 19999
    '#f03cff',  # 20000  - 24999
    '#eb4b4b',  # 25000  - 29999
    '#ffd700',  # 30000+
)

# The game's grey banner art, as the extractor copies it into <assets>/premier/.
PREMIER_SOURCE = 'premier_rating_bg_large.svg'
PREMIER_SOURCE_NONE = 'premier_rating_bg_large_none.svg'
PREMIER_SOURCES = (PREMIER_SOURCE, PREMIER_SOURCE_NONE)

_SKILLGROUP = re.compile(r'^skillgroup(\d+)$')
_PREMIER_TIER = re.compile(r'^premier_tier([0-6])$')
_HEX6 = re.compile(r'#[0-9A-Fa-f]{6}\b', re.ASCII)
# Valve's exported gradient/clip ids (paint0_linear_632_1563, clip0_632_1563).
_VALVE_ID = re.compile(r'_632_\d+')


class IndexError_(Exception):
    """Nothing indexable was found - the manifest was not written."""


# ── keys ──────────────────────────────────────────────────────────────────────

def derive_key(kind, filename):
    """The logical key `filename` is reachable under for `kind`, or None when it
    is not an asset of that kind. The exact VPK filenames vary by game build, so
    each kind strips its known prefixes/suffixes rather than assuming one scheme."""
    stem = os.path.splitext(filename)[0].lower()
    if kind == 'skillgroups':
        # ONLY the competitive/wingman ladder skillgroup1..18 - the folder also
        # holds dangerzone*/wingman* art with the same trailing digits, which
        # would collide on the numeric key.
        m = _SKILLGROUP.match(stem)
        return str(int(m.group(1))) if m else None
    if kind == 'map_icons':
        # map_icon_de_dust2.png -> "de_dust2"; other art in the tree -> None.
        return stem[len('map_icon_'):] or None if stem.startswith('map_icon_') else None
    if kind == 'overheadmaps':
        # de_dust2_radar.png / de_dust2_radar_psd.png / de_dust2.png -> "de_dust2"
        return re.sub(r'_psd$', '', re.sub(r'_radar(_psd)?$', '', stem)) or None
    if kind == 'equipment':
        # ak47.png / weapon_ak47.png -> "ak47" (the frontend also tries the
        # weapon_-prefixed form).
        return re.sub(r'^weapon_', '', stem) or None
    if kind == 'deathnotice':
        return DEATHNOTICE_KEYS.get(stem)
    if kind == 'premier':
        # Only the tinted output: premier_tier0..6 -> "0".."6", premier_none ->
        # "none". The grey source art is never what the UI should draw.
        if stem == 'premier_none':
            return 'none'
        m = _PREMIER_TIER.match(stem)
        return m.group(1) if m else None
    return stem or None


def scan_kind(assets_dir, kind):
    """-> ({key: "kind/file"}, dropped) for one kind directory, scanned
    NON-recursively (assetimport flattens into it for exactly this reason).
    Sorted, first file wins a key; a missing directory is an empty kind."""
    d = os.path.join(assets_dir, kind)
    try:
        entries = sorted(os.listdir(d))
    except OSError:
        return {}, 0
    table, dropped = {}, 0
    for f in entries:
        if os.path.splitext(f)[1].lower() not in IMAGE_EXTS:
            continue
        if not os.path.isfile(os.path.join(d, f)):
            continue
        if kind == 'premier' and f in PREMIER_SOURCES:
            continue   # tint_premier's input, deliberately unindexed - not a skip
        key = derive_key(kind, f)
        if not key or key in table:
            dropped += 1
            continue
        table[key] = f'{kind}/{f}'
    return table, dropped


def write_manifest(assets_dir, generated=None):
    """Scan every kind and write <assets_dir>/manifest.json atomically.

    -> (total, log lines). Raises IndexError_ when there is nothing to index,
    and writes nothing then - an empty manifest would switch the icon features
    on with no icons behind them."""
    kinds, lines, total = {}, [], 0
    for kind in paths.ASSET_KINDS:
        table, dropped = scan_kind(assets_dir, kind)
        kinds[kind] = table
        total += len(table)
        lines.append(f'  {kind}: {len(table)} asset(s)')
        if dropped:
            lines.append(f'  {kind}: {dropped} file(s) skipped (unrecognized name or duplicate key)')
    if total == 0:
        raise IndexError_(f'no assets found under {assets_dir} - extract or import the icons first')
    if generated is None:
        generated = datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    out = os.path.join(assets_dir, 'manifest.json')
    tmp = out + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        f.write(json.dumps({'version': 1, 'generated': generated, 'kinds': kinds},
                           separators=(',', ':'), ensure_ascii=False) + '\n')
    os.replace(tmp, out)
    lines.append(f'wrote {out} ({total} assets across {len(paths.ASSET_KINDS)} kinds)')
    return total, lines


# ── premier tint ──────────────────────────────────────────────────────────────

def _expand(hex_):
    h = str(hex_).strip().lower()
    if re.fullmatch(r'#[0-9a-f]{3}', h):
        return '#' + ''.join(ch * 2 for ch in h[1:])
    if re.fullmatch(r'#[0-9a-f]{6}', h):
        return h
    raise ValueError(f'not a hex colour: {hex_}')


def wash_color(hex_, wash):
    """Panorama's wash-color: per-channel 8-bit multiply. -> lowercase #rrggbb."""
    c, w = _expand(hex_), _expand(wash)
    out = '#'
    for i in (1, 3, 5):
        v = int(c[i:i + 2], 16) * int(w[i:i + 2], 16)
        out += format((2 * v + 255) // 510, '02x')   # round(v / 255), half up, exact
    return out


def tint_svg(svg, wash, id_suffix):
    """Tint every 6-digit hex literal by `wash` and rewrite Valve's ids with
    `id_suffix`. The ids matter: the seven files share them, and stacked in one
    document the first tier's gradients would win for every later one."""
    svg = _HEX6.sub(lambda m: wash_color(m.group(0), wash), svg)
    return _VALVE_ID.sub('_' + id_suffix, svg)


def _read(path):
    with open(path, encoding='utf-8', newline='') as f:
        return f.read()


def _write(path, text):
    with open(path, 'w', encoding='utf-8', newline='') as f:
        f.write(text)


def tint_premier(assets_dir):
    """Write premier_tier0..6.svg (+ premier_none.svg) from the grey sources in
    <assets_dir>/premier/. -> (files written, log lines). No source is not an
    error: a manual import may bring the tinted files themselves."""
    d = os.path.join(assets_dir, 'premier')
    src = os.path.join(d, PREMIER_SOURCE)
    if not os.path.isfile(src):
        return 0, []
    rated = _read(src)
    n = 0
    for tier, wash in enumerate(TIER_COLORS):
        _write(os.path.join(d, f'premier_tier{tier}.svg'), tint_svg(rated, wash, f't{tier}'))
        n += 1
    lines = []
    # The unrated banner draws its own "- - -" and only ever shows at tier 0.
    none = os.path.join(d, PREMIER_SOURCE_NONE)
    if os.path.isfile(none):
        _write(os.path.join(d, 'premier_none.svg'), tint_svg(_read(none), TIER_COLORS[0], 'tn'))
        n += 1
    else:
        lines.append('  premier: no _none variant found, skipping the unrated banner')
    lines.append(f'  premier: {n} tinted banner(s)')
    return n, lines


# ── entry points ──────────────────────────────────────────────────────────────

def finish(assets_dir=None):
    """Tint, then index. -> (ok, one-line message, log lines). Never raises for
    an expected failure, so a route can report it instead of 500ing."""
    assets_dir = assets_dir or paths.ASSETS_DIR
    try:
        _, lines = tint_premier(assets_dir)
        _, more = write_manifest(assets_dir)
    except IndexError_ as e:
        return False, str(e), [str(e)]
    except OSError as e:
        msg = f'could not write the asset index: {e}'
        return False, msg, [msg]
    lines += more
    return True, lines[-1], lines


def main(argv):
    ok, msg, lines = finish(argv[1] if len(argv) > 1 else None)
    for line in lines[:-1] if not ok else lines:
        print(line)
    if not ok:
        print('error: ' + msg, file=sys.stderr)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
