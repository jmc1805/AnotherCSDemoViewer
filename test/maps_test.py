"""Unit tests for maps.py - the server-side map-name source of truth.

Covers the three names one map has (raw / canonical / label) and the legacy
`de_dust` -> `de_dust2` alias that keeps matches parsed by earlier versions
grouping and rendering as Dust II.

Run:  python3 test/maps_test.py     (no deps)

The browser twin static/maps.logic.js is tested by test/maps.logic.test.mjs,
which also asserts the two carry the same alias and label tables.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import maps  # noqa: E402

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


def test_canonical_folds_the_legacy_alias():
    eq(maps.canonical("de_dust"), "de_dust2", "legacy de_dust folds onto de_dust2")
    eq(maps.canonical("de_dust2"), "de_dust2", "canonical name is a fixed point")
    eq(maps.canonical("DE_DUST"), "de_dust2", "canonical is case-insensitive")
    eq(maps.canonical("  de_dust  "), "de_dust2", "canonical trims")


def test_canonical_passes_unknown_maps_through():
    eq(maps.canonical("de_mirage"), "de_mirage", "unaliased map passes through")
    eq(maps.canonical("de_somecommunitymap"), "de_somecommunitymap",
       "unknown map passes through so it still groups with itself")


def test_canonical_is_total():
    eq(maps.canonical(None), "", "None never raises")
    eq(maps.canonical(""), "", "empty stays empty")


def test_aliases_of_lists_every_stored_spelling():
    eq(maps.aliases_of("de_dust2"), ["de_dust2", "de_dust"],
       "dust2 knows the spelling older matches were stored under")
    eq(maps.aliases_of("de_dust"), ["de_dust2", "de_dust"],
       "asking by the legacy name gives the same list")
    eq(maps.aliases_of("de_mirage"), ["de_mirage"], "unaliased map is alone in its list")


def test_label_reads_as_a_human_would_say_it():
    eq(maps.label("de_dust2"), "Dust 2", 'dust2 is spelled "Dust 2", not "Dust2"')
    eq(maps.label("de_dust"), "Dust 2", "a legacy-named match reads as Dust 2 too")
    eq(maps.label("de_mirage"), "Mirage", "prefix stripped and capitalised")
    eq(maps.label("cs_office"), "Office", "cs_ prefix stripped")
    eq(maps.label("ar_baggage"), "Baggage", "ar_ prefix stripped")
    eq(maps.label("de_train_night"), "Train Night", "underscores become spaces")
    eq(maps.label("workshop_thing"), "Workshop Thing", "unknown prefix kept as a word")


def test_radar_filenames_prefer_valves_export_names():
    eq(maps.radar_filenames("de_mirage"),
       ["de_mirage_radar_psd.png", "de_mirage_radar.png",
        "de_mirage_radar_tga.png", "de_mirage.png"],
       "extracted _radar_psd first, hand-copied spellings after")
    eq(maps.radar_filenames("de_nuke_lower")[0], "de_nuke_lower_radar_psd.png",
       "the lower-floor radar resolves the same way")


def test_radar_filenames_never_serve_the_retired_dust():
    names = maps.radar_filenames("de_dust")
    eq(names[0], "de_dust2_radar_psd.png", "a legacy-named match asks for Dust II's radar")
    ok(not any(n.startswith("de_dust_") or n == "de_dust.png" for n in names),
       "de_dust_radar_psd.png is the retired map's own radar and must never be tried")
    eq(maps.radar_filenames(""), [], "no name, no candidates")


def test_label_is_total():
    eq(maps.label(None), "", "None never raises")
    eq(maps.label(""), "", "empty stays empty")


for _fn in list(globals().values()):
    if callable(_fn) and getattr(_fn, "__name__", "").startswith("test_"):
        _fn()

print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
