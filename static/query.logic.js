/**
 * query.logic.js - pure query-spec manipulation for the Multi Match Analyser.
 *
 * The spec is the same JSON tree analysis/query.py evaluates:
 *
 *   { unit, scope: {maps, modes, files, played_from, played_to},
 *     where: {all: [ node, ... ]}, sort, limit }
 *
 *   node := {all:[...]} | {any:[...]} | {not: node} | {f, op, v}
 *
 * This module owns three jobs, all of them pure so they can be unit-tested
 * under node (test/query.logic.test.mjs) instead of clicked at in a browser:
 *
 *  1. **URL round-tripping** - the whole point of putting the query in the
 *     address bar is that a result set is a link you can send someone or come
 *     back to. multi.js had zero persistence before this: no localStorage, no
 *     URLSearchParams, no replaceState, so a refresh threw the selection away.
 *  2. **Facet toggling** - clicking "AWP 42" in the facet list has to add,
 *     widen, narrow or remove a condition depending on what's already there.
 *     Getting that wrong is what makes faceted filters feel broken, and it is
 *     fiddly enough to deserve tests.
 *  2b. **Groups and negation.** The engine has always had `any` and `not`
 *     nodes; the panel could only ever build `where.all`, so "opening duel OR
 *     clutch" and "not a trade" were unaskable from the UI - the exact
 *     limitation the whole rewrite was meant to remove, one layer up. A
 *     top-level entry is now a leaf, an `any` group, or either of those
 *     negated. Negating a LEAF flips its operator (eq↔ne, in↔nin, is
 *     true↔is false) rather than wrapping it in `not`: `not (hs is true)`
 *     also matches rows that have no `hs` at all (a smoke), which is not what
 *     someone clicking "¬" on "Headshot" means. Only groups, and operators
 *     with no complement (between/contains/exists), wrap in `not`.
 *  3. **Human-readable summaries** - a spec has to be describable in a line
 *     ("kills · AWP · opening duel · not post-plant") or nobody can tell what
 *     they're looking at.
 */
(function (root, factory) {
    if (typeof module === 'object' && module.exports) module.exports = factory(require('./maps.logic.js'));
    else root.QueryLogic = factory(root.MapsLogic);
}(typeof self !== 'undefined' ? self : this, function (MapsLogic) {
    'use strict';

    const DEFAULT_UNIT = 'kill';
    const DEFAULT_SORT = 'recent';

    // Naive "unit + s" produces "clutchs" and "utilitys", which reads as a bug
    // even though nothing is broken.
    const UNIT_PLURAL = {
        kill: 'kills', death: 'deaths', duel: 'duels (both sides)',
        clutch: 'clutches', multikill: 'multi-kills', utility: 'utility',
        flash: 'flashes', round: 'rounds',
        trade: 'trades', support: 'flash supports', execute: 'executes',
        crossfire: 'crossfires', teamplay: 'teamplay moments',
    };

    function unitLabel(unit) {
        return UNIT_PLURAL[unit] || (unit ? unit + 's' : '');
    }

    function emptySpec(unit) {
        return { unit: unit || DEFAULT_UNIT, scope: {}, where: { all: [] }, sort: DEFAULT_SORT };
    }

    /** Fill in the shape the rest of this module assumes. Never mutates. */
    function normalize(spec) {
        const s = spec && typeof spec === 'object' ? JSON.parse(JSON.stringify(spec)) : {};
        s.unit = s.unit || DEFAULT_UNIT;
        s.sort = s.sort || DEFAULT_SORT;
        s.scope = s.scope && typeof s.scope === 'object' ? s.scope : {};
        // A bare leaf or an `any`/`not` at the root is legal in the wire
        // format but awkward to edit, so the editable form is always an `all`
        // list - a single leaf becomes a one-element conjunction.
        if (!s.where || typeof s.where !== 'object') s.where = { all: [] };
        else if (!Array.isArray(s.where.all)) s.where = { all: [s.where] };
        return s;
    }

    /** Conditions with no effect, dropped before a spec goes anywhere. */
    function isEmptyNode(n) {
        if (!n || typeof n !== 'object') return true;
        if (Array.isArray(n.all)) return n.all.every(isEmptyNode);
        if (Array.isArray(n.any)) return n.any.length === 0 || n.any.every(isEmptyNode);
        if (n.not) return isEmptyNode(n.not);
        if (!n.f) return true;
        if (n.op === 'exists') return false;
        if (n.op === 'in' || n.op === 'nin') return !Array.isArray(n.v) || n.v.length === 0;
        return n.v === undefined || n.v === null || n.v === '';
    }

    /** Drop empty leaves out of group nodes, recursively. Never mutates. */
    function pruneNode(n) {
        if (!n || typeof n !== 'object') return n;
        if (Array.isArray(n.any)) return { any: n.any.map(pruneNode).filter(x => !isEmptyNode(x)) };
        if (Array.isArray(n.all)) return { all: n.all.map(pruneNode).filter(x => !isEmptyNode(x)) };
        if (n.not) return { not: pruneNode(n.not) };
        return n;
    }

    function clean(spec) {
        const s = normalize(spec);
        // Prune inside groups too: one half-typed alternative would otherwise
        // widen the whole group to "anything".
        s.where.all = s.where.all.map(pruneNode).filter(n => !isEmptyNode(n));
        Object.keys(s.scope).forEach(k => {
            const v = s.scope[k];
            if (v == null || v === '' || (Array.isArray(v) && !v.length)) delete s.scope[k];
        });
        return s;
    }

    // ── URL round-trip ────────────────────────────────────────────────────────
    // Percent-encoded JSON, not base64: it survives copy/paste identically and
    // stays greppable in server logs and in the address bar, which matters
    // more here than the handful of characters base64 would save.

    function toQueryString(spec) {
        const s = clean(spec);
        // `group_by` counts as state worth putting in the URL even with no
        // filters set: "every kill, broken down by player" is a page someone
        // shares, and dropping it would silently reset the table on reload.
        if (!s.where.all.length && !Object.keys(s.scope).length && !s.group_by
            && s.unit === DEFAULT_UNIT && s.sort === DEFAULT_SORT) return '';
        return encodeURIComponent(JSON.stringify(s));
    }

    function fromQueryString(q) {
        if (!q) return null;
        try {
            const parsed = JSON.parse(decodeURIComponent(q));
            if (!parsed || typeof parsed !== 'object') return null;
            return normalize(parsed);
        } catch (e) {
            // A hand-mangled URL must not blank the page - fall back to the
            // default query and let the user carry on.
            return null;
        }
    }

    // ── condition editing ─────────────────────────────────────────────────────

    /** Index of the top-level condition on `field`, or -1. Ignores negations,
     *  which are tracked as their own entries. */
    function indexOf(spec, field) {
        const s = normalize(spec);
        return s.where.all.findIndex(n => n && n.f === field);
    }

    function get(spec, field) {
        const i = indexOf(spec, field);
        return i < 0 ? null : normalize(spec).where.all[i];
    }

    /** Add or replace the condition on `field`. Returns a new spec. */
    function setCondition(spec, field, op, v) {
        const s = normalize(spec);
        const node = { f: field, op: op, v: v };
        const i = s.where.all.findIndex(n => n && n.f === field);
        if (i >= 0) s.where.all[i] = node;
        else s.where.all.push(node);
        return s;
    }

    function removeCondition(spec, field) {
        const s = normalize(spec);
        s.where.all = s.where.all.filter(n => !(n && n.f === field));
        return s;
    }

    /**
     * Click a facet value. This is the interaction the whole panel hangs on,
     * so the rules are explicit:
     *   - nothing set          -> `f eq value`
     *   - same single value    -> remove it (clicking twice undoes)
     *   - a different value    -> widen to `f in [a, b]`
     *   - already in a list    -> narrow by dropping it (last one removes)
     * With `negate`, the same rules apply to `ne` / `nin`, so alt-clicking is
     * "everything except this" and behaves symmetrically.
     */
    function toggleFacet(spec, field, value, negate) {
        const s = normalize(spec);
        const eqOp = negate ? 'ne' : 'eq';
        const inOp = negate ? 'nin' : 'in';
        const i = s.where.all.findIndex(n => n && n.f === field);
        if (i < 0) return setCondition(s, field, eqOp, value);

        const cur = s.where.all[i];
        if (cur.op === eqOp) {
            if (same(cur.v, value)) return removeCondition(s, field);
            return setCondition(s, field, inOp, [cur.v, value]);
        }
        if (cur.op === inOp && Array.isArray(cur.v)) {
            const has = cur.v.some(x => same(x, value));
            const next = has ? cur.v.filter(x => !same(x, value)) : cur.v.concat([value]);
            if (!next.length) return removeCondition(s, field);
            if (next.length === 1) return setCondition(s, field, eqOp, next[0]);
            return setCondition(s, field, inOp, next);
        }
        // Any other operator on this field (a range, a bool) is replaced -
        // clicking a value is an unambiguous statement about what you want.
        return setCondition(s, field, eqOp, value);
    }

    // ── list fields (`has` / `nhas`) ──────────────────────────────────────────
    // A list field needs APPEND semantics, and that is the whole reason it
    // can't reuse toggleFacet. `indexOf`, `setCondition` and `removeCondition`
    // above all key on the field NAME alone, so a second condition on the same
    // field replaces the first - which is right for `weapon`, where
    // "AWP and Deagle" matches nothing and must widen to `in [AWP, Deagle]`,
    // and wrong for `with_player`, where "with A and with B" is a perfectly
    // good question about a three-man play. These key on (field, value)
    // instead, so several can stand at once.
    //
    // The OR reading is still reachable and needs nothing new: the sticky
    // `∨ any of` mode wraps the same leaves in an `any` group, which is
    // "with either of them".

    function hasIndex(spec, field, value) {
        const all = normalize(spec).where.all;
        return all.findIndex(n => n && n.f === field
            && (n.op === 'has' || n.op === 'nhas') && same(n.v, value));
    }

    /**
     * Add, or remove, one value on a list field. Clicking the same value twice
     * undoes it - the rule every other chip in the panel follows - but only
     * that leaf goes; anything else standing on the same field is left alone.
     */
    function toggleHas(spec, field, value, negate) {
        const s = normalize(spec);
        const i = hasIndex(s, field, value);
        if (i >= 0) {
            const cur = s.where.all[i];
            const wantOp = negate ? 'nhas' : 'has';
            // Same value, other polarity: flip it rather than dropping it, so
            // alt-clicking a selected chip reads as "no, exclude this one".
            if (cur.op !== wantOp) {
                s.where.all[i] = { f: field, op: wantOp, v: value };
                return s;
            }
            s.where.all.splice(i, 1);
            return s;
        }
        s.where.all.push({ f: field, op: negate ? 'nhas' : 'has', v: value });
        return s;
    }

    /** Chip state for a list field. Mirrors isFacetActive/isFacetNegated,
     *  including the polarity an enclosing `not` contributes - a value inside
     *  a "none of (…)" group must paint as excluded, not selected. */
    function isListActive(spec, field, value) {
        return leafStates(normalize(spec).where).some(st =>
            (_holdsList(st.node, field, value, 'has') && !st.neg)
            || (_holdsList(st.node, field, value, 'nhas') && st.neg));
    }

    function isListNegated(spec, field, value) {
        return leafStates(normalize(spec).where).some(st =>
            (_holdsList(st.node, field, value, 'nhas') && !st.neg)
            || (_holdsList(st.node, field, value, 'has') && st.neg));
    }

    function _holdsList(n, field, value, op) {
        return !!n && n.f === field && n.op === op && same(n.v, value);
    }

    /** Every leaf plus whether an enclosing `not` inverts it. Facet
     *  highlighting needs the polarity, not just the leaf: a value inside a
     *  "none of (…)" group is excluded, and painting it as selected would say
     *  the opposite of what the query asks. */
    function leafStates(node, neg, out) {
        out = out || [];
        neg = !!neg;
        if (!node || typeof node !== 'object') return out;
        if (Array.isArray(node.all)) node.all.forEach(n => leafStates(n, neg, out));
        else if (Array.isArray(node.any)) node.any.forEach(n => leafStates(n, neg, out));
        else if (node.not) leafStates(node.not, !neg, out);
        else if (node.f) out.push({ node: node, neg: neg });
        return out;
    }

    function _holds(n, field, value, ops) {
        if (!n || n.f !== field) return false;
        if (n.op === ops[0]) return same(n.v, value);
        if (n.op === ops[1]) return Array.isArray(n.v) && n.v.some(x => same(x, value));
        return false;
    }

    /** True when this facet value is currently selected (for chip
     *  highlighting). Scans every leaf, including ones inside an `any` group:
     *  a value used as an alternative is still selected, and a facet button
     *  that doesn't light up reads as a click that didn't register. */
    function isFacetActive(spec, field, value) {
        return leafStates(normalize(spec).where).some(st =>
            (_holds(st.node, field, value, ['eq', 'in']) && !st.neg)
            || (_holds(st.node, field, value, ['ne', 'nin']) && st.neg));
    }

    function isFacetNegated(spec, field, value) {
        return leafStates(normalize(spec).where).some(st =>
            (_holds(st.node, field, value, ['ne', 'nin']) && !st.neg)
            || (_holds(st.node, field, value, ['eq', 'in']) && st.neg));
    }

    /** Loose equality - values round-trip through URLs as strings. */
    function same(a, b) {
        if (a === b) return true;
        if (a == null || b == null) return false;
        return String(a).toLowerCase() === String(b).toLowerCase();
    }

    // ── groups & negation ──────────────────────────────────

    /** Every leaf `{f,op,v}` under a node, in order, however deeply nested. */
    function leaves(node, out) {
        out = out || [];
        if (!node || typeof node !== 'object') return out;
        if (Array.isArray(node.all)) node.all.forEach(n => leaves(n, out));
        else if (Array.isArray(node.any)) node.any.forEach(n => leaves(n, out));
        else if (node.not) leaves(node.not, out);
        else if (node.f) out.push(node);
        return out;
    }

    function isGroup(node) {
        return !!(node && (Array.isArray(node.any) || Array.isArray(node.all)));
    }

    /** Complementary operators, as data - the negation rule should be
     *  readable, not buried in a branch ladder. */
    const OP_FLIP = { eq: 'ne', ne: 'eq', in: 'nin', nin: 'in',
                      gt: 'lte', lte: 'gt', gte: 'lt', lt: 'gte',
                      has: 'nhas', nhas: 'has' };

    /**
     * Negate one node. Per the module docstring a leaf flips its operator so
     * tri-state stays intact; a group, or an operator with no complement,
     * wraps in `not`. Applying it twice returns the original node.
     */
    function negateNode(node) {
        if (!node || typeof node !== 'object') return node;
        if (node.not) return node.not;                       // unwrap
        if (isGroup(node)) return { not: node };
        if (node.op === 'is') return Object.assign({}, node, { v: node.v === false });
        if (OP_FLIP[node.op]) return Object.assign({}, node, { op: OP_FLIP[node.op] });
        return { not: node };
    }

    /** True when a node currently reads as a negative - drives the chip's
     *  "not" styling and the state of its ¬ toggle. */
    function isNegated(node) {
        if (!node || typeof node !== 'object') return false;
        if (node.not) return true;
        if (node.op === 'is') return node.v === false;
        return node.op === 'ne' || node.op === 'nin' || node.op === 'nhas';
    }

    function negateAt(spec, i) {
        const s = normalize(spec);
        if (i < 0 || i >= s.where.all.length) return s;
        s.where.all[i] = negateNode(s.where.all[i]);
        return s;
    }

    function removeAt(spec, i) {
        const s = normalize(spec);
        if (i >= 0 && i < s.where.all.length) s.where.all.splice(i, 1);
        return s;
    }

    /** Loose node equality, so clicking the same facet twice is recognised as
     *  the same condition however it round-tripped through a URL. */
    function sameNode(a, b) {
        if (!a || !b) return false;
        if (a.f !== b.f || (a.op || 'eq') !== (b.op || 'eq')) return false;
        if (Array.isArray(a.v) || Array.isArray(b.v)) {
            if (!Array.isArray(a.v) || !Array.isArray(b.v) || a.v.length !== b.v.length) return false;
            return a.v.every((x, i) => same(x, b.v[i]));
        }
        return same(a.v, b.v);
    }

    /** The `any` group currently being built: the LAST top-level entry, when
     *  it is one. Alternatives always land at the tail, so a group stays where
     *  the user is looking and an earlier one is never silently reopened. */
    function openAnyIndex(spec) {
        const all = normalize(spec).where.all;
        const i = all.length - 1;
        return i >= 0 && all[i] && Array.isArray(all[i].any) ? i : -1;
    }

    /**
     * Add `node` as an alternative: into the open `any` group, or by turning
     * the last plain condition into one. That last part matters - clicking
     * "AWP" then "or Deagle" must not leave two ANDed equalities, which no
     * row can ever satisfy. Clicking the same alternative again removes it; a
     * group of one collapses back to a plain condition, an empty group goes.
     */
    function addAny(spec, node) {
        const s = normalize(spec);
        const all = s.where.all;
        let i = openAnyIndex(s);
        if (i < 0) {
            const last = all.length - 1;
            if (last >= 0 && all[last] && all[last].f && !all[last].not) {
                all[last] = { any: [all[last]] };
                i = last;
            } else {
                all.push({ any: [] });
                i = all.length - 1;
            }
        }
        const group = all[i].any;
        const at = group.findIndex(n => sameNode(n, node));
        if (at >= 0) group.splice(at, 1);
        else group.push(node);
        if (!group.length) all.splice(i, 1);
        else if (group.length === 1) all[i] = group[0];
        return s;
    }

    /** Remove one alternative from the group at `i` (the chip-internal ×). */
    function removeAnyLeaf(spec, i, j) {
        const s = normalize(spec);
        const node = s.where.all[i];
        const negated = !!(node && node.not);
        const group = node && (node.any || (node.not && node.not.any));
        if (!Array.isArray(group)) return s;
        group.splice(j, 1);
        if (!group.length) s.where.all.splice(i, 1);
        else if (group.length === 1) s.where.all[i] = negated ? negateNode(group[0]) : group[0];
        return s;
    }

    // ── description ───────────────────────────────────────────────────────────

    const OP_TEXT = {
        eq: '=', ne: '≠', in: 'in', nin: 'not in', gt: '>', gte: '≥',
        lt: '<', lte: '≤', between: 'between', contains: 'contains', exists: 'is set',
        has: 'with', nhas: 'not with',
    };

    function describeNode(node, fields) {
        if (!node) return '';
        // "With teammate with Bob" is what the generic path produces; the
        // operator already carries the preposition, so the label is dropped.
        if (node.f && (node.op === 'has' || node.op === 'nhas'))
            return (node.op === 'has' ? 'with ' : 'not with ') + node.v;
        if (Array.isArray(node.all)) return node.all.map(n => describeNode(n, fields)).filter(Boolean).join(' · ');
        if (Array.isArray(node.any)) return '(' + node.any.map(n => describeNode(n, fields)).filter(Boolean).join(' or ') + ')';
        if (node.not) {
            const inner = node.not;
            if (Array.isArray(inner.any))
                return '(none of ' + inner.any.map(n => describeNode(n, fields)).filter(Boolean).join(', ') + ')';
            return 'not ' + describeNode(inner, fields);
        }
        if (!node.f) return '';
        const label = (fields && fields[node.f] && fields[node.f].label) || node.f;
        if (node.op === 'is') return (node.v === false ? 'not ' : '') + label;
        if (node.op === 'exists') return label + ' is set';
        if (node.op === 'between' && Array.isArray(node.v)) return label + ' ' + node.v[0] + '–' + node.v[1];
        const v = Array.isArray(node.v) ? node.v.join(', ') : node.v;
        return label + ' ' + (OP_TEXT[node.op] || node.op) + ' ' + v;
    }

    /** Short display name for a match file: the stem, minus the .json. */
    function matchLabel(file) {
        return String(file || '').replace(/\.json(\.br)?$/, '');
    }

    /** One-line summary of a whole spec, for the results header. */
    function describe(spec, fields) {
        const s = clean(spec);
        const bits = [unitLabel(s.unit)];
        const scope = [];
        // Named the way a person says them; the spec keeps the raw keys.
        if (s.scope.maps && s.scope.maps.length)
            scope.push(s.scope.maps.map(m => MapsLogic.label(m)).join('/'));
        if (s.scope.modes && s.scope.modes.length) scope.push(s.scope.modes.join('/'));
        if (s.scope.files && s.scope.files.length) {
            // Name the match when there is one. "1 matches" was both
            // ungrammatical and useless - the whole question at that point is
            // WHICH match.
            scope.push(s.scope.files.length === 1
                ? matchLabel(s.scope.files[0])
                : s.scope.files.length + ' matches');
        }
        // Date bounds are named explicitly: a query restricted to a window
        // otherwise describes itself exactly like one over the whole corpus.
        if (s.scope.played_from || s.scope.played_to) {
            const d = t => new Date(t * 1000).toISOString().slice(0, 10);
            scope.push(s.scope.played_from && s.scope.played_to
                ? d(s.scope.played_from) + '–' + d(s.scope.played_to)
                : s.scope.played_from ? 'since ' + d(s.scope.played_from)
                                      : 'until ' + d(s.scope.played_to));
        }
        if (scope.length) bits.push(scope.join(' '));
        const w = describeNode(s.where, fields);
        if (w) bits.push(w);
        return bits.join(' · ');
    }

    /**
     * How many conditions are active - drives the "clear filters" affordance.
     *
     * Scope deliberately does NOT count. It is where you are, not something
     * you filtered: counting it made "Clear" appear on a freshly-landed page
     * where the user had asked for nothing, and clicking it then silently
     * widened them from one match to the whole corpus.
     */
    function conditionCount(spec) {
        return clean(spec).where.all.length;
    }

    /** Scope keys in play, for callers that do want to know. */
    function scopeCount(spec) {
        const sc = clean(spec).scope;
        return Object.keys(sc).filter(k => k !== 'kind').length;
    }

    return {
        DEFAULT_UNIT, DEFAULT_SORT, UNIT_PLURAL, unitLabel,
        emptySpec, normalize, clean, isEmptyNode,
        toQueryString, fromQueryString,
        indexOf, get, setCondition, removeCondition,
        toggleFacet, isFacetActive, isFacetNegated, same, sameNode,
        hasIndex, toggleHas, isListActive, isListNegated,
        leaves, leafStates, isGroup, negateNode, isNegated, negateAt, removeAt,
        openAnyIndex, addAny, removeAnyLeaf, pruneNode,
        describeNode, describe, conditionCount, scopeCount, matchLabel,
    };
}));
