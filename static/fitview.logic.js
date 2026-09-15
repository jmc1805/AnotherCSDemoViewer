/**
 * fitview.logic.js - how big the replayer's map may be so the whole page (hero
 * band, map row, round nav, playback controls) fits the window without a page
 * scroll. Pure: measured heights in, sizes out. No DOM; unit-tested under node
 * (test/fitview.logic.test.mjs). The glue that measures the page and writes
 * the result into CSS custom properties is static/fitview.js.
 *
 * Why a measurement and not a `calc(100vh - N)`: N is everything that is not
 * the map, and it is not a constant - the round nav wraps onto a second line
 * in overtime, the hero band hides until the match loads and changes height
 * at each responsive tier. A hardcoded N was the old guard here, and it was
 * wrong in the direction that matters: `max(520px, 100vh - 200px)` left the
 * playback controls entirely below the fold on a Surface (1368x830).
 */
(function (root, factory) {
    if (typeof module === 'object' && module.exports) module.exports = factory();
    else root.FitViewLogic = factory();
}(typeof self !== 'undefined' ? self : this, function () {
    'use strict';

    // slack: a few px so sub-pixel rounding never tips the page into a
    // one-pixel scrollbar.
    const DEFAULTS = { minSide: 0, maxSide: Infinity, slack: 8 };

    /**
     * m.viewportH  window height
     * m.rowTop     document y of the map row's top edge (all that sits above it)
     * m.belowH     height of what must stay visible under the row, margins
     *              included (round nav + controls) - measured from the row's
     *              bottom edge, so it does not depend on the row's own height
     * m.chromeV/H  map column height/width minus the canvas's: whatever the
     *              column draws around the square canvas
     * opts         { minSide, maxSide, slack } in px
     * → { side, colMax, rowH, fits } or null for an unusable measurement.
     *   side    canvas edge length
     *   colMax  max-width for the map column that yields that canvas
     *   rowH    height the side columns may use - never shorter than the map,
     *           so a window too short to fit shows the map whole and scrolls
     *   fits    false when minSide won, i.e. the page will scroll anyway
     */
    function compute(m, opts) {
        const o = Object.assign({}, DEFAULTS, opts || {});
        if (!m || !(m.viewportH > 0)) return null;
        const chromeV = m.chromeV > 0 ? m.chromeV : 0;
        const chromeH = m.chromeH > 0 ? m.chromeH : 0;
        const avail = Math.floor(m.viewportH - (m.rowTop || 0) - (m.belowH || 0) - o.slack);
        const want = avail - chromeV;
        const side = Math.max(o.minSide, Math.min(o.maxSide, want));
        return {
            side,
            colMax: side + chromeH,
            rowH: Math.max(avail, side + chromeV),
            fits: want >= o.minSide,
        };
    }

    return { compute, DEFAULTS };
}));
