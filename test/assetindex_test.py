"""Unit tests for assetindex.py - the Premier tint and the asset manifest.

The Python replacement for tools/make_assets_manifest.mjs and
tools/tint_premier_banner.mjs; the expected values below are the ones the Node
versions produced, so a regression shows as a changed key or colour rather than
as an icon that quietly stops appearing.

Run:  python3 test/assetindex_test.py     (no deps)
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import assetindex  # noqa: E402

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


def touch(d, *names):
    os.makedirs(d, exist_ok=True)
    for n in names:
        with open(os.path.join(d, n), "w", encoding="utf-8") as f:
            f.write("x")


def test_keys_per_kind():
    k = assetindex.derive_key
    eq(k("skillgroups", "skillgroup7.svg"), "7", "skillgroup number")
    eq(k("skillgroups", "skillgroup07.svg"), "7", "leading zero folds")
    eq(k("skillgroups", "dangerzone7.svg"), None, "dangerzone art never collides with rank 7")
    eq(k("skillgroups", "wingman7.svg"), None, "nor wingman art")
    eq(k("map_icons", "map_icon_de_dust2.svg"), "de_dust2", "map icon")
    eq(k("map_icons", "screenshot.png"), None, "other art in the map tree")
    eq(k("overheadmaps", "de_nuke_radar_psd.png"), "de_nuke", "_radar_psd")
    eq(k("overheadmaps", "de_nuke_lower_radar_psd.png"), "de_nuke_lower", "lower floor keeps _lower")
    eq(k("overheadmaps", "de_nuke_radar.png"), "de_nuke", "_radar")
    eq(k("overheadmaps", "de_nuke_psd.png"), "de_nuke", "bare _psd")
    eq(k("overheadmaps", "de_nuke.png"), "de_nuke", "bare name")
    eq(k("equipment", "weapon_ak47.svg"), "ak47", "weapon_ stripped")
    eq(k("equipment", "AK47.SVG"), "ak47", "case folds")
    eq(k("deathnotice", "penetrate.svg"), "wallbang", "deathnotice table")
    eq(k("deathnotice", "something_else.svg"), None, "unknown deathnotice")
    eq(k("premier", "premier_tier6.svg"), "6", "tinted tier")
    eq(k("premier", "premier_none.svg"), "none", "unrated banner")
    eq(k("premier", "premier_tier7.svg"), None, "no tier 7")
    eq(k("premier", "premier_rating_bg_large.svg"), None, "grey source is never a key")


def test_manifest_shape_order_and_skips():
    d = tempfile.mkdtemp()
    touch(os.path.join(d, "equipment"), "weapon_ak47.svg", "ak47.svg", "notes.txt")
    os.makedirs(os.path.join(d, "equipment", "nested"))
    touch(os.path.join(d, "equipment", "nested"), "awp.svg")
    touch(os.path.join(d, "skillgroups"), "skillgroup1.svg", "dangerzone1.svg")
    touch(os.path.join(d, "premier"), "premier_tier0.svg", assetindex.PREMIER_SOURCE)
    total, lines = assetindex.write_manifest(d, generated="T")
    with open(os.path.join(d, "manifest.json"), encoding="utf-8") as f:
        raw = f.read()
    m = json.loads(raw)
    eq(m["version"], 1, "version 1")
    eq(m["generated"], "T", "generated passes through")
    eq(sorted(m["kinds"]), sorted(assetindex.paths.ASSET_KINDS), "every kind present, even empty")
    eq(m["kinds"]["equipment"], {"ak47": "equipment/ak47.svg"},
       "sorted, first file wins the key; non-images and nested files are not indexed")
    eq(m["kinds"]["skillgroups"], {"1": "skillgroups/skillgroup1.svg"}, "dangerzone dropped")
    eq(m["kinds"]["premier"], {"0": "premier/premier_tier0.svg"}, "grey source not indexed")
    eq(total, 3, "total counts indexed files")
    ok(any("equipment: 1 file(s) skipped" in l for l in lines),
       "the losing duplicate counts as skipped")
    ok(not any("premier:" in l and "skipped" in l for l in lines),
       "the grey source is not reported as a skip")
    ok(raw.endswith("\n") and ", " not in raw and ": " not in raw, "compact JSON + newline")
    ok(not os.path.exists(os.path.join(d, "manifest.json.tmp")), "written atomically")


def test_nothing_to_index_writes_nothing():
    d = tempfile.mkdtemp()
    try:
        assetindex.write_manifest(d)
        ok(False, "an empty tree must raise")
    except assetindex.IndexError_:
        ok(True, "empty tree raises")
    ok(not os.path.exists(os.path.join(d, "manifest.json")), "and leaves no manifest")
    good, msg, _ = assetindex.finish(d)
    ok(not good and "no assets found" in msg, "finish() reports it instead of raising")


def test_wash_color_matches_the_game_multiply():
    w = assetindex.wash_color
    eq(w("#ffffff", "#ffd700"), "#ffd700", "white takes the wash exactly")
    eq(w("#000000", "#ffd700"), "#000000", "black stays black")
    eq(w("#E6E6E6", "#ffd700"), "#e6c200", "near-white chevron -> vivid gold")
    eq(w("#6B6A6A", "#ffd700"), "#6b5900", "grey plate -> deep gold")
    eq(w("#fff", "#abc"), "#aabbcc", "3-digit forms expand")
    ok(w("#6B6A6A", "#ffd700") != w("#E6E6E6", "#ffd700"), "plate and chevron stay distinct")
    eq(len(assetindex.TIER_COLORS), 7, "seven tier colours")
    try:
        w("red", "#ffffff")
        ok(False, "a non-hex colour must raise")
    except ValueError:
        ok(True, "non-hex raises")


def test_tint_svg_rewrites_colours_and_ids():
    svg = ('<svg><linearGradient id="paint0_linear_632_1563"><stop stop-color="#E6E6E6"/>'
           '</linearGradient><path fill="#6B6A6A" style="fill:url(#paint0_linear_632_1563)"/>'
           '<rect fill="#11223344"/></svg>')
    out = assetindex.tint_svg(svg, "#ffd700", "t6")
    ok('stop-color="#e6c200"' in out and 'fill="#6b5900"' in out, "hex literals washed")
    ok("paint0_linear_t6" in out and "_632_" not in out, "Valve ids re-suffixed")
    ok("#11223344" in out, "an 8-digit colour is not a 6-digit match")


def test_tint_premier_and_finish_end_to_end():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "premier")
    os.makedirs(p)
    grey = '<svg><path fill="#E6E6E6" id="clip0_632_1"/></svg>\r\n'
    for name in assetindex.PREMIER_SOURCES:
        with open(os.path.join(p, name), "w", encoding="utf-8", newline="") as f:
            f.write(grey)
    good, msg, lines = assetindex.finish(d)
    ok(good, f"finish succeeds ({msg})")
    eq(sorted(f for f in os.listdir(p) if not f.startswith("premier_rating")),
       sorted([f"premier_tier{i}.svg" for i in range(7)] + ["premier_none.svg"]),
       "seven tiers + the unrated banner")
    with open(os.path.join(p, "premier_tier6.svg"), encoding="utf-8", newline="") as f:
        t6 = f.read()
    eq(t6, '<svg><path fill="#e6c200" id="clip0_t6"/></svg>\r\n', "gold tier, id suffixed, CRLF kept")
    with open(os.path.join(d, "manifest.json"), encoding="utf-8") as f:
        prem = json.load(f)["kinds"]["premier"]
    eq(sorted(prem), sorted(["0", "1", "2", "3", "4", "5", "6", "none"]), "all eight indexed")
    ok(any("8 tinted banner(s)" in l for l in lines), "the log says what was tinted")


def test_no_grey_source_leaves_imported_banners_alone():
    d = tempfile.mkdtemp()
    touch(os.path.join(d, "premier"), "premier_tier3.svg")
    n, _ = assetindex.tint_premier(d)
    eq(n, 0, "nothing to tint without the grey source")
    with open(os.path.join(d, "premier", "premier_tier3.svg"), encoding="utf-8") as f:
        eq(f.read(), "x", "an imported tier file is not overwritten")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)
