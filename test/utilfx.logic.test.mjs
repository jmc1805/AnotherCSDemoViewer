/**
 * Unit tests for the utility-rendering math (static/utilfx.logic.js).
 * Run: node test/utilfx.logic.test.mjs   (no deps, no browser)
 *
 * What matters: molotov/incendiary classification from trajectory data
 * (incl. the parser's additive `weapon` field taking precedence), HE smoke
 * holes opening fast + refilling + respecting time/space windows, fire
 * spread easing to the weapon-correct size, and deterministic blob layouts.
 */
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const U = require('../static/utilfx.logic.js');

let passed = 0, failed = 0;
const ok = (c, m) => { if (c) passed++; else { failed++; console.error('  ✗ FAIL:', m); } };
const eq = (a, b, m) => ok(a === b, `${m} (got ${JSON.stringify(a)}, want ${JSON.stringify(b)})`);

// ── classifyInfernos: explicit weapon field wins ──────────────────────────────
{
    const infs = [{ start_tick: 100, X: 0, Y: 0, thrower_name: 'a', weapon: 'Incendiary' }];
    eq(U.classifyInfernos(infs, [])[0], 'Incendiary', 'parser weapon field is used as-is');
}

// ── classifyInfernos: correlates by thrower + time from trajectory stream ─────
{
    const grenades = [
        // bob's incendiary flies and lands near (500,500) just before tick 1000
        { entity_id: 7, grenade_type: 'Incendiary', thrower: 'bob', tick: 940, X: 480, Y: 470 },
        { entity_id: 7, grenade_type: 'Incendiary', thrower: 'bob', tick: 990, X: 500, Y: 500 },
        // alice's molotov elsewhere, much earlier
        { entity_id: 9, grenade_type: 'Molotov', thrower: 'alice', tick: 200, X: 0, Y: 0 },
        // non-fire grenades must be ignored entirely
        { entity_id: 11, grenade_type: 'SmokeGrenade', thrower: 'bob', tick: 995, X: 500, Y: 500 },
    ];
    const infs = [
        { start_tick: 1000, X: 505, Y: 495, thrower_name: 'bob' },
        { start_tick: 230, X: 5, Y: 5, thrower_name: 'alice' },
        { start_tick: 5000, X: 0, Y: 0, thrower_name: 'nobody' },   // no match
    ];
    const w = U.classifyInfernos(infs, grenades);
    eq(w[0], 'Incendiary', "bob's fire classified from his incendiary landing");
    eq(w[1], 'Molotov', "alice's fire classified from her molotov");
    eq(w[2], 'Molotov', 'unmatched inferno falls back to Molotov');
}

// ── classifyInfernos: time window - stale landings don't claim new fires ──────
{
    const grenades = [
        { entity_id: 1, grenade_type: 'Incendiary', thrower: 'bob', tick: 100, X: 0, Y: 0 },
    ];
    const infs = [{ start_tick: 2000, X: 0, Y: 0, thrower_name: 'bob' }];
    eq(U.classifyInfernos(infs, grenades)[0], 'Molotov',
       'landing far outside the window is ignored (falls back)');
}

// ── clampEnd: caps display duration to the authoritative real-game max ────────
{
    // demo recorded a much longer end (entity lingering) - clamp wins
    eq(U.clampEnd(1000, 1000 + 22 * 64, U.FIRE_LIFETIME_TICKS), 1000 + U.FIRE_LIFETIME_TICKS,
       'inflated recorded duration is clamped to the real burn time');
    // recorded end genuinely earlier than the cap (e.g. round ended) - kept as-is
    eq(U.clampEnd(1000, 1200, U.FIRE_LIFETIME_TICKS), 1200,
       'a recorded end shorter than the cap is not extended');
    // missing end_tick - falls back to start + cap
    eq(U.clampEnd(1000, null, U.SMOKE_LIFETIME_TICKS), 1000 + U.SMOKE_LIFETIME_TICKS,
       'missing end_tick falls back to start + max lifetime');
    eq(U.FIRE_LIFETIME_TICKS, 450, 'fire lifetime is exactly 7.03125s @ 64 tick');
    eq(U.SMOKE_LIFETIME_TICKS, 1152, 'smoke lifetime is exactly 18s @ 64 tick');
}

// ── infernoMaxRadius: CT incendiary burns smaller than T molotov ──────────────
{
    ok(U.infernoMaxRadius('Incendiary') < U.infernoMaxRadius('Molotov'),
       'incendiary footprint smaller than molotov');
    eq(U.infernoMaxRadius('Molotov'), U.MOLOTOV_RADIUS, 'molotov uses MOLOTOV_RADIUS');
}

// ── fireSpreadRadius: grows monotonically to the max, starts small ────────────
{
    const max = U.MOLOTOV_RADIUS;
    const r0 = U.fireSpreadRadius(0, max);
    const rMid = U.fireSpreadRadius(U.FIRE_SPREAD_TICKS / 2, max);
    const rFull = U.fireSpreadRadius(U.FIRE_SPREAD_TICKS, max);
    ok(r0 < max * 0.25, `starts small (${r0.toFixed(1)} vs max ${max})`);
    ok(r0 < rMid && rMid < rFull, 'radius grows monotonically');
    ok(Math.abs(rFull - max) < 1e-9, 'reaches full size at spread end');
    ok(U.fireSpreadRadius(9999, max) <= max, 'never exceeds max');
}

// ── smokeHoles: opens fast, refills, respects windows ─────────────────────────
{
    const smoke = { start_tick: 1000, end_tick: 2200, X: 0, Y: 0 };
    const he = [{ tick: 1500, X: 30, Y: 0 }];
    // just after detonation: fully carved
    const h1 = U.smokeHoles(smoke, he, 1510);
    eq(h1.length, 1, 'hole exists right after detonation');
    ok(h1[0].strength > 0.9, `hole near full strength shortly after (${h1[0].strength.toFixed(2)})`);
    // mid-regrow: weaker
    const h2 = U.smokeHoles(smoke, he, 1500 + Math.floor(U.HOLE_REGROW_TICKS * 0.7));
    ok(h2.length === 1 && h2[0].strength < h1[0].strength, 'hole weakens as smoke refills');
    // after regrow: gone
    eq(U.smokeHoles(smoke, he, 1500 + U.HOLE_REGROW_TICKS + 10).length, 0, 'hole gone after regrow');
    // before detonation: nothing
    eq(U.smokeHoles(smoke, he, 1490).length, 0, 'no hole before the HE detonates');
    // HE far away: no hole
    eq(U.smokeHoles(smoke, [{ tick: 1500, X: 9999, Y: 0 }], 1510).length, 0,
       'distant HE does not carve');
    // HE before the smoke existed: no hole
    eq(U.smokeHoles(smoke, [{ tick: 900, X: 0, Y: 0 }], 1000).length, 0,
       'HE that predates the smoke is ignored');
}

// ── smokeHoles: custom tick accessors (multi.js round-relative ticks) ─────────
{
    const smoke = { startRel: 0, endRel: 1000, X: 0, Y: 0 };
    const he = [{ rel: 100, X: 0, Y: 0 }];
    const holes = U.smokeHoles(smoke, he, 110, {
        tickOf: h => h.rel, startOf: s => s.startRel, endOf: s => s.endRel,
    });
    eq(holes.length, 1, 'accessor overrides support round-relative ticks');
}

// ── blobCarve: blobs inside a hole shrink, outside stay ───────────────────────
{
    const holes = [{ x: 0, y: 0, r: 100, strength: 1 }];
    ok(U.blobCarve(0, 0, holes) < 0.1, 'blob at hole center nearly gone');
    eq(U.blobCarve(500, 0, holes), 1, 'blob far away untouched');
    const edge = U.blobCarve(90, 0, holes);
    ok(edge > 0 && edge <= 1, `blob near hole edge partially carved (${edge.toFixed(2)})`);
    eq(U.blobCarve(0, 0, []), 1, 'no holes → no carve');
}

// ── blobLayout: deterministic, inside unit disc ───────────────────────────────
{
    const a = U.blobLayout(1234, 8), b = U.blobLayout(1234, 8);
    eq(JSON.stringify(a), JSON.stringify(b), 'same seed → identical layout');
    ok(JSON.stringify(a) !== JSON.stringify(U.blobLayout(999, 8)), 'different seed differs');
    ok(a.every(bl => Math.hypot(bl.dx, bl.dy) <= 1 && bl.r > 0), 'blobs inside unit disc');
    eq(a.length, 8, 'requested count honoured');
}

// ── smokeBloom: 0 → 1 over the bloom window ───────────────────────────────────
{
    ok(U.smokeBloom(0) < 0.05, 'no volume at t=0');
    ok(Math.abs(U.smokeBloom(U.SMOKE_BLOOM_TICKS) - 1) < 1e-9, 'full volume at bloom end');
}

console.log(`\nutilfx.logic: ${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
