/**
 * match2d.logic.js - pure constants/math shared by the three pages that draw
 * or reason about the 2D map: the replay viewer (viewer.js), the multi-round
 * analyser (multi.js), and the Rounds & Kills tab (templates/match.html).
 * No DOM, no canvas: unit-testable under node (test/match2d.logic.test.mjs).
 * Canvas glue (functions that touch a ctx) lives in static/match2d.js.
 *
 * Anything a second page would otherwise re-derive belongs here: radar
 * calibration, tick-rate and round-length constants, the world→canvas
 * projection, and the round-end reason lookups. Three pages drawing the same
 * map from three copies of these numbers is three chances to disagree about
 * where a player was standing.
 *
 * Every function here is total and side-effect free.
 */
(function (root, factory) {
    if (typeof module === 'object' && module.exports) module.exports = factory(require('./maps.logic.js'));
    else root.Match2DLogic = factory(root.MapsLogic);
}(typeof self !== 'undefined' ? self : this, function (MapsLogic) {
    'use strict';

    // Radar calibration for a 1024×1024 canvas - one entry per supported map,
    // keyed by canonical map name (maps.logic.js), never by a raw one.
    //
    // These are Valve's own numbers, not hand-tuned ones: `pos_x`/`pos_y`/
    // `scale` out of `resource/overviews/<map>.txt` inside pak01_dir.vpk, the
    // file the game itself projects the radar with. `tools/extract_map_calibration.mjs`
    // pulls them and diffs them against this table - run it after a map update
    // rather than nudging a number until the dots look right.
    const MAP_LIBRARY = {
        "de_dust2":    { scale: 4.4,  x: -2476, y: 3239 },
        "de_mirage":   { scale: 5.0,  x: -3230, y: 1713 },
        "de_inferno":  { scale: 4.9,  x: -2087, y: 3870 },
        "de_anubis":   { scale: 5.22, x: -2796, y: 3328 },
        "de_ancient":  { scale: 5.0,  x: -2953, y: 2164 },
        "de_nuke":     { scale: 7.0,  x: -3453, y: 2887 },
        "de_vertigo":  { scale: 4.0,  x: -3168, y: 1762 },
        "de_overpass": { scale: 5.2,  x: -4831, y: 1781 },
        "de_cache":    { scale: 5.5,  x: -2000, y: 3250 },
        "de_train":    { scale: 4.082077, x: -2308, y: 2078 },
    };

    // Fallback calibration for a map with no MAP_LIBRARY entry. Every world
    // position still projects somewhere plausible on the radar rather than
    // piling into a corner, which is what a null config would do.
    const DEFAULT_MAP_CONFIG = MAP_LIBRARY['de_dust2'];

    // Radar calibration for a map given the raw name a match document carries.
    // Canonicalising first is what lets a match stored under a legacy spelling
    // find its entry instead of silently taking the default (see
    // maps.logic.js). `onMissing` is called with the canonical name when the
    // map has no entry, so a caller can warn without repeating the lookup.
    function mapConfigFor(rawName, onMissing) {
        const canon = MapsLogic.canonical(rawName);
        const cfg = MAP_LIBRARY[canon];
        if (!cfg && typeof onMissing === 'function') onMissing(canon);
        return cfg || DEFAULT_MAP_CONFIG;
    }

    const TICKRATE             = 64;
    const REGULATION_HALF      = 12;   // rounds per regulation half
    const REGULATION_ROUNDS    = 24;   // total regulation rounds
    const OT_HALF_LEN          = 3;    // rounds per OT half (MR3)

    const SPRAY_DURATION_TICKS = 6;
    const HE_FADE_TICKS        = 48;
    const PLAYER_TRAIL_TICKS   = 96;   // ~1.5s player-trail window

    // Footstep audibility (Display toggle): CS2's global silent-movement cutoff is
    // ~135 world units/sec - below it, movement makes no sound. Above it, a static
    // circle scales with speed toward a full ~250-velocity sprint (RUN_SPEED_REF).
    // Radii are true to the real audible range (confirmed in-game on de_dust2: a
    // player running short/catwalk corner is audible from top-mid barrels, ~1400+
    // units away) rather than shrunk for screen legibility. FADE_MARGIN softens
    // the on/off transition at the threshold into a fade instead of an instant pop.
    const AUDIBLE_SPEED        = 135;
    const RUN_SPEED_REF        = 250;
    const FOOTSTEP_RADIUS_MIN  = 800;
    const FOOTSTEP_RADIUS_MAX  = 900;
    const FOOTSTEP_FADE_MARGIN = 20;   // u/s band around AUDIBLE_SPEED that fades in/out

    // Win-condition icon/label for a round's `reason` field (cmd/parser/main.go
    // roundEndReason): bomb_exploded, bomb_defused, t_killed (CT won by
    // elimination), ct_killed (T won by elimination), time_ran_out.
    //
    // Icons use the real equipment art (static/assets/equipment/) when the VPK
    // pipeline has been run - c4 for exploded, defuser for defused - same §1
    // deletion-test gate as the sidebar loadout icons. "Elimination" has no CS2
    // economy item to extract, so it's a small hand-drawn skull glyph in the same
    // flat white-on-transparent style, built from primitives (not a traced path)
    // so it stays crisp at ~13px. Without static/assets/ present, everything
    // falls back to the original emoji - never a broken image.
    const REASON_EQUIP_KEY = { bomb_exploded: 'c4', bomb_defused: 'defuser' };
    const REASON_EMOJI = { bomb_exploded: '💥', bomb_defused: '✂️', t_killed: '💀', ct_killed: '💀', time_ran_out: '⏱️' };
    const SKULL_ICON_SRC = 'data:image/svg+xml;utf8,' + encodeURIComponent(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">' +
        '<mask id="e"><rect width="32" height="32" fill="white"/>' +
        '<circle cx="12" cy="14" r="3" fill="black"/><circle cx="20" cy="14" r="3" fill="black"/>' +
        '<rect x="14.5" y="17" width="3" height="4" fill="black"/></mask>' +
        '<g mask="url(#e)" fill="white"><circle cx="16" cy="13" r="10"/><rect x="8" y="20" width="16" height="9" rx="3"/></g>' +
        '<g stroke="white" stroke-width="1.4"><line x1="12" y1="24" x2="12" y2="28"/><line x1="16" y1="24" x2="16" y2="28"/><line x1="20" y1="24" x2="20" y2="28"/></g>' +
        '</svg>'
    );

    function easeOut(t) { return 1 - Math.pow(1 - t, 2); }

    // Base-canvas-px position of a world coordinate for a map's calibration
    // (a MAP_LIBRARY entry). Base space = the un-zoomed 1024² image; callers
    // apply pan/zoom (static/viewport.js) on top of this.
    function worldToCanvas(mapConfig, wx, wy) {
        return {
            x: (wx - mapConfig.x) / mapConfig.scale,
            y: (mapConfig.y - wy) / mapConfig.scale,
        };
    }

    // Does first-half-CT occupy the T side in round `rNum`? Regulation-only
    // shape (alternates every OT_HALF_LEN rounds once OT starts, assuming OT1
    // continues the regulation second-half sides) - used by multi.js and
    // match.html, which don't have per-match tick data to detect the real OT
    // starting side. viewer.js has that data and does its own live detection
    // (see isSwappedAtRound there), so it does not use this helper.
    function isSwappedAtRoundSimple(rNum) {
        if (rNum <= REGULATION_HALF)   return false;
        if (rNum <= REGULATION_ROUNDS) return true;
        const otHalfIdx = Math.floor((rNum - REGULATION_ROUNDS - 1) / OT_HALF_LEN);
        return otHalfIdx % 2 === 0;
    }

    // `assets` is the window.Assets module ({available(), url(kind,key)}) or
    // null/undefined - passed in rather than read off a global so this stays
    // testable under node (same convention as assets.logic.js).
    function roundReasonIcon(reason, assets) {
        const r = (reason || '').toLowerCase();
        const emoji = REASON_EMOJI[r];
        if (!emoji) return '';
        if (assets && assets.available && assets.available()) {
            const equipKey = REASON_EQUIP_KEY[r];
            const src = equipKey ? assets.url('equipment', equipKey)
                      : (r === 't_killed' || r === 'ct_killed') ? (assets.url('deathnotice', 'suicide') || SKULL_ICON_SRC) : null;
            if (src) return `<img class="rreason-ico" src="${src}" alt="${emoji}">`;
        }
        return emoji;
    }

    function roundReasonLabel(reason) {
        switch ((reason || '').toLowerCase()) {
            case 'bomb_exploded': return 'Bomb exploded';
            case 'bomb_defused':  return 'Bomb defused';
            case 't_killed':      return 'T eliminated';
            case 'ct_killed':     return 'CT eliminated';
            case 'time_ran_out':  return 'Time ran out';
            default:               return '';
        }
    }

    return {
        MAP_LIBRARY, DEFAULT_MAP_CONFIG, mapConfigFor,
        TICKRATE, REGULATION_HALF, REGULATION_ROUNDS, OT_HALF_LEN,
        SPRAY_DURATION_TICKS, HE_FADE_TICKS, PLAYER_TRAIL_TICKS,
        AUDIBLE_SPEED, RUN_SPEED_REF, FOOTSTEP_RADIUS_MIN, FOOTSTEP_RADIUS_MAX, FOOTSTEP_FADE_MARGIN,
        REASON_EQUIP_KEY, REASON_EMOJI, SKULL_ICON_SRC,
        easeOut, worldToCanvas, isSwappedAtRoundSimple, roundReasonIcon, roundReasonLabel,
    };
}));
