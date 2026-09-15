/**
 * momentheat.logic.js - pure binning + colour ramp for the analyser's result
 * heatmap (drawn by static/momentheat.js).
 *
 * The analyser's rows already carry the subject's own position: a `kill` row
 * carries the attacker's, a `death` row the victim's. So "where do I take my
 * opening duels" and "where do I die on eco rounds" are questions the query
 * engine can already answer - they were just being answered as a *list*, when
 * the answer is a place on the map.
 *
 * This half is deliberately pure (no canvas, no DOM) so the parts that are
 * easy to get quietly wrong - which points land in which cell, what a cell's
 * intensity means, whether a single outlier can paint itself as a hotspot -
 * are unit-tested under node (test/momentheat.logic.test.mjs) instead of
 * squinted at on a radar image.
 *
 * Two decisions worth stating:
 *
 *  - **Intensity is sqrt-scaled, not linear.** One cell with 40 kills next to
 *    twenty cells with 2 is the normal shape of this data; linear scaling
 *    renders the twenty as blank map. The gamma is a parameter so a caller can
 *    ask for the honest linear version.
 *  - **The maximum is a *count*, and it is returned.** A heatmap without a
 *    scale is a picture, not a measurement - the legend needs to be able to
 *    say "darkest = 12 kills".
 */
(function (root, factory) {
    if (typeof module === 'object' && module.exports) module.exports = factory();
    else root.MomentHeatLogic = factory();
}(typeof self !== 'undefined' ? self : this, function () {
    'use strict';

    const DEFAULT_CELL = 26;      // base-canvas px; ~2.5 m on a 1024² radar
    const DEFAULT_GAMMA = 0.5;    // sqrt

    /**
     * Bin canvas-space points into a square grid.
     *
     * `points` are already projected (the caller owns the world→canvas
     * transform). Points outside the canvas are dropped and counted, never
     * clamped to the edge - a clamped point invents a hotspot on the border
     * where nothing happened.
     */
    function bin(points, opts) {
        opts = opts || {};
        const cell = Math.max(1, opts.cell || DEFAULT_CELL);
        const w = opts.w || 1024, h = opts.h || 1024;
        const map = new Map();
        let dropped = 0, total = 0, max = 0;
        (points || []).forEach(p => {
            if (!p || typeof p.x !== 'number' || typeof p.y !== 'number'
                || !isFinite(p.x) || !isFinite(p.y)) { dropped++; return; }
            if (p.x < 0 || p.y < 0 || p.x > w || p.y > h) { dropped++; return; }
            const cx = Math.floor(p.x / cell), cy = Math.floor(p.y / cell);
            const key = cx + ':' + cy;
            const c = map.get(key);
            if (c) c.n++;
            else map.set(key, { cx: cx, cy: cy, x: (cx + 0.5) * cell, y: (cy + 0.5) * cell, n: 1 });
            total++;
        });
        const cells = [];
        map.forEach(c => { cells.push(c); if (c.n > max) max = c.n; });
        // Densest last, so a hotspot is drawn over its own surroundings rather
        // than under them.
        cells.sort((a, b) => a.n - b.n);
        return { cells: cells, max: max, total: total, dropped: dropped, cell: cell };
    }

    /** 0..1 for a cell count, gamma-corrected. A lone point is never 0 - it is
     *  faint, because "one kill happened here" is still information. */
    function intensity(n, max, gamma) {
        if (!n || !max || max <= 0) return 0;
        const t = Math.min(1, n / max);
        return Math.pow(t, gamma === undefined ? DEFAULT_GAMMA : gamma);
    }

    /** Cold→hot ramp: translucent blue → cyan → yellow → red. Chosen to stay
     *  readable over both the dark walkable areas and the light callout text
     *  of a CS2 radar image. */
    function ramp(t) {
        const stops = [
            [0.00, [ 40,  90, 220]],
            [0.35, [ 30, 200, 200]],
            [0.65, [235, 210,  60]],
            [1.00, [235,  55,  40]],
        ];
        const x = Math.max(0, Math.min(1, t || 0));
        for (let i = 1; i < stops.length; i++) {
            if (x <= stops[i][0]) {
                const [t0, c0] = stops[i - 1], [t1, c1] = stops[i];
                const f = (x - t0) / (t1 - t0 || 1);
                return {
                    r: Math.round(c0[0] + (c1[0] - c0[0]) * f),
                    g: Math.round(c0[1] + (c1[1] - c0[1]) * f),
                    b: Math.round(c0[2] + (c1[2] - c0[2]) * f),
                };
            }
        }
        const last = stops[stops.length - 1][1];
        return { r: last[0], g: last[1], b: last[2] };
    }

    function rgba(t, alpha) {
        const c = ramp(t);
        return 'rgba(' + c.r + ',' + c.g + ',' + c.b + ',' + (alpha === undefined ? 1 : alpha) + ')';
    }

    /** '#ff4d4d' -> {r,g,b}. Accepts 3- and 6-digit hex. */
    function hexRgb(hex) {
        let h = String(hex || '').replace('#', '');
        if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
        const n = parseInt(h, 16);
        if (!isFinite(n)) return { r: 255, g: 255, b: 255 };
        return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
    }

    /**
     * Single-hue variant of rgba(): the layer keeps its own colour and
     * intensity drives brightness+alpha instead of hue.
     *
     * The cold->hot ramp is the right encoding for ONE quantity, and the wrong
     * one for several at once: the match page draws up to seven layers (kills,
     * deaths, shots, HE, molotov, flash, smoke) on one radar, and if they all
     * ran through the same ramp a dense smoke cell and a dense kill cell would
     * be the same red. There, hue is the layer and intensity is the count.
     */
    function shade(hex, t, alpha) {
        const c = hexRgb(hex);
        const x = Math.max(0, Math.min(1, t || 0));
        // Lift toward white as it gets denser, so a peak reads as hotter than
        // its surroundings even within one hue.
        const lift = 0.25 * x;
        const mix = v => Math.round(v + (255 - v) * lift);
        return 'rgba(' + mix(c.r) + ',' + mix(c.g) + ',' + mix(c.b) + ','
            + (alpha === undefined ? 1 : alpha) + ')';
    }

    /** Legend ticks for a max count: at most 4 labelled steps, always whole
     *  moments (there is no such thing as 2.5 kills in a cell). */
    function legendSteps(max, n) {
        const steps = Math.max(2, Math.min(n || 4, max || 1));
        const out = [];
        for (let i = 0; i < steps; i++) {
            const t = i / (steps - 1);
            out.push({ t: t, n: Math.max(1, Math.round(t * (max || 1))) });
        }
        // De-duplicate the low end when max is tiny (1,1,1,1 reads as broken).
        return out.filter((s, i) => i === 0 || s.n !== out[i - 1].n);
    }

    return { DEFAULT_CELL, DEFAULT_GAMMA, bin, intensity, ramp, rgba, hexRgb, shade, legendSteps };
}));
