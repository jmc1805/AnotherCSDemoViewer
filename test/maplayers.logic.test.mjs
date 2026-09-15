/**
 * Unit tests for the multi-level map split (static/maplayers.logic.js).
 * Run: node test/maplayers.logic.test.mjs   (no deps, no browser)
 *
 * What matters: single-level maps are left completely alone, a position with
 * no Z is never assigned a floor (old parses), the automatic mode doesn't
 * flip-flop on a tie, and off-floor players are dimmed rather than dropped.
 */
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const L = require('../static/maplayers.logic.js');

let passed = 0, failed = 0;
const ok = (c, m) => { if (c) passed++; else { failed++; console.error('  ✗ FAIL:', m); } };
const eq = (a, b, m) => ok(a === b, `${m} (got ${JSON.stringify(a)}, want ${JSON.stringify(b)})`);

const alive = (z) => ({ Z: z, health: 100 });
const dead = (z) => ({ Z: z, health: 0 });

// ── which maps are layered ───────────────────────────────────────────────────
{
    ok(L.isLayered('de_nuke'), 'nuke is layered');
    ok(L.isLayered('de_vertigo'), 'vertigo is layered');
    ok(L.isLayered('de_train'), 'train is layered (verticalsections, split -50)');
    ok(!L.isLayered('de_dust2'), 'dust2 is not');
    ok(!L.isLayered('de_mirage'), 'mirage is not');
    ok(!L.isLayered(''), 'no map name is not layered');
    ok(!L.isLayered(undefined), 'undefined is not layered');
    // A radar image on disk is not enough - the map must declare a
    // verticalsections block. de_overpass ships no _lower.png and no block.
    ok(!L.isLayered('de_overpass'), 'a map with no verticalsections block is not offered');
}

// ── layerOf ──────────────────────────────────────────────────────────────────
{
    eq(L.layerOf('de_nuke', 0), 'upper', 'nuke A site is upper');
    eq(L.layerOf('de_nuke', -400), 'upper', 'just above the split is upper');
    eq(L.layerOf('de_nuke', -495), 'upper', 'exactly on the split is upper (>= )');
    eq(L.layerOf('de_nuke', -496), 'lower', 'just below the split is lower');
    eq(L.layerOf('de_nuke', -760), 'lower', 'nuke B site is lower');

    eq(L.layerOf('de_vertigo', 12000), 'upper', 'vertigo top floor');
    eq(L.layerOf('de_vertigo', 11000), 'lower', 'vertigo bottom floor');

    // Train's split is -50, not the ~-290 trough its Z histogram shows: the
    // bulk of the map sits below the raised middle platform.
    eq(L.layerOf('de_train', 0), 'upper', 'train platform is upper');
    eq(L.layerOf('de_train', -50), 'upper', 'exactly on the split is upper (>=)');
    eq(L.layerOf('de_train', -51), 'lower', 'just below the split is lower');
    eq(L.layerOf('de_train', -224), 'lower', 'train ground level is lower');

    eq(L.layerOf('de_dust2', -400), null, 'a single-level map has no floors');
    eq(L.layerOf('de_nuke', undefined), null, 'no Z -> unknown floor, not a guess');
    eq(L.layerOf('de_nuke', null), null, 'null Z -> unknown floor');
    eq(L.layerOf('de_nuke', NaN), null, 'NaN Z -> unknown floor');
    eq(L.layerOf('de_nuke', Infinity), null, 'infinite Z -> unknown floor');
}

// ── autoLayer: the majority vote ────────────────────────────────────────────
{
    eq(L.autoLayer('de_nuke', [alive(0), alive(0), alive(-700)]), 'upper', 'majority upstairs');
    eq(L.autoLayer('de_nuke', [alive(-700), alive(-700), alive(0)]), 'lower', 'majority downstairs');
    eq(L.autoLayer('de_dust2', [alive(0)]), null, 'single-level map has no automatic floor');

    // Corpses do not vote: four dead men in Vents is not a reason to follow the
    // camera down there while the round is being played on A site.
    eq(L.autoLayer('de_nuke', [alive(0), dead(-700), dead(-700), dead(-700)]), 'upper',
       'dead players do not vote');
    // ...but a player object with no health field still counts (the analyser's
    // rows carry positions without health).
    eq(L.autoLayer('de_nuke', [{ Z: -700 }, { Z: -700 }, { Z: 0 }]), 'lower',
       'players with no health field are counted');

    // Ties: hold the previous floor rather than flicker, and default to upper.
    eq(L.autoLayer('de_nuke', [alive(0), alive(-700)], 'lower'), 'lower', 'a tie keeps the current floor');
    eq(L.autoLayer('de_nuke', [alive(0), alive(-700)], 'upper'), 'upper', 'a tie keeps the current floor (upper)');
    eq(L.autoLayer('de_nuke', [alive(0), alive(-700)]), 'upper', 'a tie with no history resolves upward');

    // Nothing to go on.
    eq(L.autoLayer('de_nuke', []), 'upper', 'no players at all -> upper');
    eq(L.autoLayer('de_nuke', [], 'lower'), 'lower', 'no players at all -> keep what was shown');
    eq(L.autoLayer('de_nuke', [{}, null, { Z: undefined }]), 'upper', 'positionless players -> upper');
    eq(L.autoLayer('de_nuke', undefined), 'upper', 'undefined roster does not throw');

    // Lower-case z is accepted too (the analyser's moment rows use it).
    eq(L.autoLayer('de_nuke', [{ z: -700 }, { z: -700 }]), 'lower', 'accepts a lower-case z');
}

// ── resolve: mode -> floor ──────────────────────────────────────────────────
{
    const ps = [alive(-700), alive(-700), alive(0)];
    eq(L.resolve('de_nuke', 'auto', ps), 'lower', 'auto follows the majority');
    eq(L.resolve('de_nuke', 'upper', ps), 'upper', 'a manual choice overrides the majority');
    eq(L.resolve('de_nuke', 'lower', [alive(0), alive(0)]), 'lower', 'manual lower with everyone upstairs');
    eq(L.resolve('de_dust2', 'lower', ps), null, 'a manual choice means nothing on a flat map');
    eq(L.resolve('de_nuke', undefined, ps), 'lower', 'an unset mode behaves as auto');
    eq(L.resolve('de_nuke', 'nonsense', ps), 'lower', 'an unknown mode behaves as auto');
}

// ── alphaFor: off-floor players are dimmed, never dropped ───────────────────
{
    eq(L.alphaFor('de_nuke', 0, 'upper'), 1, 'on the shown floor: full strength');
    ok(L.alphaFor('de_nuke', -700, 'upper') < 1, 'off the shown floor: dimmed');
    ok(L.alphaFor('de_nuke', -700, 'upper') > 0, 'off the shown floor: still drawn, never hidden');
    eq(L.alphaFor('de_nuke', -700, 'lower'), 1, 'on the shown floor (lower): full strength');
    eq(L.alphaFor('de_nuke', 0, null), 1, 'no floor on show: everyone full strength');
    eq(L.alphaFor('de_dust2', 0, 'upper'), 1, 'flat map: everyone full strength');
    eq(L.alphaFor('de_nuke', undefined, 'upper'), 1,
       'unknown floor is drawn normally - an old parse must not dim its whole roster');
    eq(L.alphaFor('de_nuke', -700, 'upper', 0.5), 0.5, 'the dim level is caller-settable');
}

// ── radarName ───────────────────────────────────────────────────────────────
{
    eq(L.radarName('de_nuke', 'upper'), 'de_nuke', 'upper uses the base radar');
    eq(L.radarName('de_nuke', 'lower'), 'de_nuke_lower', 'lower uses the _lower radar');
    eq(L.radarName('de_nuke', null), 'de_nuke', 'no floor uses the base radar');
    eq(L.radarName('de_vertigo', 'lower'), 'de_vertigo_lower', 'vertigo lower');
    // Canonicalised first, same rule as every other asset lookup in the repo.
    eq(L.radarName('DE_NUKE', 'lower'), 'de_nuke_lower', 'case-insensitive');
}

// ── the thresholds themselves ───────────────────────────────────────────────
{
    // Corroborated by the corpus: the Nuke match's Z histogram is bimodal with
    // its gap at −512..−448.
    ok(L.LAYERS.de_nuke.split > -512 && L.LAYERS.de_nuke.split < -448,
       "nuke's split sits inside the gap the real data shows");
    eq(L.MODES.join(','), 'auto,upper,lower', 'the three UI modes');
}

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
