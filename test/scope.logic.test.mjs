/**
 * Unit tests for static/scope.logic.js - the Multi Round Analyser's "which
 * matches am I looking at" model.  Run: node test/scope.logic.test.mjs
 *
 * What matters here is that the page can never lie about its own scope:
 *  - the kind survives a URL round-trip, INCLUDING "all maps", which is the
 *    absence of every scope key and therefore the one that silently reverts if
 *    it isn't written down;
 *  - switching kinds never leaves the previous kind's key behind (a stale
 *    `files` under a map scope is a query that stayed narrow while the header
 *    claimed it had widened);
 *  - specs written before this model existed still resolve to a sensible kind;
 *  - nothing mutates the caller's spec - the panel keeps a reference to the
 *    live spec and re-renders from it.
 */
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const S = require('../static/scope.logic.js');
const Q = require('../static/query.logic.js');

let passed = 0, failed = 0;
const ok = (c, m) => { if (c) passed++; else { failed++; console.error('  ✗ FAIL:', m); } };
const eq = (a, b, m) => ok(a === b, `${m} (got ${JSON.stringify(a)}, want ${JSON.stringify(b)})`);
const deq = (a, b, m) => ok(JSON.stringify(a) === JSON.stringify(b),
    `${m} (got ${JSON.stringify(a)}, want ${JSON.stringify(b)})`);
// Scope objects are compared by content: which key was written first is an
// implementation detail, and pinning it makes every reordering a false alarm.
const sorted = o => (o && typeof o === 'object' && !Array.isArray(o))
    ? Object.keys(o).sort().reduce((acc, k) => (acc[k] = o[k], acc), {}) : o;
const seq = (a, b, m) => deq(sorted(a), sorted(b), m);

const ENTRY = 'de_mirage_20260609_1306_mm.json';
const OTHER = 'de_mirage_20260612_2220_mm.json';
const THIRD = 'de_mirage_20260617_1155_mm.json';
const OFFERED = [
    { file: ENTRY, label: '09 Jun 13:06', map: 'de_mirage' },
    { file: OTHER, label: '12 Jun 22:20', map: 'de_mirage' },
    { file: THIRD, label: '17 Jun 11:55', map: 'de_mirage' },
];
const CTX = { kind: 'match', files: [ENTRY], map: 'de_mirage', entry: ENTRY };
const SET_CTX = { kind: 'set', files: [ENTRY, OTHER], map: 'de_mirage', entry: ENTRY };

// ── ctx normalisation ─────────────────────────────────────────────────────────
{
    const c = S.normalizeCtx(null);
    eq(c.kind, 'map', 'a missing context falls back to a map scope');
    deq(c.files, [], 'and to no files');
    eq(S.normalizeCtx({ kind: 'nonsense' }).kind, 'map', 'an unknown kind is not trusted');
    const src = { kind: 'set', files: [ENTRY] };
    S.normalizeCtx(src).files.push('x');
    deq(src.files, [ENTRY], 'the caller\'s context is not mutated');
}

// ── applyKind ─────────────────────────────────────────────────────────────────
{
    const base = Q.emptySpec();
    seq(S.applyKind(base, 'match', CTX).scope, { kind: 'match', files: [ENTRY] },
        'this match scopes to the entry file');
    seq(S.applyKind(base, 'set', SET_CTX).scope, { kind: 'set', files: [ENTRY, OTHER] },
        'the set scopes to the files the page arrived with');
    seq(S.applyKind(base, 'map', CTX).scope, { kind: 'map', maps: ['de_mirage'] },
        'the map scope names the map');
    deq(S.applyKind(base, 'all', CTX).scope, { kind: 'all' },
        'all maps carries the sentinel and nothing else');

    // The stale-key rule: this is the bug the old single-key handler had.
    const narrow = S.applyKind(base, 'match', CTX);
    const wide = S.applyKind(narrow, 'map', CTX);
    ok(!('files' in wide.scope), 'widening to the map drops the file scope');
    const back = S.applyKind(wide, 'match', CTX);
    ok(!('maps' in back.scope), 'narrowing back to one match drops the map scope');

    // Never mutates.
    const before = JSON.stringify(narrow);
    S.applyKind(narrow, 'all', CTX);
    eq(JSON.stringify(narrow), before, 'applyKind leaves its input alone');

    // A page with no entry (a ?map= or ?scope=all landing) can't claim "this
    // match" - and the KIND it records must be the one it actually expressed,
    // or the bar lights a narrow segment over a query that isn't narrow.
    const mapOnly = { kind: 'map', files: [], map: 'de_mirage', entry: '' };
    seq(S.applyKind(base, 'match', mapOnly).scope, { kind: 'map', maps: ['de_mirage'] },
        'with no entry match, "this match" degrades to the map and says so');
    const nothing = { kind: 'all', files: [], map: '', entry: '' };
    seq(S.applyKind(base, 'match', nothing).scope, { kind: 'all' },
        'with no entry and no map it degrades all the way, still honestly');
    seq(S.applyKind(base, 'set', nothing).scope, { kind: 'all' },
        'the same for a set with nothing to name');
}

// ── kindOf ────────────────────────────────────────────────────────────────────
{
    eq(S.kindOf(S.applyKind(Q.emptySpec(), 'all', CTX), CTX), 'all', 'the sentinel is read back');
    // Legacy shapes - a ?q= written before the sentinel existed.
    eq(S.kindOf({ scope: { maps: ['de_mirage'] } }, CTX), 'map', 'a bare map scope reads as map');
    eq(S.kindOf({ scope: { files: [ENTRY] } }, CTX), 'match', 'the entry file alone reads as this match');
    eq(S.kindOf({ scope: { files: [OTHER] } }, CTX), 'set', 'a DIFFERENT single file is a set, not "this match"');
    eq(S.kindOf({ scope: { files: [ENTRY, OTHER] } }, CTX), 'set', 'several files read as a set');
    eq(S.kindOf({ scope: {} }, CTX), 'all', 'an empty scope reads as all maps');
    eq(S.kindOf({}, CTX), 'all', 'so does a spec with no scope at all');
    eq(S.kindOf({ scope: { files: [ENTRY], maps: ['de_dust2'] } }, CTX), 'match',
       'files win over maps - the narrower key is the truth');
}

// ── URL round-trip: the reason the sentinel exists ────────────────────────────
{
    // Without `kind`, an all-maps scope is an empty scope, and toQueryString
    // returns '' for a default spec - so the reload would snap back to the
    // page's entry scope with nothing in the URL to say otherwise.
    const wide = S.applyKind(Q.emptySpec(), 'all', CTX);
    const q = Q.toQueryString(wide);
    ok(q, 'an all-maps scope still produces a querystring');
    eq(S.kindOf(Q.fromQueryString(q), CTX), 'all', 'and survives the round-trip');

    for (const kind of ['match', 'set', 'map', 'all']) {
        const spec = S.applyKind(Q.emptySpec(), kind, SET_CTX);
        eq(S.kindOf(Q.fromQueryString(Q.toQueryString(spec)), SET_CTX), kind,
           `${kind} scope round-trips through the URL`);
    }
    // `clean` must not strip the sentinel while pruning empty scope entries.
    const cleaned = Q.clean(S.applyKind(Q.emptySpec(), 'all', CTX));
    eq(cleaned.scope.kind, 'all', 'clean() keeps the kind sentinel');
}

// ── applyFiles (the by-match breakdown row) ───────────────────────────────────
{
    const s = S.applyFiles(Q.emptySpec(), [OTHER], CTX);
    seq(s.scope, { files: [OTHER], kind: 'set' },
        'picking a match that is not the entry is a one-match set');
    eq(S.applyFiles(Q.emptySpec(), [ENTRY], CTX).scope.kind, 'match',
       'picking the entry match itself is "this match"');
    seq(S.applyFiles(Q.emptySpec(), [], CTX).scope, { kind: 'all' },
        'picking nothing widens to all maps');
    const fromMap = S.applyFiles(S.applyKind(Q.emptySpec(), 'map', CTX), [OTHER], CTX);
    ok(!('maps' in fromMap.scope), 'and it drops any map scope it replaces');
}

// ── filesInScope - what the rail shows ────────────────────────────────────────
{
    deq(S.filesInScope(S.applyKind(Q.emptySpec(), 'match', CTX), CTX, OFFERED), [ENTRY],
        'a match scope shows one section');
    deq(S.filesInScope(S.applyKind(Q.emptySpec(), 'set', SET_CTX), SET_CTX, OFFERED),
        [ENTRY, OTHER], 'a set shows exactly its matches');
    eq(S.filesInScope(S.applyKind(Q.emptySpec(), 'map', CTX), CTX, OFFERED).length, 3,
       'a map scope shows every offered match');
    eq(S.filesInScope(S.applyKind(Q.emptySpec(), 'all', CTX), CTX, OFFERED).length, 3,
       'so does all-maps - the rail can only ever hold this map anyway');
    // A link from another map: refusing to draw anything would be a blank rail
    // with no explanation.
    eq(S.filesInScope({ scope: { files: ['de_nuke_x.json'] } }, CTX, OFFERED).length, 3,
       'a scope naming nothing this page offers falls back to the offered list');
    deq(S.filesInScope(S.applyKind(Q.emptySpec(), 'match', CTX), CTX,
                       [ENTRY, OTHER, THIRD]), [ENTRY],
        'the offered list may be plain file strings');
}

// ── labels ────────────────────────────────────────────────────────────────────
{
    eq(S.label('match', CTX, {}), 'This match', 'match label');
    eq(S.label('set', SET_CTX, { inScope: 3 }), 'These 3 matches', 'set label counts the live scope');
    // The bar is read by a person, so the map is named the way a person says
    // it ("Mirage", "Dust 2") rather than by its file token.
    eq(S.label('map', CTX, { offered: 5 }), 'All 5 on Mirage', 'map label names the map and the count');
    eq(S.label('map', { ...CTX, map: 'de_dust' }, { offered: 2 }), 'All 2 on Dust 2',
       'a legacy-named map still reads as Dust 2');
    eq(S.label('all', CTX, { corpus: 16 }), 'All maps (16)', 'all label carries the corpus size');
    ok(!/\b1 matches\b/.test(S.label('set', { files: [ENTRY] }, { inScope: 1 })),
       'never renders "1 matches"');
}

// ── describe / kindsFor / spansOtherMaps ──────────────────────────────────────
{
    const d = S.describe(S.applyKind(Q.emptySpec(), 'match', CTX), CTX, OFFERED, { offered: 3 });
    ok(d.includes('Mirage'), 'the header line names the map');
    ok(d.includes('this match'), 'and what is in scope');
    ok(d.includes('09 Jun 13:06'), 'and identifies which match that is');

    ok(S.kindsFor(CTX).indexOf('set') < 0, 'no "these N" button when the page arrived with one match');
    ok(S.kindsFor(SET_CTX).indexOf('set') >= 0, 'but there is one when it arrived with a selection');
    ok(S.kindsFor(SET_CTX).indexOf('match') >= 0, 'a set entry can still narrow to its entry match');
    deq(S.kindsFor(CTX).slice(-2), ['map', 'all'], 'widening options always come last');
    // Narrowing from the by-match breakdown makes a set on a page that landed
    // as one match: the bar must grow a segment for it rather than lighting
    // none of them.
    ok(S.kindsFor(CTX, 'set').indexOf('set') >= 0,
       'the live kind is always offered, even when the landing context lacked it');
    deq(S.kindsFor(CTX, 'map'), S.kindsFor(CTX),
       'the widening kinds are already there and are not duplicated');

    ok(S.spansOtherMaps(S.applyKind(Q.emptySpec(), 'all', CTX), CTX),
       'all-maps can return rows the canvas cannot draw');
    ok(!S.spansOtherMaps(S.applyKind(Q.emptySpec(), 'map', CTX), CTX),
       'a map scope cannot');
}

// ── the corpus-wide entry (the rail's "Match Analyser") ──────────────────────
{
    // ?scope=all lands with a radar map but no entry match: the query spans
    // everything, the canvas still has to draw one map.
    const GEN = { kind: 'all', files: [], map: 'de_mirage', entry: '' };
    const kinds = S.kindsFor(GEN, 'all');
    ok(kinds.indexOf('match') < 0, 'no "This match" button when there is no match to mean');
    ok(kinds.indexOf('set') < 0, 'and no "These N" either');
    deq(kinds, ['map', 'all'], 'just the two scopes the page can honestly offer');

    // Pick one match (rail "only" / by-match row) and the button comes back.
    ok(S.kindsFor(Object.assign({}, GEN, { entry: OTHER }), 'match').indexOf('match') >= 0,
       'choosing a match gives the page a "This match" to return to');

    // With no radar at all (an empty corpus), even the map scope is a lie.
    const EMPTY = { kind: 'all', files: [], map: '', entry: '' };
    deq(S.kindsFor(EMPTY, 'all'), ['all'], 'no map loaded, no map button');
    eq(S.label('map', EMPTY, {}), 'All maps', 'and no "All of this map" phrasing');
    ok(!S.describe(Q.emptySpec(), EMPTY, [], {}).includes('this map'),
       'nor in the header line');
}

// ── switching map = navigating ───────────────────────────────────────────────
{
    const GEN = { kind: 'all', files: [], map: 'de_mirage', entry: '' };
    const same = S.applyMap(Q.emptySpec(), 'de_mirage');
    eq(S.wantedMap(same, GEN), null, 'scoping to the map already drawn needs no navigation');
    const other = S.applyMap(Q.emptySpec(), 'de_dust2');
    eq(S.wantedMap(other, GEN), 'de_dust2', 'scoping to another map asks for that page');
    eq(S.kindOf(other, GEN), 'map', 'and reads as a map scope');
    eq(S.wantedMap(S.applyKind(Q.emptySpec(), 'all', GEN), GEN), null,
       'all-maps asks for no particular radar');
    eq(S.wantedMap({ scope: { maps: ['a', 'b'] } }, GEN), null,
       'two maps name no single radar to switch to');
    ok(!('files' in S.applyMap(S.applyFiles(Q.emptySpec(), [OTHER], GEN), 'de_dust2').scope),
       'switching map drops a file scope that belonged to the old one');
    seq(S.applyMap(Q.emptySpec(), '').scope, { kind: 'all' },
        'clearing the map is the all-maps scope');
}

// ── focus: which analyser the page currently IS ──────────────────────────────
{
    const GEN = { kind: 'all', files: [], map: 'de_mirage', entry: '' };

    eq(S.focusFile(S.applyKind(Q.emptySpec(), 'match', CTX), CTX), ENTRY,
       'a match scope focuses its match');
    eq(S.focusFile(S.applyFiles(Q.emptySpec(), [OTHER], GEN), GEN), OTHER,
       'narrowing to one match on the general page focuses it too');
    eq(S.focusFile(S.applyKind(Q.emptySpec(), 'set', SET_CTX), SET_CTX), null,
       'two matches are not a single focus');
    eq(S.focusFile(S.applyKind(Q.emptySpec(), 'map', CTX), CTX), null, 'nor is a map');
    eq(S.focusFile(S.applyKind(Q.emptySpec(), 'all', CTX), CTX), null, 'nor the corpus');
}

{
    // Focusing rewrites the address to ?match=<it>, so on the next load that
    // file IS the entry - and the stale 'set' sentinel must not leave the bar
    // calling it "One match".
    const asEntry = { kind: 'match', files: [OTHER], map: 'de_mirage', entry: OTHER };
    const stale = { scope: { kind: 'set', files: [OTHER] } };
    eq(S.kindOf(stale, asEntry), 'set', 'the sentinel is honoured as written');
    eq(S.kindOf(S.reconcile(stale, asEntry), asEntry), 'match',
       'but reconciling against the landed context upgrades it to "this match"');
    eq(S.kindOf(S.reconcile({ scope: { kind: 'set', files: [OTHER, THIRD] } }, asEntry), asEntry),
       'set', 'a real set is left alone');
    eq(S.reconcile({ scope: { kind: 'all' } }, asEntry).scope.kind, 'all',
       'and so is every other kind');
    const src = { scope: { kind: 'set', files: [OTHER] } };
    S.reconcile(src, asEntry);
    eq(src.scope.kind, 'set', 'reconcile does not mutate its input');
}

{
    // The address must describe the CURRENT scope, not the one the page was
    // opened at, or reloading throws you back to where you started.
    const GEN = { kind: 'all', files: [], map: 'de_mirage', entry: '' };
    deq(S.entryParams(S.applyKind(Q.emptySpec(), 'match', CTX), CTX), { match: ENTRY },
        'a focused match is addressed as ?match=');
    deq(S.entryParams(S.applyFiles(Q.emptySpec(), [OTHER], GEN), GEN), { match: OTHER },
        'including one focused from the general page');
    deq(S.entryParams(S.applyKind(Q.emptySpec(), 'set', SET_CTX), SET_CTX),
        { files: ENTRY + ',' + OTHER }, 'a real set is addressed as ?files=');
    deq(S.entryParams(S.applyKind(Q.emptySpec(), 'map', CTX), CTX), { map: 'de_mirage' },
        'a map scope as ?map=');
    deq(S.entryParams(S.applyKind(Q.emptySpec(), 'all', CTX), CTX),
        { scope: 'all', map: 'de_mirage' },
        'all-maps pins the radar, so a reload cannot silently switch it');
    deq(S.entryParams(S.applyKind(Q.emptySpec(), 'all', { map: '', entry: '' }), { map: '', entry: '' }),
        { scope: 'all' }, 'with no radar there is nothing to pin');
}

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
