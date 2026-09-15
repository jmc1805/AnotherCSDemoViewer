/**
 * Unit tests for the audibility model (static/sound.logic.js).
 * Run: node test/sound.logic.test.mjs   (no deps, no browser)
 *
 * What matters: a suppressed gun is quiet and an unsuppressed one is not (the
 * whole reason the layer is worth drawing), one-shot sounds decay instead of
 * lingering, the previous round's gunfire never bleeds into this one, and an
 * absent event array contributes nothing rather than throwing.
 */
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const S = require('../static/sound.logic.js');

let passed = 0, failed = 0;
const ok = (c, m) => { if (c) passed++; else { failed++; console.error('  ✗ FAIL:', m); } };
const eq = (a, b, m) => ok(a === b, `${m} (got ${JSON.stringify(a)}, want ${JSON.stringify(b)})`);
const near = (a, b, m, tol = 1e-6) => ok(Math.abs(a - b) <= tol, `${m} (got ${a}, want ~${b})`);

// ── shotRadius: suppressed vs not ────────────────────────────────────────────
{
    eq(S.shotRadius('weapon_ak47'), S.RADII.gunshot, 'AK is loud');
    eq(S.shotRadius('weapon_awp'), S.RADII.gunshot, 'AWP is loud');
    eq(S.shotRadius('weapon_usps'), S.RADII.gunshot_sil, 'USP-S is quiet');
    eq(S.shotRadius('weapon_usp_silencer'), S.RADII.gunshot_sil, 'USP-S under its econ name is quiet');
    eq(S.shotRadius('weapon_m4a1_silencer'), S.RADII.gunshot_sil, 'M4A1-S is quiet');
    eq(S.shotRadius('weapon_m4a4'), S.RADII.gunshot, 'M4A4 is NOT the silenced M4');
    eq(S.shotRadius(undefined), S.RADII.gunshot, 'unknown weapon assumed loud (the safe direction)');
    ok(S.RADII.gunshot_sil < S.RADII.footstep,
       'a suppressed shot carries less far than a footstep - the point of the model');
}

// ── movement: silent below the cutoff, flat above it ─────────────────────────
{
    eq(S.movement(0), null, 'standing still is silent');
    eq(S.movement(100), null, 'walking below the cutoff is silent');
    eq(S.movement(null), null, 'unknown speed is not a sound');
    eq(S.movement(NaN), null, 'NaN speed is not a sound');

    const running = S.movement(250);
    eq(running.radius, S.RADII.footstep, 'running is audible at the footstep radius');
    eq(running.alpha, 1, 'well above the cutoff is fully opaque');
    eq(running.kind, 'footstep', 'tagged as movement');

    // The fade band exists so the ring does not pop on and off around the
    // threshold; at the exact cutoff it is half faded in.
    const edge = S.movement(135);
    near(edge.alpha, 1, 'at the cutoff the fade has completed');
    const mid = S.movement(125);
    near(mid.alpha, 0.5, 'halfway through the fade band');
    eq(S.movement(114), null, 'below the fade band, nothing');

    eq(S.movement(300).radius, S.movement(140).radius,
       'sprinting is not louder than jogging - a footstep is a footstep');
}

// ── decayAlpha ───────────────────────────────────────────────────────────────
{
    eq(S.decayAlpha(100, 100, 10), 1, 'full at the event tick');
    near(S.decayAlpha(105, 100, 10), 0.5, 'half way through the decay');
    eq(S.decayAlpha(110, 100, 10), 0, 'gone at the end of the decay');
    eq(S.decayAlpha(200, 100, 10), 0, 'long gone stays gone');
    eq(S.decayAlpha(99, 100, 10), 0, 'not audible before it happens');
    eq(S.decayAlpha(100, null, 10), 0, 'a missing event tick is silent, not NaN');
}

// ── events: absent arrays ────────────────────────────────────────────────────
{
    eq(S.events().length, 0, 'no options at all -> nothing');
    eq(S.events({ tick: 500 }).length, 0, 'no event arrays -> nothing');
    eq(S.events({ tick: 500, shots: [], he: [], flashes: [], bomb: [] }).length, 0,
       'empty arrays -> nothing');
}

// ── events: gunshots ─────────────────────────────────────────────────────────
{
    const shots = [
        { tick: 1000, weapon: 'weapon_ak47', eye_x: 10, eye_y: 20 },
        { tick: 1000, weapon: 'weapon_usps', eye_x: 30, eye_y: 40 },
    ];
    const now = S.events({ tick: 1000, shots });
    eq(now.length, 2, 'both shots audible on the tick they were fired');
    eq(now[0].radius, S.RADII.gunshot, 'AK at the loud radius');
    eq(now[1].radius, S.RADII.gunshot_sil, 'USP-S at the quiet radius');
    eq(now[0].x, 10, 'uses the eye position when present');
    eq(now[0].y, 20, 'uses the eye position when present (y)');

    eq(S.events({ tick: 1000 + S.DECAY.gunshot, shots }).length, 0,
       'a gunshot stops being drawn once its decay elapses');
    eq(S.events({ tick: 999, shots }).length, 0, 'not audible a tick before it happens');

    // Older exports have no eye position; fall back to the feet origin.
    const old = S.events({ tick: 500, shots: [{ tick: 500, weapon: 'weapon_ak47', X: 7, Y: 8 }] });
    eq(old.length, 1, 'a shot with no eye position is still a sound');
    eq(old[0].x, 7, 'falls back to X');
    eq(old[0].y, 8, 'falls back to Y');

    // A shot with no position at all is dropped rather than placed at (0,0),
    // which would burn a permanent hotspot into the corner of the map.
    eq(S.events({ tick: 500, shots: [{ tick: 500, weapon: 'weapon_ak47' }] }).length, 0,
       'a positionless shot is dropped, not drawn at the origin');
}

// ── events: round scoping ────────────────────────────────────────────────────
{
    const shots = [{ tick: 900, weapon: 'weapon_ak47', eye_x: 1, eye_y: 1 }];
    eq(S.events({ tick: 900, shots, roundStart: 800 }).length, 1,
       'a shot inside the round is audible');
    eq(S.events({ tick: 900, shots, roundStart: 950 }).length, 0,
       "the previous round's gunfire does not bleed into this one");
}

// ── events: grenades and the bomb ────────────────────────────────────────────
{
    const he = [{ tick: 2000, X: 5, Y: 6 }];
    const flashes = [{ tick: 2000, X: 7, Y: 8 }];
    const out = S.events({ tick: 2000, he, flashes });
    eq(out.length, 2, 'HE and flash detonations are both sounds');
    ok(out.every(s => s.radius === S.RADII.explosion), 'both at the explosion radius');
    ok(S.RADII.explosion > S.RADII.footstep, 'an explosion carries further than a footstep');

    const bomb = [
        { tick: 3000, event: 'plant', X: 1, Y: 2 },
        { tick: 3100, event: 'defuse', X: 1, Y: 2 },
        { tick: 3200, event: 'drop', X: 1, Y: 2 },
        { tick: 3300, event: 'pickup', X: 1, Y: 2 },
    ];
    eq(S.events({ tick: 3000, bomb }).length, 1, 'a plant is a sound');
    eq(S.events({ tick: 3100, bomb }).length, 1, 'a defuse is a sound');
    eq(S.events({ tick: 3200, bomb }).length, 0, 'dropping the bomb is not modelled as one');
    eq(S.events({ tick: 3300, bomb }).length, 0, 'picking it up is not either');
    eq(S.events({ tick: 3000, bomb })[0].radius, S.RADII.bomb, 'plant at the bomb radius');
}

// ── the table itself ─────────────────────────────────────────────────────────
{
    ok(S.RADII.gunshot >= 3900 && S.RADII.gunshot <= 4000,
       'unsuppressed gunfire ~4000u, per the measured range');
    ok(S.RADII.footstep >= 1000 && S.RADII.footstep <= 1200,
       'running footsteps ~1100u');
    ok(S.RADII.bomb >= 1000, 'plant/defuse is a loud, non-silenceable action');
    ok(S.DECAY.gunshot < S.DECAY.explosion && S.DECAY.explosion < S.DECAY.bomb,
       'decays are ordered shortest-to-longest by how long the real cue lasts');
}

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
