"""assetimport.py - manual import of the CS2 UI icons, as an alternative to
extracting them from the game's VPK.

`tools/extract_ui_assets.*` is the normal path, but it needs two things at
once: a CS2 installation and Source2Viewer-CLI. That is fine on the
machine that plays the game and impossible on, say, a headless box running the
Docker image, or a laptop with no CS2 install. The images themselves are just
files, so this accepts them directly - a .zip of an already-extracted
`static/assets/` tree, or a handful of loose images - and drops them where the
manifest builder will find them.

Two things this module is careful about, both of which are why the logic lives
here rather than inline in the route:

  Flattening.  The manifest builder (`assetindex.py`) scans each kind directory
      non-recursively, so an image nested one level deeper is silently invisible
      to the app. Every import is written to `<assets>/<kind>/<basename>`, and
      only the basename is ever used to build that path - which also means a
      malicious archive member (`../../app.py`, `C:\\windows\\...`) cannot
      escape the destination, because the directory part of a member name is
      read for *routing* only and never joined to a path.

  Routing.  Which kind a file belongs to is derived from the archive's own
      folder names first (a zipped `static/assets/` already says
      `equipment/ak47.svg`), then from the filename conventions the manifest
      builder itself keys on. Equipment icons have no distinguishing prefix -
      `ak47.svg` could be anything - so an unrecognised name is reported back as
      unrouted rather than guessed into a kind, unless the caller named one
      explicitly. Guessing wrong here does not fail loudly; it produces a
      manifest that quietly lacks the icon.

The routing rules follow `assetindex.derive_key()`. They are intentionally only
used to pick a *directory* - the manifest builder remains the single source of
truth for the lookup keys themselves.
"""
import os
import zipfile

import assetindex
import paths

# What the manifest builder is willing to index - its own set, not a copy.
IMAGE_EXTS = assetindex.IMAGE_EXTS

# Guards against a hostile or simply enormous upload. The whole real asset set
# is ~1200 small files well under 30 MB, so these are generous.
MAX_FILES = 5000
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024

# deathnotice stems, from assetindex's table. Listed rather than pattern-matched
# for the same reason it is there: the raw names share no single stemming rule.
_DEATHNOTICE_STEMS = frozenset(assetindex.DEATHNOTICE_KEYS)

# The game's grey Premier art. assetindex.finish() tints it on import.
_PREMIER_SOURCE_STEMS = frozenset(os.path.splitext(n)[0] for n in assetindex.PREMIER_SOURCES)


def is_image(name):
    """True when `name` has an extension the manifest builder will index."""
    return os.path.splitext(name or '')[1].lower() in IMAGE_EXTS


def basename_of(name):
    """The bare filename of an archive member, or '' if there isn't a usable one.

    Archive members use forward slashes by spec, but zips written on Windows are
    routinely full of backslashes, so both separate. A member whose basename is
    empty (a directory entry), hidden, or a traversal token is rejected - the
    caller must treat '' as "skip this entry".
    """
    raw = (name or '').replace('\\', '/')
    # A trailing separator means a directory entry, not a file with that name.
    # Stripping it first would turn "assets/equipment/" into "equipment" and
    # invite a caller to treat a folder as an importable file.
    if not raw or raw.endswith('/'):
        return ''
    leaf = raw.rsplit('/', 1)[-1]
    if not leaf or leaf.startswith('.') or leaf in ('.', '..'):
        return ''
    # A drive-relative name like "C:foo.png" has no separator to split on.
    if ':' in leaf:
        return ''
    return leaf


def detect_kind(name, explicit=None):
    """Which asset kind `name` belongs in, or None when it can't be told.

    `name` may carry the archive path (`assets/equipment/ak47.svg`); `explicit`
    is a caller-chosen kind that overrides detection entirely. Returns a member
    of paths.ASSET_KINDS or None.
    """
    if explicit in paths.ASSET_KINDS:
        return explicit

    parts = [p for p in (name or '').replace('\\', '/').split('/') if p]
    # A directory named after a kind wins: a zipped static/assets/ tree already
    # states the answer, and it is more trustworthy than any filename guess.
    for part in parts[:-1]:
        if part.lower() in paths.ASSET_KINDS:
            return part.lower()

    stem = os.path.splitext(basename_of(name))[0].lower()
    if not stem:
        return None
    if stem.startswith('skillgroup') and stem[len('skillgroup'):].isdigit():
        return 'skillgroups'
    if stem.startswith('map_icon_'):
        return 'map_icons'
    if stem.endswith('_radar') or stem.endswith('_radar_psd'):
        return 'overheadmaps'
    if stem in _DEATHNOTICE_STEMS:
        return 'deathnotice'
    # The Premier banner: the tinted output as assetindex names it, or the
    # game's own grey source art, which the import's rebuild tints into the
    # seven tier files (only those are indexed - the grey files never are).
    if stem == 'premier_none' or stem in _PREMIER_SOURCE_STEMS:
        return 'premier'
    if stem.startswith('premier_tier') and stem[len('premier_tier'):].isdigit():
        return 'premier'
    # Everything else - most importantly the equipment icons, whose names carry
    # no marker at all - is the caller's call to make.
    return None


def new_report():
    """A fresh accumulator, so one request can span several uploads.

    -> {'imported': dict[str, int],   # per-kind counts of files written
        'total': int, 'bytes': int,
        'unrouted': list[str],        # images whose kind could not be told
        'skipped': list[str],         # not an image, or over the size cap
        'truncated': bool}            # the two lists above were capped
    """
    return {'imported': {}, 'total': 0, 'bytes': 0,
            'unrouted': [], 'skipped': [], 'truncated': False}


def _note(report, bucket, name, cap=25):
    """Record a rejected file, keeping the list short enough to render."""
    if len(report[bucket]) < cap:
        report[bucket].append(name)
    else:
        report['truncated'] = True


def _write(report, dest_root, kind, leaf, data):
    d = os.path.join(dest_root, kind)
    os.makedirs(d, exist_ok=True)
    # Written whole rather than streamed: these are icons, and MAX_FILE_BYTES
    # already bounds them.
    with open(os.path.join(d, leaf), 'wb') as fh:
        fh.write(data)
    report['imported'][kind] = report['imported'].get(kind, 0) + 1
    report['total'] += 1
    report['bytes'] += len(data)


def ingest_zip(fileobj, dest_root, explicit_kind=None, report=None):
    """Import every routable image in a zip. Returns the report dict.

    Raises `zipfile.BadZipFile` if the upload isn't a zip at all, which the
    caller turns into a 400 - an unreadable archive is worth saying out loud
    rather than reporting as "0 files imported".
    """
    report = report if report is not None else new_report()
    with zipfile.ZipFile(fileobj) as zf:
        members = [m for m in zf.infolist() if not m.is_dir()]
        if len(members) > MAX_FILES:
            raise ValueError(f'archive has {len(members)} files; the limit is {MAX_FILES}')
        # Trust the declared sizes only as a first filter - the real total is
        # counted as data is read, so a lying header cannot get past it.
        for m in members:
            leaf = basename_of(m.filename)
            if not leaf or not is_image(leaf):
                _note(report, 'skipped', m.filename)
                continue
            kind = detect_kind(m.filename, explicit_kind)
            if not kind:
                _note(report, 'unrouted', m.filename)
                continue
            if m.file_size > MAX_FILE_BYTES:
                _note(report, 'skipped', m.filename)
                continue
            if report['bytes'] + m.file_size > MAX_TOTAL_BYTES:
                raise ValueError('archive expands past the '
                                 f'{MAX_TOTAL_BYTES // (1024 * 1024)} MB import limit')
            with zf.open(m) as fh:
                data = fh.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                _note(report, 'skipped', m.filename)
                continue
            _write(report, dest_root, kind, leaf, data)
    return report


def ingest_file(name, data, dest_root, explicit_kind=None, report=None):
    """Import one loose image. Returns the report dict."""
    report = report if report is not None else new_report()
    leaf = basename_of(name)
    if not leaf or not is_image(leaf):
        _note(report, 'skipped', name)
        return report
    if len(data) > MAX_FILE_BYTES:
        _note(report, 'skipped', name)
        return report
    kind = detect_kind(name, explicit_kind)
    if not kind:
        _note(report, 'unrouted', name)
        return report
    _write(report, dest_root, kind, leaf, data)
    return report


def summarize(report):
    """One human sentence for the Settings page status line."""
    if not report['total']:
        if report['unrouted']:
            return ('Nothing imported - no folder or filename said which kind these '
                    'belong to. Pick a specific kind above and try again.')
        return 'Nothing imported - no images found in that upload.'
    per = ', '.join(f'{k} {n}' for k, n in sorted(report['imported'].items()))
    msg = f"Imported {report['total']} image(s) · {per}"
    extra = []
    if report['unrouted']:
        extra.append(f"{len(report['unrouted'])} unrouted")
    if report['skipped']:
        extra.append(f"{len(report['skipped'])} skipped")
    return msg + (f" ({', '.join(extra)})" if extra else '')
