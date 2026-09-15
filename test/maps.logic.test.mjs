/**
 * Unit tests for map-name resolution (static/maps.logic.js).
 * Run: node test/maps.logic.test.mjs   (no deps, no browser)
 *
 * Covers the three names one map has - raw, canonical, label - and the
 * legacy `de_dust` → `de_dust2` alias that keeps matches parsed by earlier
 * versions grouping and rendering as Dust II. The last block asserts this
 * module and its server-side twin maps.py carry the same tables, since the
 * two are duplicated by necessity.
 */
// maps.logic.js is a UMD classic script (loaded via <script src> in every page
// that names a map), so load it as CJS here - same pattern as the other logic
// module tests.
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
const require = createRequire(import.meta.url);
const M = require('../static/maps.logic.js');

let passed = 0, failed = 0;
const ok = (cond, msg) => { if (cond) passed++; else { failed++; console.error('  ✗ FAIL:', msg); } };
const eq = (a, b, msg) => ok(a === b, `${msg} (got ${JSON.stringify(a)}, want ${JSON.stringify(b)})`);
const eqArr = (a, b, msg) => ok(JSON.stringify(a) === JSON.stringify(b), `${msg} (got ${JSON.stringify(a)}, want ${JSON.stringify(b)})`);

// ── canonical: the legacy alias, and passthrough for everything else ──────────
eq(M.canonical('de_dust'), 'de_dust2', 'legacy de_dust folds onto de_dust2');
eq(M.canonical('de_dust2'), 'de_dust2', 'canonical name is a fixed point');
eq(M.canonical('DE_DUST'), 'de_dust2', 'canonical is case-insensitive');
eq(M.canonical('  de_dust  '), 'de_dust2', 'canonical trims');
eq(M.canonical('de_mirage'), 'de_mirage', 'unaliased map passes through');
eq(M.canonical('de_somecommunitymap'), 'de_somecommunitymap', 'unknown map passes through');

// ── canonical: totality - no input shape throws ───────────────────────────────
eq(M.canonical(null), '', 'null → empty');
eq(M.canonical(undefined), '', 'undefined → empty');
eq(M.canonical(''), '', 'empty → empty');

// ── aliasesOf: every stored spelling, canonical first ─────────────────────────
eqArr(M.aliasesOf('de_dust2'), ['de_dust2', 'de_dust'], 'dust2 knows its legacy spelling');
eqArr(M.aliasesOf('de_dust'), ['de_dust2', 'de_dust'], 'asking by the legacy name gives the same list');
eqArr(M.aliasesOf('de_mirage'), ['de_mirage'], 'unaliased map is alone in its list');

// ── label: the readable name ──────────────────────────────────────────────────
eq(M.label('de_dust2'), 'Dust 2', 'dust2 is spelled "Dust 2", not "Dust2"');
eq(M.label('de_dust'), 'Dust 2', 'a legacy-named match reads as Dust 2 too');
eq(M.label('de_mirage'), 'Mirage', 'prefix stripped and capitalised');
eq(M.label('de_ancient'), 'Ancient', 'ancient');
eq(M.label('cs_office'), 'Office', 'cs_ prefix stripped');
eq(M.label('ar_baggage'), 'Baggage', 'ar_ prefix stripped');
eq(M.label('de_train_night'), 'Train Night', 'underscores become spaces');
eq(M.label('workshop_thing'), 'Workshop Thing', 'unknown prefix is kept as a word');
eq(M.label(''), '', 'empty label for an empty name');
eq(M.label(null), '', 'null never throws');
eq(M.labelUpper('de_dust'), 'DUST 2', 'hero band gets the uppercase form');

// ── radarUrl: always the alias-resolving route, never /static/map directly ────
eq(M.radarUrl('de_dust'), '/radar/de_dust2.png', 'legacy name asks for the canonical radar');
eq(M.radarUrl('de_dust2'), '/radar/de_dust2.png', 'canonical name asks for the same radar');
eq(M.radarUrl('de_mirage'), '/radar/de_mirage.png', 'unaliased radar');
ok(M.radarUrl('de_nuke').indexOf('/static/map/') < 0,
   'radar URLs go through the route so the alias can be resolved server-side');

// ── parity with maps.py, which must carry the same tables ─────────────────────
// The two files are duplicated because the browser cannot import Python; this
// reads the module as text rather than running it, so the test needs no
// Python interpreter.
const py = readFileSync(new URL('../maps.py', import.meta.url), 'utf8');
Object.keys(M.MAP_ALIASES).forEach(raw => {
    ok(new RegExp(`'${raw}':\\s*'${M.MAP_ALIASES[raw]}'`).test(py),
       `maps.py carries the same alias ${raw} → ${M.MAP_ALIASES[raw]}`);
});
Object.keys(M.MAP_LABELS).forEach(canon => {
    ok(new RegExp(`'${canon}':\\s*'${M.MAP_LABELS[canon]}'`).test(py),
       `maps.py carries the same label ${canon} → ${M.MAP_LABELS[canon]}`);
});
ok(/MAP_PREFIXES\s*=\s*\('de_', 'cs_', 'ar_'\)/.test(py),
   'maps.py strips the same name prefixes');

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
