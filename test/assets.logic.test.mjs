/**
 * Unit tests for the CS2 UI asset resolution logic (static/assets.logic.js).
 * Run: node test/assets.logic.test.mjs   (no deps, no browser)
 *
 * Covers the two things that must stay correct regardless of the exact VPK
 * filenames: key-candidate generation (weapon_/map/skillgroup normalization +
 * aliases) and resolve()'s most-specific-first lookup with a null fallback.
 *
 * Also covers the Premier banner's rating->tier banding, which is a
 * transcription of the game's GetClampedRating and must agree with
 * MatchHeroLogic.premierTier's chip classes. The multiply that bakes the seven
 * tier files is assetindex.py's, tested by test/assetindex_test.py.
 */
// assets.logic.js is a UMD classic script (loaded via <script src> in the 2D
// pages), so load it as CJS here - same pattern as test/chunks.logic.test.mjs.
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { candidates, resolve, skillGroupKey, premierTier, premierBannerKey } = require('../static/assets.logic.js');
const MatchHeroLogic = require('../static/matchhero.logic.js');

let passed = 0, failed = 0;
const ok = (cond, msg) => { if (cond) passed++; else { failed++; console.error('  ✗ FAIL:', msg); } };
const eq = (a, b, msg) => ok(a === b, `${msg} (got ${JSON.stringify(a)}, want ${JSON.stringify(b)})`);
const eqArr = (a, b, msg) => ok(JSON.stringify(a) === JSON.stringify(b), `${msg} (got ${JSON.stringify(a)}, want ${JSON.stringify(b)})`);

// ── candidates: equipment strips weapon_ and expands aliases ──────────────────
eqArr(candidates('equipment', 'weapon_ak47'), ['weapon_ak47', 'ak47'], 'equipment weapon_ prefix + bare');
eqArr(candidates('equipment', 'AK47'), ['ak47'], 'equipment is case-insensitive, bare key');
ok(candidates('equipment', 'weapon_usps').includes('weapon_usp_silencer'), 'usps aliases to usp_silencer');
ok(candidates('equipment', 'weapon_incgrenade').includes('weapon_molotov'), 'inc grenade aliases to molotov');

// ── candidates: map names resolve canonical-first ─────────────────────────────
// Order matters here, it is not cosmetic: the VPK ships art for both de_dust
// (Dust) and de_dust2 (Dust II), so a match stored under the legacy `de_dust`
// spelling must reach for Dust II's icon first or it renders the wrong map.
eqArr(candidates('map_icons', 'de_dust'), ['de_dust2', 'de_dust'],
      'legacy de_dust asks for the Dust II icon before the Dust one');
eqArr(candidates('map_icons', 'de_dust2'), ['de_dust2'], 'canonical name needs no fallback');
eqArr(candidates('overheadmaps', 'de_dust'), ['de_dust2', 'de_dust'],
      'overhead maps canonicalise the same way');
eqArr(candidates('overheadmaps', 'de_mirage'), ['de_mirage'], 'unaliased map passthrough');

// ── candidates: totality (null/empty never throws) ────────────────────────────
eqArr(candidates('equipment', null), [], 'null key → empty');
eqArr(candidates('map_icons', '   '), [], 'blank key → empty');

// ── resolve: most-specific-first, null fallback ───────────────────────────────
const M = { kinds: {
    equipment: { 'ak47': 'equipment/ak47.png', 'weapon_ak47': 'equipment/full_ak47.png' },
    map_icons: { 'de_dust2': 'map_icons/de_dust2.png' },
    skillgroups: { '7': 'skillgroups/skillgroup7.png' },
} };
eq(resolve(M, 'equipment', 'weapon_ak47'), 'equipment/full_ak47.png', 'prefers the more specific weapon_ key');
eq(resolve(M, 'equipment', 'ak47'), 'equipment/ak47.png', 'bare key resolves');
eq(resolve(M, 'map_icons', 'de_dust'), 'map_icons/de_dust2.png', 'aliased map resolves');
eq(resolve(M, 'skillgroups', '7'), 'skillgroups/skillgroup7.png', 'skillgroup int resolves');
eq(resolve(M, 'equipment', 'weapon_awp'), null, 'unknown key → null');
eq(resolve(M, 'grenades', 'smoke'), null, 'unknown kind → null');
eq(resolve(null, 'equipment', 'ak47'), null, 'null manifest → null');

// ── skillGroupKey: only Competitive/Wingman get an icon key ───────────────────
eq(skillGroupKey({ rank_type: 12, rank: 7 }), '7', 'competitive rank 7 → "7"');
eq(skillGroupKey({ rank_type: 7, rank: 18 }), '18', 'wingman rank 18 → "18"');
eq(skillGroupKey({ rank_type: 11, rank: 15000 }), null, 'premier → null (rating number, no icon)');
eq(skillGroupKey({ rank_type: 12, rank: 0 }), null, 'rank 0 / unranked → null');
eq(skillGroupKey(null), null, 'null entry → null');

// ── premierTier: the game's own bands (rating_emblem.vts_c GetClampedRating) ──
eq(premierTier(0), 0, 'rating 0 -> tier 0');
eq(premierTier(4999), 0, 'just under the first boundary stays tier 0');
eq(premierTier(5000), 1, 'exactly 5000 promotes to tier 1');
eq(premierTier(29999), 5, 'just under 30k is still red');
eq(premierTier(30000), 6, 'exactly 30000 is gold');
eq(premierTier(250000), 6, 'tier is clamped at 6, never 7+');
eq(premierTier(-500), 0, 'a negative rating clamps to 0 rather than going negative');

// The banded tier and the chip class are two spellings of one rule and must not
// drift - that drift would show as a banner in one colour behind a number in
// another. Walk every boundary rather than spot-checking.
const CHIP_BY_TIER = ['p-gray', 'p-lblue', 'p-blue', 'p-purple', 'p-pink', 'p-red', 'p-gold'];
for (const r of [0, 1, 4999, 5000, 9999, 10000, 14999, 15000, 19999, 20000, 24999, 25000, 29999, 30000, 99999]) {
    eq(CHIP_BY_TIER[premierTier(r)], MatchHeroLogic.premierTier(r),
       `tier band and chip class agree at rating ${r}`);
}

// ── premierBannerKey: 'none' is a distinct banner, not tier 0 ─────────────────
eq(premierBannerKey(0), 'none', 'no rating -> the unrated banner');
eq(premierBannerKey(null), 'none', 'null rating -> the unrated banner');
eq(premierBannerKey(undefined), 'none', 'undefined rating -> the unrated banner');
eq(premierBannerKey(3200), '0', 'a real low rating is tier 0, NOT none');
eq(premierBannerKey(31200), '6', 'a top rating resolves to tier 6');

// ── premier candidates/resolve ────────────────────────────────────────────────
eqArr(candidates('premier', '6'), ['6'], 'premier keys are already canonical');
const PM = { kinds: { premier: { '6': 'premier/premier_tier6.svg', 'none': 'premier/premier_none.svg' } } };
eq(resolve(PM, 'premier', premierBannerKey(31200)), 'premier/premier_tier6.svg', 'gold rating resolves to the gold banner');
eq(resolve(PM, 'premier', premierBannerKey(0)), 'premier/premier_none.svg', 'unrated resolves to the unrated banner');
eq(resolve(PM, 'premier', premierBannerKey(12100)), null, 'an unextracted tier -> null, not a wrong banner');

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
