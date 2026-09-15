/**
 * assets.logic.js - pure resolution logic for the CS2 UI image assets.
 * No DOM, no fetch: everything here is
 * unit-testable under node (test/assets.logic.test.mjs). The browser glue
 * (fetch + <img>/Image cache + graceful fallback) lives in static/assets.js.
 *
 * The manifest shape (produced by assetindex.py):
 *   { version, generated, kinds: { <kind>: { <logicalKey>: <relativePath> } } }
 * where kind ∈ skillgroups | map_icons | overheadmaps | equipment | deathnotice
 * | premier.
 *
 * Every function is total and side-effect free; an absent kind/key resolves to
 * null so callers can fall back to today's text label / CSS chip.
 */
(function (root, factory) {
    if (typeof module === 'object' && module.exports) module.exports = factory(require('./maps.logic.js'));
    else root.AssetsLogic = factory(root.MapsLogic);
}(typeof self !== 'undefined' ? self : this, function (MapsLogic) {
    'use strict';

    // Weapon aliases: viewer keys that map onto a single shared icon file.
    const WEAPON_ALIASES = {
        weapon_usps: 'weapon_usp_silencer',
        weapon_p2000: 'weapon_hkp2000',
        weapon_incgrenade: 'weapon_molotov',   // inc grenade shares the molotov icon
        weapon_m4a1: 'weapon_m4a1_silencer',    // fallback if only one M4 icon exists
        // The M4A4's econ item is `weapon_m4a1`, so the VPK ships its icon as
        // `m4a1` - but the parser emits `weapon_m4a4`. Map it to the m4a1 icon.
        weapon_m4a4: 'weapon_m4a1',
        // Dual Berettas' econ item name is `weapon_elite` - the VPK ships the
        // icon as `elite`, but the parser emits `weapon_dual_berettas` (derived
        // from demoinfocs' human-readable equipment name).
        weapon_dual_berettas: 'weapon_elite',
        // A dropped defuse kit. The parser has to synthesise this key
        // (cmd/parser/main.go: a CS2 demo carries no entity for a kit on the
        // ground, only the pawn's m_bHasDefuser flag), and it has no
        // `weapon_` prefix to strip, so it needs its own mapping onto the
        // VPK's `defuser` icon.
        item_defuser: 'defuser',
    };

    // Normalize a raw caller key into the candidate keys to try against a kind's
    // manifest, most-specific first. Returns an array (never empty).
    function candidates(kind, rawKey) {
        if (rawKey == null) return [];
        let k = String(rawKey).trim().toLowerCase();
        if (!k) return [];
        const out = [];
        const push = (v) => { if (v && out.indexOf(v) < 0) out.push(v); };

        if (kind === 'equipment') {
            const bare = k.replace(/^weapon_/, '');
            push(k);                       // e.g. weapon_ak47
            push(bare);                    // e.g. ak47
            if (WEAPON_ALIASES[k]) {
                push(WEAPON_ALIASES[k]);
                push(WEAPON_ALIASES[k].replace(/^weapon_/, ''));
            }
        } else if (kind === 'map_icons' || kind === 'overheadmaps') {
            // Canonical name first, raw name only as a fallback. The order is
            // load-bearing: the VPK ships art for both de_dust and de_dust2,
            // which are different maps, so a match stored under the legacy
            // `de_dust` spelling would otherwise resolve to Dust's icon
            // instead of Dust II's. See maps.logic.js for the alias itself.
            push(MapsLogic.canonical(k));
            push(k);
        } else if (kind === 'premier') {
            // premierBannerKey() has already banded the rating, so the key is
            // canonical: "0".."6" or "none". Nothing to normalise.
            push(k);
        } else if (kind === 'skillgroups') {
            // Callers pass the raw skill-group integer (Competitive/Wingman rank);
            // try it as-is and zero-padded, tolerating either filename scheme.
            push(k);
            if (/^\d+$/.test(k)) push(String(parseInt(k, 10)));
        } else {
            push(k);
        }
        return out;
    }

    // Resolve (kind, rawKey) → relative path from a manifest object, or null.
    function resolve(manifest, kind, rawKey) {
        if (!manifest || !manifest.kinds) return null;
        const table = manifest.kinds[kind];
        if (!table) return null;
        for (const c of candidates(kind, rawKey)) {
            if (Object.prototype.hasOwnProperty.call(table, c)) return table[c];
        }
        return null;
    }

    // Which skill-group icon key a parser `ranks[]` entry maps to, or null when
    // the rank has no per-value icon (Premier is a rating number, not a group;
    // rank 0 / unranked has no badge). Mirrors formatRank() in match.html.
    //   rank_type: 11 = Premier, 12 = Competitive, 7 = Wingman.
    function skillGroupKey(rankEntry) {
        if (!rankEntry || !rankEntry.rank) return null;
        if (rankEntry.rank_type === 12 || rankEntry.rank_type === 7) {
            return String(rankEntry.rank);
        }
        return null;   // Premier (11) and anything else → no icon
    }

    // Which Premier tier a CS Rating falls in, 0-6. Transcribed from
    // GetClampedRating() in the game's panorama/scripts/rating_emblem.vts_c:
    //   Math.max(0, Math.min(Math.floor(rating / 1000.00 / 5), 6))
    // i.e. one band per 5000 rating, everything from 30000 up sharing tier 6.
    // MatchHeroLogic.premierTier() picks the same bands by another spelling and
    // the two must not drift - test/assets.logic.test.mjs asserts they agree.
    function premierTier(rating) {
        const r = Number(rating);
        if (!isFinite(r)) return 0;
        return Math.max(0, Math.min(Math.floor(r / 1000.0 / 5), 6));
    }

    // The premier-kind manifest key for a CS Rating: the tier band, or 'none'
    // for a player with no rating (the game draws a distinct "- - -" banner for
    // that, rather than a tier-0 one with an empty number).
    function premierBannerKey(rating) {
        const r = Number(rating);
        if (!rating || !isFinite(r) || r <= 0) return 'none';
        return String(premierTier(r));
    }

    return { candidates, resolve, skillGroupKey, premierTier, premierBannerKey, WEAPON_ALIASES };
}));
