/**
 * maps.logic.js - the single source of truth for map names in the browser.
 *
 * Three different names exist for one map and they must not be confused:
 *
 *   raw        what a match document actually carries, e.g. `de_dust` on a
 *              match parsed before the header scan was fixed, `de_dust2`
 *              after.
 *   canonical  the one name the app groups, filters and looks assets up by.
 *              `canonical()` folds every legacy spelling onto it.
 *   label      what a human reads: "Dust 2", "Office", "Baggage".
 *
 * Mirrors maps.py, which does the same job server-side; the alias and label
 * tables are duplicated there on purpose (the browser cannot import Python),
 * so a change here needs the same change there. test/maps.logic.test.mjs and
 * test/maps_test.py assert the two agree.
 *
 * No DOM: unit-testable under node. Every function is total and side-effect
 * free - an empty or unknown name comes back usable rather than throwing.
 */
(function (root, factory) {
    if (typeof module === 'object' && module.exports) module.exports = factory();
    else root.MapsLogic = factory();
}(typeof self !== 'undefined' ? self : this, function () {
    'use strict';

    // Legacy raw names → canonical name.
    //
    // Only `de_dust` is aliased, and only because of a header-scan bug that
    // shipped in earlier versions: the map name in a CS2 demo header is a
    // length-prefixed protobuf string, and the framing byte that follows it is
    // the character '2', so Dust II scanned as "de_dust22" and got its
    // trailing digits trimmed down to "de_dust". Matches parsed by those
    // versions are still on disk under that name, and de_dust is a real (if
    // long-retired) map with its own radar and icon, so nothing downstream may
    // guess: this table is the one place that says a stored `de_dust` means
    // Dust II.
    const MAP_ALIASES = { de_dust: 'de_dust2' };

    // Canonical name → display label, for the maps whose label is not simply
    // the name with its prefix stripped and title-cased (see `label`).
    const MAP_LABELS = { de_dust2: 'Dust 2' };

    const MAP_PREFIXES = ['de_', 'cs_', 'ar_'];

    // Radar PNGs live in static/assets/overheadmaps/ under Valve's export
    // names (`de_mirage_radar_psd.png`). The /radar/<name>.png route maps a
    // plain canonical name onto that file (maps.radar_filenames), so the
    // client never builds a path into the asset folder itself.
    const RADAR_ROUTE = '/radar/';

    function canonical(name) {
        const key = String(name == null ? '' : name).trim().toLowerCase();
        return Object.prototype.hasOwnProperty.call(MAP_ALIASES, key)
            ? MAP_ALIASES[key] : key;
    }

    // Every raw spelling that canonicalises to `name`, most-canonical first -
    // for looking something up under whichever name it was stored as.
    function aliasesOf(name) {
        const canon = canonical(name);
        const out = [canon];
        Object.keys(MAP_ALIASES).forEach(raw => {
            if (MAP_ALIASES[raw] === canon && raw !== canon) out.push(raw);
        });
        return out;
    }

    // Human-readable map name: 'de_dust' and 'de_dust2' both → 'Dust 2'.
    // Falls back to the name with its prefix stripped, underscores turned into
    // spaces and each word capitalised, which is already right for every map
    // except the ones in MAP_LABELS.
    function label(name) {
        const canon = canonical(name);
        if (!canon) return '';
        if (Object.prototype.hasOwnProperty.call(MAP_LABELS, canon)) return MAP_LABELS[canon];
        let bare = canon;
        for (const p of MAP_PREFIXES) {
            if (bare.indexOf(p) === 0) { bare = bare.slice(p.length); break; }
        }
        const words = bare.split('_').filter(Boolean)
            .map(w => w.charAt(0).toUpperCase() + w.slice(1));
        return words.join(' ') || canon;
    }

    // Uppercase label for the hero band, which sets map names in caps.
    function labelUpper(name) { return label(name).toUpperCase(); }

    // URL of a map's radar overview PNG. Alias-aware via the /radar/ route, so
    // callers never have to know which spelling the file on disk uses.
    function radarUrl(name) {
        return RADAR_ROUTE + encodeURIComponent(canonical(name)) + '.png';
    }

    return { canonical, aliasesOf, label, labelUpper, radarUrl,
             MAP_ALIASES, MAP_LABELS, MAP_PREFIXES, RADAR_ROUTE };
}));
