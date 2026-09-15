/**
 * maplayers.logic.js - which floor of a multi-level map a position is on.
 *
 * Nuke and Vertigo are two maps stacked on top of each other, and a flat radar
 * cannot show both: on Nuke the whole of Ramp/Secret/Vents/B site sits directly
 * underneath A site, so every player down there was being drawn on top of the
 * players above them, in the same colours, with no way to tell which was which.
 * Valve ships a second radar image for exactly this
 * (`de_nuke_lower_radar_psd.png`, `de_vertigo_lower_…`, `de_train_lower_…` -
 * extracted with the others into static/assets/overheadmaps/),
 * keyed off a single altitude threshold per map. How much the two images
 * differ varies: Nuke's are two different halves of the map, Train's differ
 * only where the raised middle platform hides what is under it.
 *
 * The thresholds are map constants, not something to derive per match: they
 * come from the map's own radar definition (the `verticalsections` block in
 * `<map>.txt` inside the VPK, which is what the game itself uses to decide
 * which overview to draw). de_nuke's is corroborated by this repo's own data -
 * the Z histogram of every kill and shot in the corpus's Nuke match is clearly
 * bimodal with its gap at −512..−448 and only 10 of 3138 samples inside it.
 *
 * Pure: no DOM, no canvas. Unit-tested by test/maplayers.logic.test.mjs.
 */
(function (root, factory) {
    if (typeof module === 'object' && module.exports) module.exports = factory(require('./maps.logic.js'));
    else root.MapLayersLogic = factory(root.MapsLogic);
}(typeof self !== 'undefined' ? self : this, function (MapsLogic) {
    'use strict';

    // Canonical map name → { split }. `split` is the world Z at or above which
    // a position is on the UPPER floor.
    //
    // Every value is Valve's, read out of the `verticalsections` block of
    // `resource/overviews/<map>.txt` in pak01_dir.vpk (the "lower" section's
    // AltitudeMax) - the same file the calibration in match2d.logic.js comes
    // from, pulled by tools/extract_map_calibration.mjs. Only maps that
    // declare a `verticalsections` block are listed; a map with a
    // `<map>_lower_radar_psd.png` radar on disk but no block is single-level as far as
    // the game is concerned, and drawing a second floor for it would cut
    // players onto a level that does not exist.
    //
    // Do NOT derive a split from a Z histogram. de_train is why: its histogram
    // has an obvious trough around −290, and −290 is wrong. Valve's threshold
    // is −50, which puts ~92% of play on the lower radar and only the raised
    // middle platform above - and it is the low 8% that genuinely stacks (40%
    // of the upper footprint's cells sit directly over lower ones). A trough
    // in a Z histogram is mostly a map's tallest staircase, not its floor
    // boundary.
    const LAYERS = {
        de_nuke:    { split: -495 },
        de_vertigo: { split: 11700 },
        de_train:   { split: -50 },
    };

    const UPPER = 'upper';
    const LOWER = 'lower';
    const MODES = ['auto', UPPER, LOWER];

    /** Does this map have a second floor we know how to split? */
    function isLayered(rawName) {
        return Object.prototype.hasOwnProperty.call(LAYERS, MapsLogic.canonical(rawName));
    }

    function configFor(rawName) {
        return LAYERS[MapsLogic.canonical(rawName)] || null;
    }

    /**
     * Which floor a world Z is on: 'upper' | 'lower', or null when the map is
     * single-level or the position has no Z.
     *
     * A missing Z is null rather than 'upper': matches parsed before the
     * parser emitted Z carry none, and defaulting them onto one floor would
     * silently claim every player on an old Nuke demo was on A site. Callers
     * treat null as "floor unknown" and draw the player normally.
     */
    function layerOf(rawName, z) {
        const cfg = configFor(rawName);
        if (!cfg) return null;
        if (typeof z !== 'number' || !isFinite(z)) return null;
        return z >= cfg.split ? UPPER : LOWER;
    }

    /**
     * The floor most of `players` are on - the automatic mode's answer.
     *
     * Only players with a known floor vote, and (when the caller marks them)
     * only living ones: a corpse lying in Vents is not a reason to follow the
     * action down there. A tie keeps `prev` if it was one of the two, so the
     * view doesn't flip back and forth on every frame of a 5-5 split; with no
     * previous floor a tie resolves to upper, which is where the radar's
     * default image already points.
     */
    function autoLayer(rawName, players, prev) {
        if (!configFor(rawName)) return null;
        let up = 0, down = 0;
        (players || []).forEach(p => {
            if (!p) return;
            if (p.health != null && p.health <= 0) return;
            const l = layerOf(rawName, p.Z != null ? p.Z : p.z);
            if (l === UPPER) up++;
            else if (l === LOWER) down++;
        });
        if (up === 0 && down === 0) return prev === LOWER ? LOWER : UPPER;
        if (up === down) return (prev === UPPER || prev === LOWER) ? prev : UPPER;
        return up > down ? UPPER : LOWER;
    }

    /**
     * Resolve a UI mode to the floor actually being shown.
     *   mode 'auto'  → autoLayer()
     *   mode 'upper' / 'lower' → itself
     * Returns null for a single-level map, which is the caller's signal to draw
     * nothing layer-specific at all.
     */
    function resolve(rawName, mode, players, prev) {
        if (!configFor(rawName)) return null;
        if (mode === UPPER || mode === LOWER) return mode;
        return autoLayer(rawName, players, prev);
    }

    /**
     * Opacity multiplier for one player given the floor on show.
     *
     * Off-floor players are dimmed rather than hidden: they are still on the
     * map, still alive, and still about to matter - hiding them would make the
     * replay lie. Unknown floor (no Z) is drawn at full strength, since the
     * alternative is dimming everyone on an old demo.
     */
    function alphaFor(rawName, z, shownLayer, dimmed) {
        if (!shownLayer) return 1;
        const l = layerOf(rawName, z);
        if (l === null || l === shownLayer) return 1;
        return dimmed === undefined ? 0.28 : dimmed;
    }

    /** Radar image name for a floor: the base map, or `<map>_lower`. */
    function radarName(rawName, shownLayer) {
        const canon = MapsLogic.canonical(rawName);
        return shownLayer === LOWER ? canon + '_lower' : canon;
    }

    return { LAYERS, MODES, UPPER, LOWER, isLayered, configFor, layerOf,
             autoLayer, resolve, alphaFor, radarName };
}));
