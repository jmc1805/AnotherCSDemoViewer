/**
 * Unit tests for static/match2d.logic.js - constants/math shared by
 * viewer.js, multi.js, and templates/match.html.
 * Run: node test/match2d.logic.test.mjs   (no deps, no browser)
 */
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const M = require('../static/match2d.logic.js');

let passed = 0, failed = 0;
const ok = (c, m) => { if (c) passed++; else { failed++; console.error('  ✗ FAIL:', m); } };
const eq = (a, b, m) => ok(a === b, `${m} (got ${JSON.stringify(a)}, want ${JSON.stringify(b)})`);

// ── worldToCanvas ───────────────────────────────────────────────────────────
{
    const cfg = M.MAP_LIBRARY.de_dust2;
    const p = M.worldToCanvas(cfg, cfg.x, cfg.y);
    eq(p.x, 0, 'worldToCanvas: map origin -> canvas x=0');
    eq(p.y, 0, 'worldToCanvas: map origin -> canvas y=0');

    const q = M.worldToCanvas(cfg, cfg.x + cfg.scale * 10, cfg.y - cfg.scale * 20);
    eq(q.x, 10, 'worldToCanvas: x scales by mapConfig.scale');
    eq(q.y, 20, 'worldToCanvas: y is flipped (world-up = canvas-up)');
}

// ── easeOut ──────────────────────────────────────────────────────────────
{
    eq(M.easeOut(0), 0, 'easeOut(0) = 0');
    eq(M.easeOut(1), 1, 'easeOut(1) = 1');
    ok(M.easeOut(0.5) > 0.5, 'easeOut(0.5) overshoots past linear (decelerating curve)');
}

// ── isSwappedAtRoundSimple ──────────────────────────────────────────────────
{
    eq(M.isSwappedAtRoundSimple(1), false, 'round 1 (reg. 1st half): not swapped');
    eq(M.isSwappedAtRoundSimple(12), false, 'round 12 (last reg. 1st half): not swapped');
    eq(M.isSwappedAtRoundSimple(13), true, 'round 13 (reg. 2nd half starts): swapped');
    eq(M.isSwappedAtRoundSimple(24), true, 'round 24 (last reg. round): swapped');
    eq(M.isSwappedAtRoundSimple(25), true, 'round 25 (OT1 1st half, MR3): continues reg. 2nd-half sides (swapped)');
    eq(M.isSwappedAtRoundSimple(28), false, 'round 28 (OT1 2nd half): flips back (not swapped)');
}

// ── roundReasonLabel ─────────────────────────────────────────────────────
{
    eq(M.roundReasonLabel('bomb_exploded'), 'Bomb exploded', 'label: bomb_exploded');
    eq(M.roundReasonLabel('CT_KILLED'), 'CT eliminated', 'label: case-insensitive');
    eq(M.roundReasonLabel('made_up_reason'), '', 'label: unknown reason -> empty string');
    eq(M.roundReasonLabel(undefined), '', 'label: undefined -> empty string');
}

// ── roundReasonIcon: no assets module -> emoji fallback ────────────────────
{
    eq(M.roundReasonIcon('bomb_exploded', null), '💥', 'icon: no assets -> emoji');
    eq(M.roundReasonIcon('bomb_exploded', { available: () => false }), '💥', 'icon: assets unavailable -> emoji');
    eq(M.roundReasonIcon('made_up_reason', null), '', 'icon: unknown reason -> empty string');
}

// ── roundReasonIcon: assets available -> real icon, falls back per-key ─────
{
    const assets = {
        available: () => true,
        url: (kind, key) => (kind === 'equipment' && key === 'c4') ? '/equipment/c4.png' : null,
    };
    eq(M.roundReasonIcon('bomb_exploded', assets),
       '<img class="rreason-ico" src="/equipment/c4.png" alt="💥">',
       'icon: bomb_exploded resolves the c4 equipment icon');
    eq(M.roundReasonIcon('bomb_defused', assets), '✂️',
       'icon: assets present but this key has no url -> emoji fallback');
}

{
    const assets = {
        available: () => true,
        url: (kind, key) => (kind === 'deathnotice' && key === 'suicide') ? '/deathnotice/suicide.png' : null,
    };
    eq(M.roundReasonIcon('t_killed', assets),
       '<img class="rreason-ico" src="/deathnotice/suicide.png" alt="💀">',
       'icon: elimination resolves the deathnotice skull icon');
}

{
    const assets = { available: () => true, url: () => null };
    eq(M.roundReasonIcon('ct_killed', assets), `<img class="rreason-ico" src="${M.SKULL_ICON_SRC}" alt="💀">`,
       'icon: elimination with no deathnotice url falls back to the built-in skull SVG (never a broken image)');
}

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
