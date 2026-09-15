/**
 * Unit tests for the replayer's fit-to-window sizing (static/fitview.logic.js).
 * Run: node test/fitview.logic.test.mjs   (no deps, no browser)
 *
 * The invariants: the map takes exactly the height left over, the ceiling and
 * the floor both win when they should, the side columns are never shorter than
 * the map, and chrome around the canvas is paid for out of the budget.
 */
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const F = require('../static/fitview.logic.js');

let passed = 0, failed = 0;
const ok = (c, m) => { if (c) passed++; else { failed++; console.error('  ✗ FAIL:', m); } };
const eq = (a, b, m) => ok(a === b, `${m} (got ${a}, want ${b})`);

const VIEWER = { minSide: 430, maxSide: 756, slack: 8 };

// ── A Surface Pro window (1368x830): the map takes what is left ───────────────
{
    const r = F.compute({ viewportH: 830, rowTop: 92, belowH: 170 }, VIEWER);
    eq(r.side, 560, 'side = viewport - above - below - slack');
    eq(r.colMax, 560, 'no chrome: column max equals the canvas');
    eq(r.rowH, 560, 'side columns get the same height as the map');
    ok(r.fits, 'fits');
}

// ── A tall window: the ceiling wins, the side columns may use the rest ────────
{
    const r = F.compute({ viewportH: 1400, rowTop: 92, belowH: 170 }, VIEWER);
    eq(r.side, 756, 'capped at maxSide');
    eq(r.rowH, 1130, 'rowH is the whole budget, not the capped map');
    ok(r.fits, 'still fits');
}

// ── A very short window: the floor wins and the page scrolls ──────────────────
{
    const r = F.compute({ viewportH: 600, rowTop: 92, belowH: 170 }, VIEWER);
    eq(r.side, 430, 'never smaller than minSide');
    ok(!r.fits, 'reports that it cannot fit');
    ok(r.rowH >= r.side, 'side columns are never shorter than the map');
}

// ── Chrome around the canvas comes out of the budget ──────────────────────────
{
    const r = F.compute({ viewportH: 830, rowTop: 92, belowH: 170, chromeV: 30, chromeH: 24 }, VIEWER);
    eq(r.side, 530, 'vertical chrome shrinks the canvas');
    eq(r.colMax, 554, 'horizontal chrome widens the column max');
    eq(r.rowH, 560, 'the row still gets the full budget');
}

// ── Negative chrome (sub-pixel noise) is ignored, not added ───────────────────
{
    const r = F.compute({ viewportH: 830, rowTop: 92, belowH: 170, chromeV: -0.5, chromeH: -0.5 }, VIEWER);
    eq(r.side, 560, 'negative chromeV ignored');
    eq(r.colMax, 560, 'negative chromeH ignored');
}

// ── Defaults: no limits means the budget is used as-is ────────────────────────
{
    const r = F.compute({ viewportH: 500, rowTop: 0, belowH: 0 });
    eq(r.side, 492, 'only the default slack comes off');
}

// ── Unusable measurements return null, so the CSS fallback stays in charge ────
{
    eq(F.compute(null, VIEWER), null, 'null input');
    eq(F.compute({ viewportH: 0 }, VIEWER), null, 'zero viewport (hidden/preview)');
    eq(F.compute({ viewportH: NaN }, VIEWER), null, 'NaN viewport');
}

console.log(`fitview.logic: ${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
