/**
 * fitview.js - keeps the replayer's map sized so the page fits the window.
 * Measures the page, asks FitViewLogic.compute (static/fitview.logic.js) and
 * writes --fit-col / --fit-h onto the map row. The page's CSS decides where
 * those apply (the wide tiers only), so a stacked layout ignores them without
 * this file knowing a single breakpoint.
 *
 * FitView.bind({ row, col, canvas, last, watch: [elements], limits })
 *   row     the flex row holding the map column (receives the properties)
 *   col     the map column; canvas the square canvas inside it
 *   last    the lowest element that must stay on screen
 *   watch   elements whose size changes move the budget (hero, round nav…)
 * → { refresh } or null when an element or FitViewLogic is missing.
 */
(function (root) {
    'use strict';

    function bind(opts) {
        const o = opts || {};
        const { row, col, canvas, last } = o;
        if (!row || !col || !canvas || !last || !root.FitViewLogic) return null;

        let frame = 0;
        function set(name, px) {
            const v = Math.round(px) + 'px';
            if (row.style.getPropertyValue(name) !== v) row.style.setProperty(name, v);
        }
        function measure() {
            frame = 0;
            const rowBox = row.getBoundingClientRect();
            const lastBox = last.getBoundingClientRect();
            const colBox = col.getBoundingClientRect();
            const cvBox = canvas.getBoundingClientRect();
            const mb = parseFloat(getComputedStyle(last).marginBottom) || 0;
            const r = root.FitViewLogic.compute({
                viewportH: root.innerHeight,
                rowTop: rowBox.top + (root.scrollY || 0),
                belowH: lastBox.bottom + mb - rowBox.bottom,
                chromeV: colBox.height - cvBox.height,
                chromeH: colBox.width - cvBox.width,
            }, o.limits);
            if (!r) return;
            set('--fit-col', r.colMax);
            set('--fit-h', r.rowH);
        }
        // One measurement per frame however many observers fire at once.
        function schedule() { if (!frame) frame = root.requestAnimationFrame(measure); }

        root.addEventListener('resize', schedule);
        if (root.ResizeObserver) {
            const ro = new root.ResizeObserver(schedule);
            (o.watch || []).forEach(el => { if (el) ro.observe(el); });
        }
        schedule();
        return { refresh: schedule };
    }

    root.FitView = { bind };
}(typeof self !== 'undefined' ? self : this));
