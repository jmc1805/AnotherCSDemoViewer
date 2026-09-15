/**
 * Unit tests for the 2D-pages chunk logic (static/chunks.logic.js):
 * v2-decoded-chunk → JSON-chunk-shaped tick dict, blind/flash reconstruction,
 * chunk-index preference, effective tick step.
 * Run: node test/chunks.logic.test.mjs   (no deps, no browser)
 */
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const {
    unifiedIndex, effectiveStep, buildBlindIndex, flashAt, buildRoundLookup, v2ToTickDict,
} = require('../static/chunks.logic.js');

let passed = 0, failed = 0;
const approx = (a, b, eps = 1e-3) => Math.abs(a - b) <= eps;
function ok(cond, msg) { if (cond) passed++; else { failed++; console.error('  ✗ FAIL:', msg); } }
function eq(a, b, msg) { ok(a === b, `${msg} (got ${JSON.stringify(a)}, want ${JSON.stringify(b)})`); }
function near(a, b, msg, eps) { ok(approx(a, b, eps), `${msg} (got ${a}, want ~${b})`); }

// ── unifiedIndex ─────────────────────────────────────────────────────────────
{
    const v2 = [{ file: 'a.bin.br' }], v1 = [{ file: 'a.json' }];
    eq(unifiedIndex({ chunkIndexV2: v2, chunkIndex: v1 }).isV2, true, 'prefers v2 when present');
    eq(unifiedIndex({ chunkIndexV2: v2, chunkIndex: v1 }).entries, v2, 'v2 entries returned');
    eq(unifiedIndex({ chunkIndexV2: [], chunkIndex: v1 }).isV2, false, 'empty v2 → JSON path');
    eq(unifiedIndex({ chunkIndex: v1 }).entries, v1, 'JSON-only match');
    eq(unifiedIndex({}).entries.length, 0, 'neither index → empty entries');
}

// ── effectiveStep ────────────────────────────────────────────────────────────
eq(effectiveStep({ tickStep: 1 }, true), 2, 'v2 doubles tickStep (posStep=2)');
eq(effectiveStep({ tickStep: 1 }, false), 1, 'JSON path keeps tickStep');
eq(effectiveStep({}, true), 2, 'missing tickStep defaults to 1');

// ── blind index / flashAt ────────────────────────────────────────────────────
{
    const idx = buildBlindIndex([
        { tick: 1000, victim_name: 'bob', blind_duration: 2.0 },
        { tick: 1064, victim_name: 'bob', blind_duration: 3.0 },   // overlapping re-flash
        { tick: 1000, victim_name: 'alice', blind_duration: 0.5 },
        { tick: 9000, victim_name: 'bob', blind_duration: 1.0 },
    ]);
    eq(flashAt(idx, 'bob', 999), 0, 'before any flash → 0');
    near(flashAt(idx, 'bob', 1000), 2.0, 'full duration at flash tick');
    near(flashAt(idx, 'bob', 1064), 3.0, 'overlap → max remaining wins');
    near(flashAt(idx, 'bob', 1128), 2.0, '1s into the 3s flash');
    eq(flashAt(idx, 'bob', 1064 + 3 * 64 + 1) <= 0, true, 'expired → ≤0');
    near(flashAt(idx, 'alice', 1016), 0.25, 'per-victim isolation');
    eq(flashAt(idx, 'carol', 1000), 0, 'unknown player → 0');
    near(flashAt(idx, 'bob', 9032), 0.5, 'later event unaffected by early ones');
    eq(flashAt(buildBlindIndex(null), 'bob', 0), 0, 'null events → 0');
}

// ── buildRoundLookup ─────────────────────────────────────────────────────────
{
    const at = buildRoundLookup([
        { round_num: 2, start: 5000 }, { round_num: 1, start: 100 },   // unsorted on purpose
    ]);
    eq(at(50), null, 'before round 1');
    eq(at(100), 1, 'round 1 start');
    eq(at(4999), 1, 'still round 1');
    eq(at(9999), 2, 'round 2');
    eq(buildRoundLookup(null)(50), null, 'null rounds → null');
}

// ── v2ToTickDict ─────────────────────────────────────────────────────────────
// Hand-built decoded chunk: 2 snapshots, 2 player slots; slot 1 absent in snap 2.
// Quant frame X:[0,1000] Y:[-500,500] Z:[10,10] (zero span → span guard).
function q(v, min, max) { let s = max - min; if (s <= 0) s = 1; return Math.round((v - min) / s * 65535); }
const chunk = {
    quant: { minX: 0, maxX: 1000, minY: -500, maxY: 500, minZ: 10, maxZ: 10 },
    players: [{ name: 'bob', side: 'ct' }, { name: 'alice', side: 't' }],
    weapons: ['-', 'weapon_ak47', 'weapon_usps', 'weapon_flashbang'],
    snapTicks: Uint32Array.from([1000, 1002]),
    presence: Uint32Array.from([0b11, 0b01]),
    recOff: Uint32Array.from([0, 2, 3]),
    posX: Uint16Array.from([q(250, 0, 1000), q(999, 0, 1000), q(300, 0, 1000)]),
    posY: Uint16Array.from([q(-100, -500, 500), q(0, -500, 500), q(-100, -500, 500)]),
    posZ: Uint16Array.from([0, 65535, 0]),
    eyeZ: Uint16Array.from([0, 0, 0]),
    yaw: Uint16Array.from([
        Math.round(356 / 360 * 65536) & 0xffff,   // -4° in JSON convention
        Math.round(90 / 360 * 65536),
        0,
    ]),
    pitch: Uint16Array.from([Math.round((0 + 90) / 180 * 65535), 0, 65535]),
    health: Uint8Array.from([100, 0, 87]),
    armor: Uint8Array.from([50, 0, 100]),
    money: Uint16Array.from([800, 0, 3400]),
    //            bob: helmet+defuse (ct)   alice: hasC4 + side T      bob snap2: plain ct
    flags: Uint16Array.from([(1 << 2) | (1 << 3), (1 << 1) | (1 << 5), 0]),
    weapon: Uint8Array.from([1, 2, 0]),
    inventory: Uint32Array.from([0b1010, 0b1100, 0b0001]),   // bit0 sentinel must be ignored
    // Fixed-point *8 u/s (chunks_v2.go quantVel): bob@1000 (30,40)->speed 50,
    // alice@1000 (135,0)->speed 135, bob@1002 (0,0)->speed 0.
    velX: Int16Array.from([30 * 8, 135 * 8, 0]),
    velY: Int16Array.from([40 * 8, 0, 0]),
};
const blinds = buildBlindIndex([{ tick: 980, victim_name: 'bob', blind_duration: 1.0 }]);
const roundAt = buildRoundLookup([{ round_num: 3, start: 900 }]);
const dict = v2ToTickDict(chunk, blinds, roundAt);

{
    eq(Object.keys(dict).join(','), '1000,1002', 'dict keyed by snapshot ticks (strings)');
    const [bob, alice] = dict['1000'];
    eq(bob.name, 'bob', 'slot order ascending: slot 0 first');
    eq(alice.name, 'alice', 'slot 1 second');

    near(bob.X, 250, 'X dequantized', 0.1);
    near(bob.Y, -100, 'Y dequantized', 0.1);
    near(bob.Z, 10, 'zero-span Z → min (span guard)', 0.1);
    near(alice.X, 999, 'X near max', 0.1);
    near(bob.yaw, -4, 'yaw wrapped to -180..180', 0.01);
    near(alice.yaw, 90, 'yaw 90° stays positive', 0.01);
    near(bob.pitch, 0, 'pitch midpoint → 0', 0.01);

    eq(bob.side, 'ct', 'flags bit5 clear → ct');
    eq(alice.side, 't', 'flags bit5 set → t');
    eq(bob.helmet, true, 'helmet flag');
    eq(bob.defuse_kit, true, 'defuse flag');
    eq(bob.has_c4, false, 'no C4');
    eq(alice.has_c4, true, 'C4 flag');

    eq(bob.health, 100, 'health passthrough');
    eq(alice.health, 0, 'dead player still present (death marker)');
    eq(bob.money, 800, 'money passthrough');
    eq(alice.armor, 0, 'armor passthrough');

    eq(bob.active_weapon, 'weapon_ak47', 'active weapon via table');
    eq(alice.active_weapon, 'weapon_usps', 'active weapon slot 2');
    eq(bob.inventory.join(','), 'weapon_ak47,weapon_flashbang', 'inventory bits→names');
    eq(alice.inventory.join(','), 'weapon_usps,weapon_flashbang', 'inventory decode 2');

    near(bob.speed, 50, 'speed = hypot(velX,velY)/8', 1e-6);
    near(alice.speed, 135, 'speed at the CS2 silent-movement threshold', 1e-6);

    near(bob.flash_duration, 1.0 - 20 / 64, 'flash remaining from blind event', 1e-6);
    eq(alice.flash_duration, 0, 'unflashed player 0');
    eq(bob.round_num, 3, 'round_num from rounds lookup');

    const snap2 = dict['1002'];
    eq(snap2.length, 1, 'absent slot omitted in snapshot 2');
    eq(snap2[0].name, 'bob', 'present slot decoded');
    eq(snap2[0].active_weapon, '', 'weapon id 0 ("-") → empty active weapon');
    eq(snap2[0].inventory.length, 0, 'sentinel-only inventory → empty');
    near(snap2[0].pitch, 90, 'pitch max → +90 (Source positive-down)', 0.01);
}

// null blind/round helpers → safe defaults
{
    const d = v2ToTickDict(chunk, null, null);
    eq(d['1000'][0].flash_duration, 0, 'no blindIndex → flash 0');
    eq(d['1000'][0].round_num, null, 'no round lookup → null');
}

console.log(`\nchunks.logic: ${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
