"""Unit tests for assetimport.py - the manual UI-icon import.

Run: python test/assetimport_test.py   (no framework, no server)

What matters here: an archive member can never write outside the kind
directory, files land flat (the manifest builder does not recurse, so a nested
file is invisible to the app), and a file whose kind cannot be told is reported
back rather than guessed into one.
"""
import io
import os
import shutil
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import assetimport
import paths

passed = failed = 0


def ok(cond, msg):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print('  x FAIL:', msg)


def eq(a, b, msg):
    ok(a == b, f'{msg} (got {a!r}, want {b!r})')


def zip_of(names):
    """An in-memory zip with a 1-byte file at each of `names`."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for n in names:
            zf.writestr(n, b'x')
    buf.seek(0)
    return buf


# -- basename_of / is_image ---------------------------------------------------
eq(assetimport.basename_of('equipment/ak47.svg'), 'ak47.svg', 'forward slashes')
eq(assetimport.basename_of('equipment\\ak47.svg'), 'ak47.svg', 'zips written on Windows')
eq(assetimport.basename_of('assets/equipment/'), '', 'a directory entry has no basename')
eq(assetimport.basename_of(''), '', 'empty name')
# Traversal is defused by *keeping only* the leaf, not by rejecting the name:
# '../../../app.py' yields 'app.py', which then fails the is_image() check. The
# directory part is read for routing and never joined to a path.
eq(assetimport.basename_of('../../../app.py'), 'app.py', 'traversal collapses to its leaf')
eq(assetimport.basename_of('../../../evil.png'), 'evil.png', 'an image-named traversal too')
eq(assetimport.basename_of('..'), '', 'a bare traversal token is not a filename')
eq(assetimport.basename_of('.hidden.png'), '', 'dotfiles are skipped')
eq(assetimport.basename_of('C:evil.png'), '', 'a drive-relative name is refused')
ok(assetimport.is_image('a.PNG'), 'extension test is case-insensitive')
ok(assetimport.is_image('a.svg'), 'svg is an image here')
ok(not assetimport.is_image('manifest.json'), 'the manifest is not an importable image')
ok(not assetimport.is_image('run.bat'), 'executables are not images')

# -- detect_kind --------------------------------------------------------------
eq(assetimport.detect_kind('equipment/ak47.svg'), 'equipment', 'folder name wins')
eq(assetimport.detect_kind('assets/equipment/ak47.svg'), 'equipment', 'nested under assets/')
eq(assetimport.detect_kind('static/assets/map_icons/anything.svg'), 'map_icons',
   'a deep path still finds the kind folder')
eq(assetimport.detect_kind('EQUIPMENT/ak47.svg'), 'equipment', 'folder match is case-insensitive')
eq(assetimport.detect_kind('skillgroup7.svg'), 'skillgroups', 'skillgroup<N> routes itself')
eq(assetimport.detect_kind('skillgroup.svg'), None, 'skillgroup with no number does not')
eq(assetimport.detect_kind('map_icon_de_dust2.svg'), 'map_icons', 'map_icon_ prefix')
eq(assetimport.detect_kind('de_nuke_radar_psd.png'), 'overheadmaps', '_radar_psd suffix')
eq(assetimport.detect_kind('de_nuke_radar.png'), 'overheadmaps', '_radar suffix')
eq(assetimport.detect_kind('icon_headshot.svg'), 'deathnotice', 'a known deathnotice stem')
eq(assetimport.detect_kind('premier_tier6.svg'), 'premier', 'a tinted premier banner routes by name')
eq(assetimport.detect_kind('premier_none.svg'), 'premier', 'the unrated premier banner routes by name')
eq(assetimport.detect_kind('assets/premier/premier_tier0.svg'), 'premier',
   'a zipped static/assets/premier/ tree routes by its folder')
eq(assetimport.detect_kind('premier_rating_bg_large.svg'), 'premier',
   "the game's grey source art routes to premier - the import's rebuild tints it")
eq(assetimport.detect_kind('premier_rating_bg_large_none.svg'), 'premier',
   'and so does its unrated variant')
eq(assetimport.detect_kind('premier_tierX.svg'), None, 'a non-numeric tier is not a banner')
eq(assetimport.detect_kind('penetrate.svg'), 'deathnotice', 'wallbang icon by its VPK name')
# The one that must NOT be guessed: equipment icons carry no marker.
eq(assetimport.detect_kind('ak47.svg'), None, 'a bare weapon name is unrouted, not assumed')
eq(assetimport.detect_kind('ak47.svg', 'equipment'), 'equipment', 'an explicit kind decides it')
eq(assetimport.detect_kind('equipment/ak47.svg', 'map_icons'), 'map_icons',
   'an explicit kind overrides even a folder name')
eq(assetimport.detect_kind('ak47.svg', 'not_a_kind'), None, 'an unknown explicit kind is ignored')

# -- ingest_zip: the happy path -----------------------------------------------
dest = tempfile.mkdtemp(prefix='assetimport_')
try:
    rep = assetimport.ingest_zip(
        zip_of(['assets/equipment/ak47.svg', 'assets/equipment/awp.svg',
                'assets/map_icons/map_icon_de_train.svg', 'assets/manifest.json',
                'assets/skillgroups/skillgroup1.svg']),
        dest)
    eq(rep['total'], 4, 'four images imported, manifest.json ignored')
    eq(rep['imported'], {'equipment': 2, 'map_icons': 1, 'skillgroups': 1}, 'counts per kind')
    ok(os.path.isfile(os.path.join(dest, 'equipment', 'ak47.svg')), 'file landed in its kind dir')
    ok(os.path.isfile(os.path.join(dest, 'map_icons', 'map_icon_de_train.svg')), 'map icon landed')
    eq(rep['unrouted'], [], 'a zipped assets tree routes entirely by itself')

    # Flattening: the manifest builder does not recurse, so a nested member must
    # still end up directly in the kind directory.
    rep = assetimport.ingest_zip(zip_of(['equipment/sub/dir/deagle.svg']), dest)
    ok(os.path.isfile(os.path.join(dest, 'equipment', 'deagle.svg')),
       'a nested member is flattened into the kind dir')
    ok(not os.path.exists(os.path.join(dest, 'equipment', 'sub')),
       'no subdirectory is created')
finally:
    shutil.rmtree(dest, ignore_errors=True)

# -- ingest_zip: nothing escapes the destination ------------------------------
dest = tempfile.mkdtemp(prefix='assetimport_')
outside = tempfile.mkdtemp(prefix='assetimport_out_')
try:
    # Every one of these is a zip-slip attempt. They are either skipped outright
    # or written as a plain basename inside a kind dir - never above `dest`.
    rep = assetimport.ingest_zip(
        zip_of(['../../../evil.png',
                '../../app.py',
                'equipment/../../../evil2.png',
                '/abs/evil3.png',
                'equipment/../../evil4.png']),
        dest, explicit_kind='equipment')
    before = set(os.listdir(outside))
    eq(set(os.listdir(outside)), before, 'nothing was written to an unrelated directory')
    for root, _dirs, files in os.walk(dest):
        rel = os.path.relpath(root, dest)
        ok(not rel.startswith('..'), f'everything written stayed under dest (saw {rel})')
        for f in files:
            eq(os.path.basename(f), f, 'written names are bare basenames')
    # The .py member must never be written at all - it is not an image.
    ok(not os.path.isfile(os.path.join(dest, 'equipment', 'app.py')), 'a .py member is skipped')
    ok(rep['total'] <= 4, 'at most the four image-named members could land')
finally:
    shutil.rmtree(dest, ignore_errors=True)
    shutil.rmtree(outside, ignore_errors=True)

# -- ingest_zip: unrouted files are reported, not guessed ---------------------
dest = tempfile.mkdtemp(prefix='assetimport_')
try:
    rep = assetimport.ingest_zip(zip_of(['ak47.svg', 'awp.svg']), dest)
    eq(rep['total'], 0, 'bare weapon names import nothing without a kind')
    eq(sorted(rep['unrouted']), ['ak47.svg', 'awp.svg'], 'and are named in the report')
    ok('Pick a specific kind' in assetimport.summarize(rep), 'the summary says what to do')

    rep = assetimport.ingest_zip(zip_of(['ak47.svg', 'awp.svg']), dest, explicit_kind='equipment')
    eq(rep['total'], 2, 'the same zip with a kind chosen imports both')
    ok(assetimport.summarize(rep).startswith('Imported 2 image(s)'), 'summary counts them')
finally:
    shutil.rmtree(dest, ignore_errors=True)

# -- ingest_zip: limits -------------------------------------------------------
dest = tempfile.mkdtemp(prefix='assetimport_')
try:
    too_many = zip_of([f'equipment/w{i}.svg' for i in range(assetimport.MAX_FILES + 1)])
    try:
        assetimport.ingest_zip(too_many, dest)
        ok(False, 'a zip over MAX_FILES should raise')
    except ValueError as e:
        ok('limit' in str(e), 'the file-count limit is explained')

    try:
        assetimport.ingest_zip(io.BytesIO(b'not a zip at all'), dest)
        ok(False, 'a non-zip should raise BadZipFile')
    except zipfile.BadZipFile:
        ok(True, 'a corrupt archive raises BadZipFile')
finally:
    shutil.rmtree(dest, ignore_errors=True)

# -- ingest_file --------------------------------------------------------------
dest = tempfile.mkdtemp(prefix='assetimport_')
try:
    rep = assetimport.ingest_file('map_icon_de_train.svg', b'<svg/>', dest)
    eq(rep['imported'], {'map_icons': 1}, 'a loose file routes by its name')

    rep = assetimport.ingest_file('ak47.svg', b'<svg/>', dest)
    eq(rep['unrouted'], ['ak47.svg'], 'a loose file with no marker is unrouted')

    rep = assetimport.ingest_file('notes.txt', b'hello', dest)
    eq(rep['skipped'], ['notes.txt'], 'a non-image is skipped')
    eq(rep['total'], 0, 'and imports nothing')

    rep = assetimport.ingest_file('big.png', b'x' * (assetimport.MAX_FILE_BYTES + 1), dest)
    eq(rep['total'], 0, 'an oversized file is refused')
    eq(rep['skipped'], ['big.png'], 'and reported')

    # Re-importing the same name replaces it rather than erroring.
    assetimport.ingest_file('map_icon_de_train.svg', b'<svg id="new"/>', dest)
    with open(os.path.join(dest, 'map_icons', 'map_icon_de_train.svg'), 'rb') as fh:
        eq(fh.read(), b'<svg id="new"/>', 'a repeat import overwrites')
finally:
    shutil.rmtree(dest, ignore_errors=True)

# -- every kind the app knows about is reachable ------------------------------
# A kind added to paths.ASSET_KINDS but not handled here would silently be
# un-importable, so assert the explicit route works for all of them.
dest = tempfile.mkdtemp(prefix='assetimport_')
try:
    for kind in paths.ASSET_KINDS:
        rep = assetimport.ingest_file('whatever.png', b'x', dest, explicit_kind=kind)
        eq(rep['imported'], {kind: 1}, f'{kind} accepts an explicit import')
        eq(assetimport.detect_kind(f'{kind}/whatever.png'), kind, f'{kind} routes by folder')
finally:
    shutil.rmtree(dest, ignore_errors=True)

print(f'{passed} passed, {failed} failed')
sys.exit(1 if failed else 0)
