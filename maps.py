"""maps.py - the single source of truth for map names.

Three different names exist for one map and they must not be confused:

  raw        what a match file/document actually carries, e.g. `de_dust` on a
             match parsed before the header scan was fixed, `de_dust2` after.
  canonical  the one name the app groups, filters and looks assets up by.
             `canonical()` folds every legacy spelling onto it.
  label      what a human reads: "Dust 2", "Office", "Baggage".

Mirrored in the browser by static/maps.logic.js - the alias and label tables
are duplicated there on purpose (the client has no way to import this module),
so a change here needs the same change there. test/maps_test.py and
test/maps.logic.test.mjs assert the two agree.
"""

# Legacy raw names → canonical name.
#
# Only `de_dust` is aliased, and only because of a header-scan bug that shipped
# in earlier versions: the map name in a CS2 demo header is a length-prefixed
# protobuf string, and the framing byte that follows it is the character '2',
# so Dust II scanned as "de_dust22" and got its trailing digits trimmed down to
# "de_dust". Matches parsed by those versions are still on disk under that
# name, and de_dust is a real (if long-retired) map with its own radar and
# icon, so nothing downstream may guess: this table is the one place that says
# a stored `de_dust` means Dust II. cmd/parser/main.go's parseMapName no longer
# produces it.
MAP_ALIASES = {
    'de_dust': 'de_dust2',
}

# Canonical name → display label, for the maps whose label is not simply the
# name with its prefix stripped and title-cased (see `label()`).
MAP_LABELS = {
    'de_dust2': 'Dust 2',
}

MAP_PREFIXES = ('de_', 'cs_', 'ar_')


def canonical(name):
    """Fold a raw map name onto the one name the app groups and filters by.

    Total: an empty or unknown name comes back unchanged (lowercased), so an
    unrecognised community map still groups consistently with itself.
    """
    key = (name or '').strip().lower()
    return MAP_ALIASES.get(key, key)


def aliases_of(name):
    """Every raw spelling that canonicalises to `name`, most-canonical first.

    Used where something has to be found on disk under whichever spelling was
    current when it was written.
    """
    canon = canonical(name)
    out = [canon]
    out += [raw for raw, target in MAP_ALIASES.items()
            if target == canon and raw != canon]
    return out


# Radar overview PNGs live in the extracted `overheadmaps` assets
# (paths.MAP_IMG_DIR) under Valve's own export names: tools/extract_ui_assets.*
# writes `de_mirage_radar_psd.png`, `de_nuke_lower_radar_psd.png`, and a few maps
# also ship a `_radar_tga` twin. `_radar` and a bare `<map>.png` cover files
# copied in by hand. First match wins.
RADAR_FILE_SUFFIXES = ('_radar_psd', '_radar', '_radar_tga', '')


def radar_filenames(name):
    """Candidate filenames for a map's radar PNG, in lookup order.

    Only the CANONICAL name is tried, never its aliases: these files carry
    Valve's names, and `de_dust_radar_psd.png` is the retired de_dust's real
    radar, not Dust II's - serving it for a `de_dust2` match would be the dust
    bug again. `name` may carry a floor suffix (`de_nuke_lower`).
    """
    canon = canonical(name)
    return [canon + suffix + '.png' for suffix in RADAR_FILE_SUFFIXES] if canon else []


def label(name):
    """Human-readable map name: 'de_dust' and 'de_dust2' → 'Dust 2'.

    Falls back to the name with its prefix stripped, underscores turned into
    spaces and each word capitalised, which is already right for every map
    except the ones in MAP_LABELS.
    """
    canon = canonical(name)
    if not canon:
        return ''
    if canon in MAP_LABELS:
        return MAP_LABELS[canon]
    bare = canon
    for prefix in MAP_PREFIXES:
        if bare.startswith(prefix):
            bare = bare[len(prefix):]
            break
    return ' '.join(w.capitalize() for w in bare.split('_') if w) or canon
