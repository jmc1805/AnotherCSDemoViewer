/**
 * Unit tests for the shared match-hero band logic (static/matchhero.logic.js):
 * average-rank aggregation and the clamped-damage replay that backs ADR/rating
 * on the stats page and the "total dmg" pill on the viewer/multi-round pages.
 * Run: node test/matchhero.logic.test.mjs   (no deps, no browser)
 */
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
const require = createRequire(import.meta.url);
const { CS_SKILL_GROUPS, premierTier, rankTier, computeAvgRank,
        forEachClampedHit, totalDamage, modeLabel, premierRanksPresent, DEMO_DECIDED_MODES,
        PREMIER_RANK_TYPE } = require('../static/matchhero.logic.js');

let passed = 0, failed = 0;
const ok = (cond, msg) => { if (cond) passed++; else { failed++; console.error('  ✗ FAIL:', msg); } };
const eq = (a, b, msg) => ok(a === b, `${msg} (got ${JSON.stringify(a)}, want ${JSON.stringify(b)})`);

// ── premierTier / rankTier ─────────────────────────────────────
eq(premierTier(0), 'p-gray', '0 rating is the grey tier');
eq(premierTier(4999), 'p-gray', 'just under 5k stays grey');
eq(premierTier(5000), 'p-lblue', '5k is the light-blue boundary');
eq(premierTier(9999), 'p-lblue', 'just under 10k stays light blue');
eq(premierTier(10000), 'p-blue', '10k is the blue boundary');
eq(premierTier(15000), 'p-purple', '15k is the purple boundary');
eq(premierTier(20000), 'p-pink', '20k is the pink boundary');
eq(premierTier(25000), 'p-red', '25k is the red boundary');
eq(premierTier(30000), 'p-gold', '30k+ is gold');
eq(premierTier(undefined), 'p-gray', 'missing rating falls back to grey');

eq(rankTier(11, 17246), 'p-purple', 'premier rank uses its rating tier');
eq(rankTier(12, 15), 'sg', 'competitive skill group uses the shared gold chip');
eq(rankTier(7, 15), 'sg', 'wingman skill group uses the shared gold chip');
eq(rankTier(0, 0), '', 'unknown rank type has no tier');

// ── computeAvgRank ─────────────────────────────────────────────────────────
eq(computeAvgRank([]), null, 'no ranks → null');
eq(computeAvgRank([{ rank_type: 12, rank: 0 }]), null, 'all-zero ranks → null');
{
    const ar = computeAvgRank([{ rank_type: 11, rank: 15000 }, { rank_type: 11, rank: 17000 }]);
    eq(ar.rankType, 11, 'premier: picks the dominant rank_type');
    eq(ar.roundAvg, 16000, 'premier: averages the raw rating');
    eq(ar.label, '16,000', 'premier: label is the localized rating number');
}
{
    // Dominant type is Competitive (3 samples) even though Premier appears too.
    const ar = computeAvgRank([
        { rank_type: 12, rank: 8 }, { rank_type: 12, rank: 10 }, { rank_type: 12, rank: 9 },
        { rank_type: 11, rank: 20000 },
    ]);
    eq(ar.rankType, 12, 'mixed types: majority rank_type wins');
    eq(ar.roundAvg, 9, 'mixed types: averages only within the dominant type');
    eq(ar.label, CS_SKILL_GROUPS[9], 'competitive: label is the skill-group name');
}
eq(computeAvgRank([{ rank_type: 7, rank: 5 }]).label, CS_SKILL_GROUPS[5] + ' (WM)', 'wingman: label suffixed "(WM)"');

// ── forEachClampedHit / totalDamage: the overkill-clamp fix ────────────────
{
    // A single life takes 3 shots that would raw-sum past 100 HP; only the
    // health actually removed (100 total) should count, split 27+27+46 across
    // the three hits that actually landed before death.
    const dmg = [
        { round_num: 1, victim_name: 'V', attacker_name: 'A', tick: 1, dmg_health: 27 },
        { round_num: 1, victim_name: 'V', attacker_name: 'A', tick: 2, dmg_health: 27 },
        { round_num: 1, victim_name: 'V', attacker_name: 'A', tick: 3, dmg_health: 108 }, // overkill hit
    ];
    const hits = [];
    forEachClampedHit(dmg, (d, actual) => hits.push(actual));
    eq(JSON.stringify(hits), JSON.stringify([27, 27, 46]), 'overkill hit is clamped to health remaining, not raw dmg');
    eq(totalDamage(dmg), 100, 'total damage for one life never exceeds 100');
}
{
    // Two separate lives (different rounds) each take a 90-dmg hit twice -
    // clamped total is 100 per life = 200, not the raw 360.
    const dmg = [
        { round_num: 1, victim_name: 'V', attacker_name: 'A', tick: 1, dmg_health: 90 },
        { round_num: 1, victim_name: 'V', attacker_name: 'A', tick: 2, dmg_health: 90 },
        { round_num: 2, victim_name: 'V', attacker_name: 'A', tick: 1, dmg_health: 90 },
        { round_num: 2, victim_name: 'V', attacker_name: 'A', tick: 2, dmg_health: 90 },
    ];
    eq(totalDamage(dmg), 200, 'separate rounds are separate lives, each clamped independently');
}
eq(totalDamage([{ round_num: 1, victim_name: 'V', attacker_name: 'V', tick: 1, dmg_health: 50 }]), 0, 'self-damage is excluded');
eq(totalDamage([{ round_num: 1, victim_name: 'V', attacker_name: null, tick: 1, dmg_health: 50 }]), 0, 'no attacker is excluded');
eq(totalDamage([]), 0, 'empty input → 0');
eq(totalDamage(undefined), 0, 'undefined input → 0');

// ── parity with match_summary.py, which carries the same skill-group names ────
// The two are duplicated because the browser cannot import Python, so a rank
// could otherwise be named one thing server-side and another in the page. This
// reads the module as text rather than running it, so no interpreter is needed.
{
    const py = readFileSync(new URL('../match_summary.py', import.meta.url), 'utf8');
    const block = py.slice(py.indexOf('_COMP_RANK_NAMES = {'));
    const names = {};
    for (const [, n, label] of block.slice(0, block.indexOf('}')).matchAll(/(\d+):\s*"([^"]+)"/g)) {
        names[+n] = label;
    }
    eq(Object.keys(names).length, 18, 'match_summary.py names all 18 skill groups');
    eq(CS_SKILL_GROUPS[0], 'Unranked', 'index 0 is the unranked placeholder, so rank n is index n');
    eq(CS_SKILL_GROUPS.length, 19, 'placeholder plus the 18 groups');
    let agree = true;
    for (let n = 1; n <= 18; n++) if (CS_SKILL_GROUPS[n] !== names[n]) {
        agree = false;
        console.error(`  ✗ FAIL: rank ${n} is "${CS_SKILL_GROUPS[n]}" here, "${names[n]}" in match_summary.py`);
    }
    ok(agree, 'every skill group is named identically on both sides');
}

// ── modeLabel: the demo's own evidence beats the filename token ───────────────
eq(modeLabel('mm', [{ rank_type: 11, rank: 21040 }]), 'PREM',
   "a premier rating overrides an 'mm' filename token");
eq(modeLabel('', [{ rank_type: 11, rank: 21040 }]), 'PREM', 'premier wins with no token at all');
eq(modeLabel('mm', [{ rank_type: 12, rank: 17 }]), 'MM', 'competitive keeps the token');
eq(modeLabel('mm', []), 'MM', 'no ranks keeps the token');
eq(modeLabel('mm', undefined), 'MM', 'undefined ranks keeps the token');
eq(modeLabel('wingman', null), 'WINGMAN', 'the token is upper-cased as-is');
eq(modeLabel('', null), '', 'no token and no ranks stays empty');

// ANY premier rating counts, not the dominant type - rank rows are absent for
// unranked players, so a premier match can surface exactly one rating.
eq(modeLabel('mm', [{ rank_type: 12, rank: 15 }, { rank_type: 12, rank: 16 },
                    { rank_type: 11, rank: 10589 }]), 'PREM',
   'one premier rating beats a competitive majority');

// rank 0 is 'unranked', not a rating.
// FACEIT comes from the demo's own server name, so it is not a default for the
// Premier check to correct - twin of match_summary.DEMO_DECIDED_MODES.
eq(modeLabel('faceit', []), 'FACEIT', 'the faceit token labels FACEIT');
eq(modeLabel('faceit', [{ rank_type: 11, rank: 21040 }]), 'FACEIT',
   'where the match was played beats a Premier rating in it');
eq(modeLabel('FaceIt', null), 'FACEIT', 'the token is matched case-insensitively');

eq(modeLabel('mm', [{ rank_type: 11, rank: 0 }]), 'MM',
   'rank_type 11 with rank 0 is unranked, not a premier rating');
ok(!premierRanksPresent([{ rank_type: 11 }]), 'a rank_type with no rank value is not a rating');
ok(!premierRanksPresent(null), 'null ranks is not a rating');
ok(!premierRanksPresent([null, 'junk', 7]), 'malformed rank rows are ignored, not thrown on');
eq(PREMIER_RANK_TYPE, 11, 'premier rank_type constant');

// ── the Python twin must agree (match_summary.py) ─────────────────────────────
{
    const py = readFileSync(new URL('../match_summary.py', import.meta.url), 'utf8');
    const m = py.match(/PREMIER_RANK_TYPE\s*=\s*(\d+)/);
    ok(m && Number(m[1]) === PREMIER_RANK_TYPE,
       `PREMIER_RANK_TYPE is ${PREMIER_RANK_TYPE} here and ${m ? m[1] : '(absent)'} in match_summary.py`);
    // The dashboard labels server-side and every other page labels client-side;
    // one saying PREM while the other says MM is the bug this replaced.
    ok(/def premier_ranks_present\(/.test(py) && /def mode_label\(/.test(py),
       'match_summary.py still exposes the twin helpers this mirrors');
    ok(/'PREM'/.test(py), 'match_summary.py emits the same PREM literal');

    // The two lists of demo-decided modes must hold the same names, or one page
    // says FACEIT while another says PREM about the same match.
    const pyModes = py.match(/DEMO_DECIDED_MODES\s*=\s*\(([^)]*)\)/);
    const pyNames = pyModes ? [...pyModes[1].matchAll(/'([^']+)'/g)].map(m => m[1]) : null;
    ok(pyNames && pyNames.join(',') === DEMO_DECIDED_MODES.join(','),
       `DEMO_DECIDED_MODES is [${DEMO_DECIDED_MODES}] here and [${pyNames}] in match_summary.py`);
}

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
