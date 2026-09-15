/**
 * viewport.logic.js - pure pan/zoom view-transform math shared by the 2D pages
 * (viewer.js, multi.js). No DOM, no canvas: unit-testable under node
 * (test/viewport.logic.test.mjs). The glue that binds wheel/drag/dblclick and
 * applies the transform to a 2D context lives in static/viewport.js.
 *
 * Coordinate spaces:
 *   • base   - the map's canvas-internal pixels (what worldToCanvas() returns),
 *              i.e. the un-zoomed 0..W × 0..H image space.
 *   • screen - canvas-internal pixels after the view transform is applied.
 * A view is { scale, tx, ty }: screen = base * scale + t. Drawing world content
 * under `ctx.setTransform(scale,0,0,scale,tx,ty)` means every existing
 * base-space draw call inherits zoom/pan for free - no per-call change.
 */
(function (root, factory) {
    if (typeof module === 'object' && module.exports) module.exports = factory();
    else root.ViewportLogic = factory();
}(typeof self !== 'undefined' ? self : this, function () {
    'use strict';

    const DEFAULTS = { minScale: 1, maxScale: 8 };

    function create(opts) {
        const o = Object.assign({}, DEFAULTS, opts || {});
        return { scale: 1, tx: 0, ty: 0, minScale: o.minScale, maxScale: o.maxScale };
    }

    const clampScale = (v, s) => Math.max(v.minScale, Math.min(v.maxScale, s));

    function baseToScreen(v, bx, by) { return { x: bx * v.scale + v.tx, y: by * v.scale + v.ty }; }
    function screenToBase(v, sx, sy) { return { x: (sx - v.tx) / v.scale, y: (sy - v.ty) / v.scale }; }

    // Multiply zoom by `factor`, keeping the screen point (sx,sy) anchored to the
    // same base point (so the map zooms toward the cursor). Returns v (mutated).
    function zoomAt(v, sx, sy, factor) {
        const ns = clampScale(v, v.scale * factor);
        const k = ns / v.scale;
        v.tx = sx - (sx - v.tx) * k;
        v.ty = sy - (sy - v.ty) * k;
        v.scale = ns;
        return v;
    }

    function panBy(v, dx, dy) { v.tx += dx; v.ty += dy; return v; }

    function reset(v) { v.scale = v.minScale; v.tx = 0; v.ty = 0; return v; }

    const isReset = (v) => v.scale === 1 && v.tx === 0 && v.ty === 0;

    // Keep the base content (0..w × 0..h) covering the canvas: never pan so far
    // that a gap shows. If the content is smaller than the canvas on an axis
    // (only possible if minScale < 1), center it on that axis.
    function clampPan(v, w, h) {
        const cw = w * v.scale, ch = h * v.scale;
        v.tx = cw <= w ? (w - cw) / 2 : Math.min(0, Math.max(w - cw, v.tx));
        v.ty = ch <= h ? (h - ch) / 2 : Math.min(0, Math.max(h - ch, v.ty));
        return v;
    }

    // Is a base point within the visible canvas (+margin)? Used for draw culling,
    // which must be view-aware once zoom/pan can move content in and out of frame.
    function isVisible(v, bx, by, w, h, margin) {
        const s = baseToScreen(v, bx, by);
        margin = margin || 0;
        return s.x > -margin && s.x < w + margin && s.y > -margin && s.y < h + margin;
    }

    return { create, clampScale, baseToScreen, screenToBase, zoomAt, panBy, reset, isReset, clampPan, isVisible };
}));
