/**
 * Unit tests for static/momentheat.logic.js - the analyser result heatmap's
 * binning and colour ramp.  Run: node test/momentheat.logic.test.mjs
 *
 * What matters here is that the picture cannot lie: points off the canvas are
 * dropped rather than clamped onto the edge (a clamped point invents a hotspot
 * that never happened), a single point still shows up, and the intensity scale
 * is monotone so a denser cell can never draw cooler than a sparser one.
 */
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const H = require('../static/momentheat.logic.js');

let passed = 0, failed = 0;
const ok = (c, m) => { if (c) passed++; else { failed++; console.error('  ✗ FAIL:', m); } };
const eq = (a, b, m) => ok(a === b, `${m} (got ${JSON.stringify(a)}, want ${JSON.stringify(b)})`);

// ── binning ───────────────────────────────────────────────────────────────────
{
    const pts = [{ x: 10, y: 10 }, { x: 12, y: 14 }, { x: 300, y: 300 }];
    const b = H.bin(pts, { cell: 20, w: 1024, h: 1024 });
    eq(b.total, 3, 'every in-bounds point is counted');
    eq(b.cells.length, 2, 'points inside one cell collapse into it');
    eq(b.max, 2, 'max is the densest cell count');
    const dense = b.cells[b.cells.length - 1];
    eq(dense.n, 2, 'cells sort ascending so the hotspot draws last');
    eq(dense.x, 10, 'cell centre is reported, not the corner');
}

{
    // Off-canvas points are dropped, NOT clamped: clamping would paint a
    // hotspot on the map edge out of moments that happened nowhere near it.
    const b = H.bin([{ x: -5, y: 10 }, { x: 10, y: 2000 }, { x: 10, y: 10 }],
                    { cell: 20, w: 1024, h: 1024 });
    eq(b.total, 1, 'only the in-bounds point is binned');
    eq(b.dropped, 2, 'the rest are reported as dropped');
    eq(b.cells.length, 1, 'and no edge cell is invented');
}

{
    const b = H.bin([{ x: 1, y: 1 }, null, { x: NaN, y: 3 }, { x: 5 }], { cell: 10 });
    eq(b.total, 1, 'null / NaN / half-formed points are skipped, not crashed on');
    eq(b.dropped, 3, 'and counted');
}

{
    eq(H.bin([], {}).total, 0, 'no points is an empty result, not an error');
    eq(H.bin([], {}).max, 0, 'with a zero max');
}

// ── intensity ─────────────────────────────────────────────────────────────────
{
    eq(H.intensity(0, 10), 0, 'an empty cell has no intensity');
    eq(H.intensity(10, 10), 1, 'the densest cell is full intensity');
    ok(H.intensity(1, 100) > 0, 'a single moment is faint but never invisible');
    // sqrt scaling: the long tail has to stay visible, which is the whole
    // reason not to scale linearly.
    ok(H.intensity(25, 100) > 25 / 100, 'the default gamma lifts the tail above linear');
    eq(H.intensity(25, 100, 1), 0.25, 'gamma 1 gives the plain linear scale back');
    let prev = -1;
    for (let n = 0; n <= 20; n++) {
        const t = H.intensity(n, 20);
        ok(t >= prev, 'intensity is monotone in the count');
        prev = t;
    }
    eq(H.intensity(5, 0), 0, 'a zero max cannot divide by zero');
}

// ── ramp ──────────────────────────────────────────────────────────────────────
{
    const cold = H.ramp(0), hot = H.ramp(1);
    ok(cold.b > cold.r, 'the cold end is blue');
    ok(hot.r > hot.b, 'the hot end is red');
    const mid = H.ramp(0.5);
    ok(mid.r >= 0 && mid.r <= 255 && mid.g >= 0 && mid.g <= 255, 'interpolated stops stay in range');
    const clamped = H.ramp(9);
    eq(JSON.stringify(clamped), JSON.stringify(hot), 'out-of-range t clamps to the hot end');
    ok(H.rgba(0.5, 0.3).startsWith('rgba('), 'rgba() produces a css colour');
    ok(H.rgba(0.5, 0.3).endsWith(',0.3)'), 'and carries the requested alpha');
}

// ── legend ────────────────────────────────────────────────────────────────────
{
    const steps = H.legendSteps(12, 4);
    eq(steps.length, 4, 'four labelled steps by default');
    eq(steps[steps.length - 1].n, 12, 'the last step is the real max');
    ok(steps.every(s => Number.isInteger(s.n)), 'counts are whole moments');
    const tiny = H.legendSteps(1, 4);
    eq(tiny.length, 1, 'a max of 1 collapses to a single step instead of repeating it');
}

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
