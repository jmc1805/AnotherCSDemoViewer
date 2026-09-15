/**
 * momentheat.js - canvas half of the analyser's result heatmap.
 *
 * Pairs with momentheat.logic.js exactly like utilfx / grounditems do: the
 * maths lives in the pure module and is unit-tested, this one only paints.
 *
 * Drawn in WORLD space (under the viewport transform), because the points are
 * map positions - it has to pan and zoom with the radar, not float above it.
 * Callers pass their own `worldToCanvas`, so this works identically in the
 * multi-match overlay and anywhere else that has a radar and a projection.
 */
const MomentHeat = (() => {
    'use strict';

    /**
     * @param ctx     2D context, viewport transform already applied
     * @param opts.points        [{x, y}] in WORLD coordinates
     * @param opts.worldToCanvas (wx, wy) -> {x, y} in base-canvas coordinates
     * @param opts.cell          bin size in base px (default from the logic module)
     * @param opts.alpha         peak opacity (default 0.62 - the radar has to
     *                            stay readable underneath, or the layer is
     *                            decoration rather than an overlay)
     * @param opts.mode          'heat' (default) | 'points'
     * @param opts.color         hex hue for a single-layer draw. Omitted =>
     *                            the cold->hot ramp (one quantity). Set =>
     *                            MomentHeatLogic.shade, so several layers can
     *                            share one radar and stay tellable apart.
     * @returns the binning result, so the caller can render a legend that
     *          states what the darkest cell actually means
     */
    function draw(ctx, opts) {
        opts = opts || {};
        const w = opts.w || (ctx.canvas ? ctx.canvas.width : 1024);
        const h = opts.h || (ctx.canvas ? ctx.canvas.height : 1024);
        const projected = [];
        (opts.points || []).forEach(p => {
            if (!p) return;
            const c = opts.worldToCanvas(p.x, p.y);
            if (c) projected.push(c);
        });
        const cell = opts.cell || MomentHeatLogic.DEFAULT_CELL;
        const binned = MomentHeatLogic.bin(projected, { cell: cell, w: w, h: h });
        if (!binned.cells.length) return binned;

        const alpha = opts.alpha === undefined ? 0.62 : opts.alpha;
        const paint = opts.color
            ? (t, a) => MomentHeatLogic.shade(opts.color, t, a)
            : (t, a) => MomentHeatLogic.rgba(t, a);
        ctx.save();
        if (opts.mode === 'points') {
            // Every moment as its own dot: the honest view when there are few
            // enough of them that a density estimate would be a fiction.
            ctx.globalAlpha = Math.min(1, alpha + 0.15);
            ctx.fillStyle = paint(0.8, 1);
            projected.forEach(p => {
                ctx.beginPath();
                ctx.arc(p.x, p.y, 3, 0, Math.PI * 2);
                ctx.fill();
            });
        } else {
            // Additive soft blobs. 'lighter' is what makes overlapping cells
            // read as "more happened here" instead of as the topmost cell's
            // colour - the whole point of a density layer.
            ctx.globalCompositeOperation = 'lighter';
            const r = cell * 1.35;
            binned.cells.forEach(c => {
                const t = MomentHeatLogic.intensity(c.n, binned.max);
                const g = ctx.createRadialGradient(c.x, c.y, 0, c.x, c.y, r);
                g.addColorStop(0, paint(t, alpha * (0.35 + 0.65 * t)));
                g.addColorStop(1, paint(t, 0));
                ctx.fillStyle = g;
                ctx.beginPath();
                ctx.arc(c.x, c.y, r, 0, Math.PI * 2);
                ctx.fill();
            });
        }
        ctx.restore();
        return binned;
    }

    /** Legend markup for the panel next to the map. Kept here so the caller
     *  never has to reimplement the ramp in CSS and drift from the canvas. */
    function legendHtml(binned, label, color) {
        if (!binned || !binned.total) return '';
        const steps = MomentHeatLogic.legendSteps(binned.max, 4);
        const paint = color
            ? (t, a) => MomentHeatLogic.shade(color, t, a)
            : (t, a) => MomentHeatLogic.rgba(t, a);
        const swatches = steps.map(s =>
            `<span class="heat-swatch" style="background:${paint(s.t, 0.9)}" title="${s.n} per cell"></span>`
        ).join('');
        const dropped = binned.dropped
            ? ` · ${binned.dropped} off-map` : '';
        return `<span class="heat-legend-l">${label || 'moments'}</span>${swatches}
            <span class="heat-legend-r">1–${binned.max} per cell · ${binned.total} plotted${dropped}</span>`;
    }

    return { draw, legendHtml };
})();
