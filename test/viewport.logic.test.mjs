/**
 * Unit tests for the shared pan/zoom transform math (static/viewport.logic.js).
 * Run: node test/viewport.logic.test.mjs   (no deps, no browser)
 *
 * The invariants that matter for correctness: zoomAt anchors the cursor point,
 * screenToBase inverts baseToScreen, clampPan never lets a gap show, and
 * isVisible tracks content moving in/out of frame under pan.
 */
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const V = require('../static/viewport.logic.js');

let passed = 0, failed = 0;
const approx = (a, b, eps = 1e-9) => Math.abs(a - b) <= eps;
const ok = (c, m) => { if (c) passed++; else { failed++; console.error('  ✗ FAIL:', m); } };
const near = (a, b, m) => ok(approx(a, b), `${m} (got ${a}, want ${b})`);

const W = 1024, H = 1024;

// ── baseToScreen / screenToBase are inverses ──────────────────────────────────
{
    const v = V.create();
    V.zoomAt(v, 300, 400, 2);            // some arbitrary non-identity view
    const s = V.baseToScreen(v, 123, 456);
    const b = V.screenToBase(v, s.x, s.y);
    near(b.x, 123, 'screenToBase inverts baseToScreen (x)');
    near(b.y, 456, 'screenToBase inverts baseToScreen (y)');
}

// ── zoomAt anchors the cursor: the base point under the cursor stays put ───────
{
    const v = V.create();
    const before = V.screenToBase(v, 700, 200);
    V.zoomAt(v, 700, 200, 1.15);
    const after = V.screenToBase(v, 700, 200);
    near(after.x, before.x, 'cursor base point fixed under zoom (x)');
    near(after.y, before.y, 'cursor base point fixed under zoom (y)');
    ok(approx(v.scale, 1.15), 'scale updated');
}

// ── scale is clamped to [minScale, maxScale] ──────────────────────────────────
{
    const v = V.create({ minScale: 1, maxScale: 4 });
    for (let i = 0; i < 50; i++) V.zoomAt(v, 512, 512, 2);
    ok(v.scale === 4, 'scale clamped to maxScale');
    for (let i = 0; i < 50; i++) V.zoomAt(v, 512, 512, 0.5);
    ok(v.scale === 1, 'scale clamped to minScale');
}

// ── clampPan keeps content covering the canvas (no gap) ───────────────────────
{
    const v = V.create();
    V.zoomAt(v, 512, 512, 3);            // scale 3 → content 3072 wide
    V.panBy(v, 99999, 99999);            // shove way off
    V.clampPan(v, W, H);
    ok(v.tx <= 0 && v.tx >= W - W * v.scale, 'tx stays within [w-cw, 0]');
    ok(v.ty <= 0 && v.ty >= H - H * v.scale, 'ty stays within [h-ch, 0]');
    // top-left corner (base 0,0) must not pull inside the canvas → no gap
    ok(V.baseToScreen(v, 0, 0).x <= 0, 'no left gap after clamp');
}

// ── at reset, identity: base==screen, tx=ty=0, scale=min ──────────────────────
{
    const v = V.create();
    V.zoomAt(v, 100, 100, 2); V.panBy(v, 50, -30);
    V.reset(v);
    ok(V.isReset(v), 'isReset true after reset');
    const s = V.baseToScreen(v, 640, 480);
    ok(s.x === 640 && s.y === 480, 'identity maps base to itself');
    // clampPan at scale 1 keeps tx=ty=0 (content exactly fills canvas)
    V.clampPan(v, W, H);
    ok(v.tx === 0 && v.ty === 0, 'clampPan keeps identity at scale 1');
}

// ── isVisible tracks content moving out of frame under pan ────────────────────
{
    const v = V.create();
    ok(V.isVisible(v, 10, 10, W, H, 0), 'top-left visible at reset');
    V.zoomAt(v, 0, 0, 4);               // zoom into the top-left corner
    ok(!V.isVisible(v, 1000, 1000, W, H, 0), 'far corner off-frame when zoomed into TL');
}

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
