/**
 * Unit tests for the analyser's client-side query state (static/query.logic.js).
 * Run: node test/query.logic.test.mjs   (no deps, no browser)
 *
 * What matters: URL round-tripping (a shared link must reproduce the exact
 * result set), facet click semantics (add → widen → narrow → remove, and the
 * negated mirror of that), and never mutating the caller's spec - the panel
 * keeps a reference to the current spec and re-renders from it, so an in-place
 * edit would desync the UI from the query that was actually run.
 */
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const Q = require('../static/query.logic.js');

let passed = 0, failed = 0;
const ok = (c, m) => { if (c) passed++; else { failed++; console.error('  ✗ FAIL:', m); } };
const eq = (a, b, m) => ok(a === b, `${m} (got ${JSON.stringify(a)}, want ${JSON.stringify(b)})`);
const deq = (a, b, m) => ok(JSON.stringify(a) === JSON.stringify(b),
    `${m} (got ${JSON.stringify(a)}, want ${JSON.stringify(b)})`);

const FIELDS = {
    weapon: { label: 'Weapon' }, opening: { label: 'Opening duel' },
    player: { label: 'Player' }, bomb_planted: { label: 'Bomb planted' },
};

// ── normalize ─────────────────────────────────────────────────────────────────
{
    const s = Q.normalize(null);
    eq(s.unit, 'kill', 'default unit');
    eq(s.sort, 'recent', 'default sort');
    deq(s.where, { all: [] }, 'default where is an empty conjunction');
    deq(s.scope, {}, 'default scope is empty');

    const bare = Q.normalize({ where: { f: 'opening', op: 'is', v: true } });
    deq(bare.where, { all: [{ f: 'opening', op: 'is', v: true }] },
        'a bare leaf is lifted into an all-list so the panel can edit it');

    const anyRoot = Q.normalize({ where: { any: [{ f: 'a', op: 'eq', v: 1 }] } });
    eq(Array.isArray(anyRoot.where.all), true, 'an any-root is wrapped, not discarded');
    eq(anyRoot.where.all.length, 1, 'and kept as a single nested condition');
}

// ── no mutation ───────────────────────────────────────────────────────────────
{
    const orig = { unit: 'kill', where: { all: [{ f: 'weapon', op: 'eq', v: 'AWP' }] } };
    const snapshot = JSON.stringify(orig);
    Q.setCondition(orig, 'player', 'eq', 'alpha');
    Q.removeCondition(orig, 'weapon');
    Q.toggleFacet(orig, 'weapon', 'AK-47');
    Q.clean(orig);
    Q.normalize(orig);
    eq(JSON.stringify(orig), snapshot, 'every operation leaves the input spec untouched');
}

// ── isEmptyNode / clean ───────────────────────────────────────────────────────
{
    ok(Q.isEmptyNode(null), 'null is empty');
    ok(Q.isEmptyNode({}), 'a node with no field is empty');
    ok(Q.isEmptyNode({ f: 'weapon', op: 'eq', v: '' }), 'an empty value is empty');
    ok(Q.isEmptyNode({ f: 'weapon', op: 'in', v: [] }), 'an empty list is empty');
    ok(!Q.isEmptyNode({ f: 'weapon', op: 'eq', v: 'AWP' }), 'a real condition is not');
    ok(!Q.isEmptyNode({ f: 'assist', op: 'exists' }), 'exists needs no value');
    // false and 0 are real values and must survive the cleaner.
    ok(!Q.isEmptyNode({ f: 'opening', op: 'is', v: false }), '`is false` is a real condition');
    ok(!Q.isEmptyNode({ f: 'damage', op: 'eq', v: 0 }), 'zero is a real value');

    const c = Q.clean({ where: { all: [{ f: 'a', op: 'eq', v: 1 }, {}, { f: 'b', op: 'in', v: [] }] },
                        scope: { maps: [], modes: ['mm'], files: null } });
    eq(c.where.all.length, 1, 'empty conditions are dropped');
    deq(Object.keys(c.scope), ['modes'], 'empty scope entries are dropped');
}

// ── URL round-trip ────────────────────────────────────────────────────────────
{
    const spec = {
        unit: 'death',
        scope: { maps: ['de_mirage'] },
        where: { all: [
            { f: 'opening', op: 'is', v: true },
            { any: [{ f: 'weapon', op: 'eq', v: 'AWP' }, { f: 'hs', op: 'is', v: true }] },
            { not: { f: 'bomb_planted', op: 'is', v: true } },
        ] },
        sort: 'impact',
    };
    const q = Q.toQueryString(spec);
    const back = Q.fromQueryString(q);
    eq(back.unit, 'death', 'unit survives the round trip');
    eq(back.sort, 'impact', 'sort survives');
    deq(back.scope.maps, ['de_mirage'], 'scope survives');
    eq(back.where.all.length, 3, 'every condition survives');
    ok(Array.isArray(back.where.all[1].any), 'a nested OR survives intact');
    ok(back.where.all[2].not, 'a NOT survives intact');
    // The exact same result set must come back out.
    deq(Q.clean(back), Q.clean(spec), 'round trip is lossless');

    eq(Q.toQueryString(Q.emptySpec()), '', 'the default query produces no querystring at all');
    eq(Q.fromQueryString(''), null, 'an absent q is null, not a crash');
    eq(Q.fromQueryString('not%20json'), null, 'a mangled q falls back to null rather than throwing');
    eq(Q.fromQueryString('%7B%22a%22%3A1%7D').unit, 'kill', 'a valid but foreign object still normalizes');
}

// ── facet toggling: the add → widen → narrow → remove cycle ───────────────────
{
    let s = Q.emptySpec();
    s = Q.toggleFacet(s, 'weapon', 'AWP');
    deq(Q.get(s, 'weapon'), { f: 'weapon', op: 'eq', v: 'AWP' }, 'first click sets eq');

    s = Q.toggleFacet(s, 'weapon', 'AK-47');
    deq(Q.get(s, 'weapon'), { f: 'weapon', op: 'in', v: ['AWP', 'AK-47'] },
        'a second value widens to an in-list');

    s = Q.toggleFacet(s, 'weapon', 'Deagle');
    deq(Q.get(s, 'weapon').v, ['AWP', 'AK-47', 'Deagle'], 'a third value extends the list');

    s = Q.toggleFacet(s, 'weapon', 'AK-47');
    deq(Q.get(s, 'weapon').v, ['AWP', 'Deagle'], 'clicking a selected value narrows the list');

    s = Q.toggleFacet(s, 'weapon', 'Deagle');
    deq(Q.get(s, 'weapon'), { f: 'weapon', op: 'eq', v: 'AWP' },
        'down to one value it collapses back to eq');

    s = Q.toggleFacet(s, 'weapon', 'AWP');
    eq(Q.get(s, 'weapon'), null, 'clicking the last value removes the condition entirely');
}

// ── facet toggling: negated mirror ────────────────────────────────────────────
{
    let s = Q.emptySpec();
    s = Q.toggleFacet(s, 'weapon', 'AWP', true);
    deq(Q.get(s, 'weapon'), { f: 'weapon', op: 'ne', v: 'AWP' }, 'alt-click excludes');
    s = Q.toggleFacet(s, 'weapon', 'AK-47', true);
    deq(Q.get(s, 'weapon'), { f: 'weapon', op: 'nin', v: ['AWP', 'AK-47'] },
        'a second exclusion widens to nin');
    s = Q.toggleFacet(s, 'weapon', 'AWP', true);
    deq(Q.get(s, 'weapon'), { f: 'weapon', op: 'ne', v: 'AK-47' }, 'and narrows symmetrically');
    s = Q.toggleFacet(s, 'weapon', 'AK-47', true);
    eq(Q.get(s, 'weapon'), null, 'and clears');
}

// ── switching between include and exclude ─────────────────────────────────────
{
    let s = Q.toggleFacet(Q.emptySpec(), 'weapon', 'AWP');
    s = Q.toggleFacet(s, 'weapon', 'AWP', true);
    deq(Q.get(s, 'weapon'), { f: 'weapon', op: 'ne', v: 'AWP' },
        'alt-clicking an included value flips it to excluded rather than stacking');
}

// ── a range condition is replaced, not merged ─────────────────────────────────
{
    let s = Q.setCondition(Q.emptySpec(), 't_into_round', 'between', [0, 20]);
    s = Q.toggleFacet(s, 't_into_round', 5);
    deq(Q.get(s, 't_into_round'), { f: 't_into_round', op: 'eq', v: 5 },
        'clicking a value replaces an incompatible operator');
}

// ── active / negated reporting (chip highlighting) ────────────────────────────
{
    let s = Q.toggleFacet(Q.emptySpec(), 'weapon', 'AWP');
    ok(Q.isFacetActive(s, 'weapon', 'AWP'), 'the selected value reports active');
    ok(!Q.isFacetActive(s, 'weapon', 'AK-47'), 'others do not');
    s = Q.toggleFacet(s, 'weapon', 'AK-47');
    ok(Q.isFacetActive(s, 'weapon', 'AWP') && Q.isFacetActive(s, 'weapon', 'AK-47'),
       'both values in an in-list report active');
    ok(!Q.isFacetActive(Q.emptySpec(), 'weapon', 'AWP'), 'nothing set, nothing active');

    const n = Q.toggleFacet(Q.emptySpec(), 'weapon', 'AWP', true);
    ok(Q.isFacetNegated(n, 'weapon', 'AWP'), 'an excluded value reports negated');
    ok(!Q.isFacetActive(n, 'weapon', 'AWP'), 'and not active');
}

// ── values round-trip as strings, so comparison is loose ──────────────────────
{
    const s = Q.toggleFacet(Q.emptySpec(), 'team', 1);
    ok(Q.isFacetActive(s, 'team', '1'), 'a numeric value set from JSON matches its string form from a URL');
    ok(Q.same('CT', 'ct'), 'case-insensitive');
    ok(!Q.same(null, 'ct'), 'null matches nothing');
}

// ── description ───────────────────────────────────────────────────────────────
{
    const spec = {
        unit: 'kill', scope: { maps: ['de_mirage'] },
        where: { all: [
            { f: 'weapon', op: 'eq', v: 'AWP' },
            { f: 'opening', op: 'is', v: true },
            { not: { f: 'bomb_planted', op: 'is', v: true } },
        ] },
    };
    const d = Q.describe(spec, FIELDS);
    ok(d.includes('kills'), 'names the unit');
    ok(d.includes('Mirage'), 'names the map scope, the way a person says it');
    ok(d.includes('Weapon = AWP'), 'uses the field label, not the raw key');
    ok(d.includes('Opening duel'), 'a boolean reads as its label alone');
    ok(d.includes('not Bomb planted'), 'a negation reads as "not X"');

    eq(Q.describe(Q.emptySpec(), FIELDS), 'kills', 'an empty query describes as just its unit');
    eq(Q.describeNode({ f: 'opening', op: 'is', v: false }, FIELDS), 'not Opening duel',
       '`is false` reads as a negation');
    ok(Q.describeNode({ any: [{ f: 'weapon', op: 'eq', v: 'AWP' },
                              { f: 'weapon', op: 'eq', v: 'AK-47' }] }, FIELDS).includes(' or '),
       'an OR reads as "or"');
    eq(Q.describeNode({ f: 't', op: 'between', v: [0, 20] }, {}), 't 0–20', 'ranges read as a span');
}

// ── condition count ───────────────────────────────────────────────────────────
{
    eq(Q.conditionCount(Q.emptySpec()), 0, 'an empty spec has no conditions');
    let s = Q.toggleFacet(Q.emptySpec(), 'weapon', 'AWP');
    s = Q.setCondition(s, 'opening', 'is', true);
    s.scope.maps = ['de_dust2'];
    // Scope is where you are, not something you filtered: counting it lit up
    // "Clear" on a page where nothing had been asked, and clearing then
    // silently widened the query from one match to the whole corpus.
    eq(Q.conditionCount(s), 2, 'only where-conditions count');
    eq(Q.scopeCount(s), 1, 'scope is counted separately');
    eq(Q.scopeCount({ scope: { kind: 'all' } }), 0,
       'the kind sentinel is not a scope restriction');
}

{
    // Scope in the summary line.
    const one = { unit: 'kill', scope: { files: ['de_mirage_20260609_1306_mm.json'] } };
    ok(Q.describe(one, {}).includes('de_mirage_20260609_1306_mm'),
       'a single-match scope names the match');
    ok(!Q.describe(one, {}).includes('1 matches'), 'and never says "1 matches"');
    ok(Q.describe({ unit: 'kill', scope: { files: ['a.json', 'b.json'] } }, {}).includes('2 matches'),
       'several matches read as a count');
    ok(Q.describe({ unit: 'kill', scope: { played_from: 1750000000 } }, {}).includes('since'),
       'a date bound is visible in the summary instead of silently applying');
}

// ── groups (OR) and negation ──────────────────────────────────────────────────
{
    // Building an OR out of two facet clicks. The second click must convert
    // the first condition into the group rather than ANDing a second equality
    // beside it - "weapon = AWP AND weapon = Deagle" matches nothing, ever.
    let s = Q.setCondition(Q.emptySpec(), 'weapon', 'eq', 'AWP');
    s = Q.addAny(s, { f: 'weapon', op: 'eq', v: 'Deagle' });
    deq(Q.clean(s).where, { all: [{ any: [
        { f: 'weapon', op: 'eq', v: 'AWP' },
        { f: 'weapon', op: 'eq', v: 'Deagle' },
    ] }] }, 'a second alternative absorbs the standing condition into an any-group');

    // Cross-field OR - the case that was flatly unaskable before.
    s = Q.addAny(s, { f: 'opening', op: 'is', v: true });
    eq(Q.clean(s).where.all.length, 1, 'alternatives stay in one group, not three conditions');
    eq(Q.clean(s).where.all[0].any.length, 3, 'a group holds fields of different kinds');

    // Clicking the same alternative again takes it back out.
    s = Q.addAny(s, { f: 'opening', op: 'is', v: true });
    eq(Q.clean(s).where.all[0].any.length, 2, 'adding a present alternative removes it');

    // Down to one, the group collapses - a one-element OR is just a condition.
    s = Q.addAny(s, { f: 'weapon', op: 'eq', v: 'Deagle' });
    deq(Q.clean(s).where, { all: [{ f: 'weapon', op: 'eq', v: 'AWP' }] },
        'a group of one collapses back to a plain condition');

    // ...and emptying it removes the entry entirely.
    s = Q.addAny(s, { f: 'weapon', op: 'eq', v: 'AWP' });
    deq(Q.clean(s).where, { all: [] }, 'an emptied group leaves nothing behind');
}

{
    // Negating a LEAF flips its operator instead of wrapping it in `not`.
    // `not (hs is true)` also matches rows with no `hs` at all (a smoke has
    // none), which is not what clicking ¬ on "Headshot" means.
    const leaf = { f: 'opening', op: 'is', v: true };
    deq(Q.negateNode(leaf), { f: 'opening', op: 'is', v: false }, 'is true -> is false');
    deq(Q.negateNode(Q.negateNode(leaf)), leaf, 'negating twice is the identity');
    deq(Q.negateNode({ f: 'weapon', op: 'eq', v: 'AWP' }), { f: 'weapon', op: 'ne', v: 'AWP' },
        'eq -> ne');
    deq(Q.negateNode({ f: 'weapon', op: 'in', v: ['AWP'] }), { f: 'weapon', op: 'nin', v: ['AWP'] },
        'in -> nin');
    deq(Q.negateNode({ f: 'round', op: 'gt', v: 12 }), { f: 'round', op: 'lte', v: 12 },
        'gt -> lte');

    // No complementary operator -> wrap, which is what `not` is for.
    deq(Q.negateNode({ f: 'round', op: 'between', v: [1, 5] }),
        { not: { f: 'round', op: 'between', v: [1, 5] } }, 'between has no flip, so it wraps');

    // A group always wraps: "none of (...)".
    const grp = { any: [{ f: 'weapon', op: 'eq', v: 'AWP' }] };
    deq(Q.negateNode(grp), { not: grp }, 'a group negates by wrapping');
    deq(Q.negateNode(Q.negateNode(grp)), grp, 'and unwraps again');

    ok(Q.isNegated({ f: 'opening', op: 'is', v: false }), 'is false reads as negated');
    ok(Q.isNegated({ not: grp }), 'a wrapped group reads as negated');
    ok(!Q.isNegated({ f: 'opening', op: 'is', v: true }), 'a plain condition does not');
}

{
    // Chip-level editing by index: negate one, remove one alternative, remove
    // the whole entry.
    let s = Q.setCondition(Q.emptySpec(), 'weapon', 'eq', 'AWP');
    s = Q.addAny(s, { f: 'weapon', op: 'eq', v: 'Deagle' });
    s = Q.setCondition(s, 'opening', 'is', true);
    s = Q.negateAt(s, 1);
    deq(Q.clean(s).where.all[1], { f: 'opening', op: 'is', v: false }, 'negateAt flips one chip');
    s = Q.negateAt(s, 0);
    ok(!!Q.clean(s).where.all[0].not, 'negateAt wraps a group chip');
    eq(Q.describeNode(Q.clean(s).where.all[0], FIELDS).slice(0, 8), '(none of',
       'a negated group describes as "none of"');
    s = Q.removeAnyLeaf(s, 0, 1);
    deq(Q.clean(s).where.all[0], { f: 'weapon', op: 'ne', v: 'AWP' },
        'dropping to one alternative collapses the negated group into a negated leaf');
    s = Q.removeAt(s, 0);
    eq(Q.clean(s).where.all.length, 1, 'removeAt drops the whole entry');
}

{
    // Facet highlighting has to see inside groups, and has to respect the
    // polarity an enclosing `not` imposes.
    let s = Q.setCondition(Q.emptySpec(), 'weapon', 'eq', 'AWP');
    s = Q.addAny(s, { f: 'weapon', op: 'eq', v: 'Deagle' });
    ok(Q.isFacetActive(s, 'weapon', 'Deagle'), 'an alternative inside a group is selected');
    s = Q.negateAt(s, 0);
    ok(!Q.isFacetActive(s, 'weapon', 'Deagle'), 'inside "none of", it is not selected');
    ok(Q.isFacetNegated(s, 'weapon', 'Deagle'), 'it is excluded instead');
}

{
    // A group survives the URL round-trip like any other node.
    let s = Q.setCondition(Q.emptySpec(), 'weapon', 'eq', 'AWP');
    s = Q.addAny(s, { f: 'opening', op: 'is', v: true });
    s = Q.negateAt(s, 0);
    const back = Q.fromQueryString(Q.toQueryString(s));
    deq(Q.clean(back).where, Q.clean(s).where, 'a negated OR group round-trips through the URL');
}

{
    // A half-built alternative must not widen the group to "anything".
    const s = { unit: 'kill', where: { all: [{ any: [
        { f: 'weapon', op: 'eq', v: 'AWP' },
        { f: 'weapon', op: 'eq', v: '' },
    ] }] } };
    deq(Q.clean(s).where, { all: [{ any: [{ f: 'weapon', op: 'eq', v: 'AWP' }] }] },
        'empty leaves are pruned from inside a group');
}

{
    // The breakdown dimension is view state, but it belongs in the URL: a
    // link to "every kill by player" has to arrive with the table already on.
    const s = Object.assign(Q.emptySpec(), { group_by: 'by_player' });
    ok(Q.toQueryString(s), 'group_by alone still produces a querystring');
    eq(Q.fromQueryString(Q.toQueryString(s)).group_by, 'by_player', 'and round-trips');
}

// ── list fields: `has` / `nhas` ───────────────────────────────────────────────
// A list field APPENDS where a facet field replaces. That is the whole reason
// toggleHas exists: "with A and with B" is a real question about a three-man
// play, while "weapon = AWP and weapon = Deagle" matches nothing, ever.
{
    let s = Q.toggleHas(Q.emptySpec(), 'with_player', 'alpha');
    deq(s.where.all, [{ f: 'with_player', op: 'has', v: 'alpha' }], 'first pick');

    s = Q.toggleHas(s, 'with_player', 'bravo');
    eq(s.where.all.length, 2, 'a second value on the same field ADDS a leaf');
    deq(s.where.all[1], { f: 'with_player', op: 'has', v: 'bravo' }, 'and keeps the first');

    s = Q.toggleHas(s, 'with_player', 'alpha');
    deq(s.where.all, [{ f: 'with_player', op: 'has', v: 'bravo' }],
        'clicking one again removes only that leaf');

    s = Q.toggleHas(s, 'with_player', 'bravo');
    deq(s.where.all, [], 'and the last one clears the field');
}

{
    // Alt-click on an already-selected value flips its polarity rather than
    // dropping it - "no, exclude this one" in a single gesture.
    let s = Q.toggleHas(Q.emptySpec(), 'with_player', 'alpha');
    s = Q.toggleHas(s, 'with_player', 'alpha', true);
    deq(s.where.all, [{ f: 'with_player', op: 'nhas', v: 'alpha' }], 'has -> nhas');
    s = Q.toggleHas(s, 'with_player', 'alpha', true);
    deq(s.where.all, [], 'and again clears it');
}

{
    // toggleFacet must be left alone by all of this.
    let s = Q.toggleFacet(Q.emptySpec(), 'weapon', 'AWP');
    s = Q.toggleFacet(s, 'weapon', 'Deagle');
    eq(s.where.all.length, 1, 'a facet field still holds ONE condition');
    eq(s.where.all[0].op, 'in', 'widened rather than ANDed');
}

// ── negating a list leaf flips it, it does not wrap ───────────────────────────
{
    const leaf = { f: 'with_player', op: 'has', v: 'alpha' };
    deq(Q.negateNode(leaf), { f: 'with_player', op: 'nhas', v: 'alpha' }, 'has -> nhas');
    deq(Q.negateNode(Q.negateNode(leaf)), leaf, 'negating twice is the identity');
    ok(Q.isNegated({ f: 'with_player', op: 'nhas', v: 'a' }), 'nhas reads as negative');
    ok(!Q.isNegated(leaf), 'has does not');
}

// ── chip state for list fields ────────────────────────────────────────────────
{
    let s = Q.toggleHas(Q.emptySpec(), 'with_player', 'alpha');
    ok(Q.isListActive(s, 'with_player', 'alpha'), 'selected value lights up');
    ok(!Q.isListActive(s, 'with_player', 'bravo'), 'others do not');
    ok(!Q.isListNegated(s, 'with_player', 'alpha'), 'and is not painted as excluded');

    s = Q.toggleHas(Q.emptySpec(), 'with_player', 'alpha', true);
    ok(Q.isListNegated(s, 'with_player', 'alpha'), 'an excluded value is painted so');
    ok(!Q.isListActive(s, 'with_player', 'alpha'), 'and not as selected');
}

{
    // Inside a "none of (…)" group the polarity inverts, exactly as it does
    // for ordinary facets - a value there is excluded, not selected.
    const s = Q.normalize({ where: { all: [{ not: { any: [
        { f: 'with_player', op: 'has', v: 'alpha' },
    ] } }] } });
    ok(Q.isListNegated(s, 'with_player', 'alpha'), 'negated group inverts the chip');
    ok(!Q.isListActive(s, 'with_player', 'alpha'), 'and it does not also read as selected');
}

// ── OR mode still reaches the "either of them" reading ────────────────────────
{
    let s = Q.addAny(Q.emptySpec(), { f: 'with_player', op: 'has', v: 'alpha' });
    s = Q.addAny(s, { f: 'with_player', op: 'has', v: 'bravo' });
    eq(s.where.all.length, 1, 'one group');
    eq(s.where.all[0].any.length, 2, 'holding both alternatives');
    ok(Q.isListActive(s, 'with_player', 'alpha'), 'an alternative still lights its chip');
    const back = Q.fromQueryString(Q.toQueryString(s));
    deq(back.where, s.where, 'and the group survives the URL round-trip');
}

// ── describeNode reads a list leaf as English ─────────────────────────────────
{
    eq(Q.describeNode({ f: 'with_player', op: 'has', v: 'alpha' }, FIELDS), 'with alpha',
       'the operator carries the preposition, so the label is dropped');
    eq(Q.describeNode({ f: 'vs_player', op: 'nhas', v: 'bravo' }, FIELDS), 'not with bravo',
       'and its negative');
}

// ── the new units have real plurals ───────────────────────────────────────────
{
    eq(Q.unitLabel('trade'), 'trades', 'trade');
    eq(Q.unitLabel('support'), 'flash supports', 'support');
    eq(Q.unitLabel('execute'), 'executes', 'execute');
    eq(Q.unitLabel('crossfire'), 'crossfires', 'crossfire');
    eq(Q.unitLabel('teamplay'), 'teamplay moments', 'teamplay');
}

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
