// bombaction.logic - the plant/defuse action in progress, and whether a
// defuse beats the fuse. Run standalone: `node test/bombaction.logic.test.mjs`.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const L = require('../static/bombaction.logic.js');

let passed = 0, failed = 0;
function ok(cond, msg) { if (cond) { passed++; } else { failed++; console.log('  x FAIL: ' + msg); } }
function eq(got, want, msg) {
    const g = JSON.stringify(got), w = JSON.stringify(want);
    ok(g === w, `${msg} (got ${g}, want ${w})`);
}
function near(got, want, msg, tol = 1e-6) {
    ok(Math.abs(got - want) <= tol, `${msg} (got ${got}, want ${want})`);
}

const T = L.TICKRATE;

// ── which events describe the bomb's location vs an action ──────────────────
// viewer.js's getBombState is a last-event-wins scan; if it saw the action
// events it would answer "defuse_start" for a planted bomb and drop the 40 s
// countdown mid-defuse. This filter is what keeps the two readings apart.
for (const ev of ['plant', 'defuse', 'pickup', 'drop', 'detonate'])
    ok(L.isStateEvent(ev), `${ev} is a bomb-location event`);
for (const ev of ['plant_begin', 'plant_abort', 'defuse_start', 'defuse_abort'])
    ok(!L.isStateEvent(ev), `${ev} is an action, not a bomb-location event`);

// ── durations, measured from real demos (see the module header) ─────────────
// Ticks, not seconds: that is the unit the demo carries, and 3.2s -- the value
// the wiki gives for a plant -- is 204.8 ticks, which nothing in the corpus
// ever lands on. Every completed plant measured 200.
eq(L.durationTicks('plant'), 200, 'a plant takes 200 ticks (3.125s), not the wiki 3.2s');
eq(L.durationTicks('defuse', false), 640, 'a defuse without a kit takes 640 ticks (10s)');
eq(L.durationTicks('defuse', true), 320, 'a defuse with a kit takes 320 ticks (5s)');
near(L.durationSeconds('plant'), 3.125, 'seconds are derived from the tick constant');

// ── an in-progress plant ────────────────────────────────────────────────────
{
    const bomb = [
        { tick: 1000, event: 'plant_begin', name: 'Ana', X: 10, Y: 20, bombsite: 'A' },
        { tick: 1000 + L.PLANT_TICKS, event: 'plant', name: 'Ana', X: 10, Y: 20 },
    ];
    eq(L.actionAt(bomb, 999, 0), null, 'nothing in progress before the plant begins');

    const half = 1000 + L.PLANT_TICKS / 2;
    const mid = L.actionAt(bomb, half, 0);
    ok(mid && mid.kind === 'plant', 'a plant in progress is reported');
    eq(mid.player, 'Ana', 'the actor is named');
    near(mid.progress, 0.5, 'progress is elapsed/duration');
    near(L.remainingSeconds(mid, half), L.PLANT_SECONDS / 2, 'remaining seconds');
    eq(mid.hasKit, null, 'a plant has no kit notion');

    eq(L.actionAt(bomb, 1000 + L.PLANT_TICKS, 0), null,
       'the action is over once it completes');
}

// ── an aborted plant clears immediately ─────────────────────────────────────
{
    const bomb = [
        { tick: 1000, event: 'plant_begin', name: 'Ana' },
        { tick: 1050, event: 'plant_abort', name: 'Ana' },
    ];
    ok(L.actionAt(bomb, 1040, 0), 'still planting a tick before the abort');
    eq(L.actionAt(bomb, 1050, 0), null, 'an abort clears the action at once');
}

// ── the kit is what makes a defuse 5s instead of 10s ────────────────────────
{
    const withKit = [{ tick: 5000, event: 'defuse_start', name: 'Bo', has_kit: true }];
    const noKit = [{ tick: 5000, event: 'defuse_start', name: 'Bo', has_kit: false }];
    eq(L.actionAt(withKit, 5100, 0).durationTicks, 5 * T, 'kit defuse runs 5s');
    eq(L.actionAt(noKit, 5100, 0).durationTicks, 10 * T, 'kitless defuse runs 10s');
    // A parse predating has_kit must not silently become the fast defuse.
    const old = [{ tick: 5000, event: 'defuse_start', name: 'Bo' }];
    eq(L.actionAt(old, 5100, 0).durationTicks, 10 * T,
       'an absent has_kit is treated as no kit, never as the 5s defuse');
}

// ── does the defuse beat the fuse? the whole point of the feature ───────────
{
    const plantTick = 10000;
    // Kitless defuse started 25s in: ends at 35s, fuse at 40s -> 5s to spare.
    const late = [{ tick: plantTick + 25 * T, event: 'defuse_start', name: 'Bo', has_kit: false }];
    const v1 = L.defuseVerdict(L.actionAt(late, plantTick + 26 * T, 0), plantTick);
    ok(v1.inTime, 'a defuse that finishes before the fuse is in time');
    near(v1.marginSeconds, 5, 'margin is fuse-end minus defuse-end');

    // Same defuse started 31s in: ends at 41s -> 1s short.
    const tooLate = [{ tick: plantTick + 31 * T, event: 'defuse_start', name: 'Bo', has_kit: false }];
    const v2 = L.defuseVerdict(L.actionAt(tooLate, plantTick + 32 * T, 0), plantTick);
    ok(!v2.inTime, 'a defuse that runs past the fuse is not in time');
    near(v2.marginSeconds, -1, 'a negative margin is how short it falls');

    // The kit is exactly what rescues it.
    const rescued = [{ tick: plantTick + 31 * T, event: 'defuse_start', name: 'Bo', has_kit: true }];
    ok(L.defuseVerdict(L.actionAt(rescued, plantTick + 32 * T, 0), plantTick).inTime,
       'the same moment with a kit finishes in time');

    eq(L.defuseVerdict(L.actionAt(late, plantTick + 26 * T, 0), null), null,
       'no plant tick means no verdict - never a guess');
    const plant = [{ tick: 1000, event: 'plant_begin', name: 'Ana' }];
    eq(L.defuseVerdict(L.actionAt(plant, 1050, 0), 900), null,
       'a plant has no defuse verdict');
}

// ── an action with no end event expires rather than hanging ─────────────────
// A defuser killed mid-defuse may leave no BombDefuseAborted behind. Without
// the nominal-duration cap the ring would stay pinned to a corpse all round.
{
    const bomb = [{ tick: 2000, event: 'defuse_start', name: 'Bo', has_kit: true }];
    ok(L.actionAt(bomb, 2000 + 4 * T, 0), 'still defusing inside the 5s');
    eq(L.actionAt(bomb, 2000 + 6 * T, 0), null,
       'an action with no end event expires at its nominal duration');
}

// ── round scoping, same convention as getBombState ──────────────────────────
{
    const bomb = [
        { tick: 100, event: 'defuse_start', name: 'Old', has_kit: true },
        { tick: 9000, event: 'plant_begin', name: 'Ana' },
    ];
    eq(L.actionAt(bomb, 9020, 8000).kind, 'plant',
       'events before the round start are ignored');
    eq(L.actionAt(bomb, 150, 8000), null,
       'a previous round cannot bleed an action into this one');
}

// ── measure(): the constants are checkable, not merely asserted ─────────────
{
    const bomb = [
        { tick: 0, event: 'plant_begin', name: 'Ana' },
        { tick: L.PLANT_TICKS, event: 'plant', name: 'Ana' },
        { tick: 1000, event: 'defuse_start', name: 'Bo', has_kit: true },
        { tick: 1000 + 5 * T, event: 'defuse', name: 'Bo' },
        { tick: 3000, event: 'defuse_start', name: 'Cy', has_kit: false },
        { tick: 3100, event: 'defuse_abort', name: 'Cy' },
    ];
    const m = L.measure(bomb);
    near(m.plant[0], L.PLANT_SECONDS, 'a completed plant yields its real duration');
    near(m.defuseKit[0], 5, 'a completed kit defuse yields its real duration');
    eq(m.defuse, [], 'an aborted defuse teaches no duration');
}

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
