/**
 * scope.logic.js - what set of matches the Multi Round Analyser is looking at.
 *
 * The page serves four genuinely different jobs and has to look different in
 * each: *analyse this match*, *compare these N matches I picked*, *explore
 * this whole map*, and *query the entire corpus*. Which one you are in is
 * called the scope.
 *
 * Scope has exactly one owner. `spec.scope` - already in the URL, already
 * understood by analysis/query.py's `_scope_ok()` - is the single source of
 * truth; the header bar renders *from* it and writes back *through* it, and
 * the round rail filters on the same derived value. That single ownership is
 * the point of the module: the page offers a wider match list than the query
 * is scoped to (widening is then a client-side change, with no reload), and
 * with two independent notions of "which matches" the header can end up
 * describing one set while the results describe another.
 *
 * `ctx` is the page's scope context, computed server-side and handed over as
 * `window.MULTI_SCOPE`:
 *
 *     { kind, files: [...], map: 'de_mirage', entry: '<file>.json' }
 *
 * `entry` is empty on the map-wide (`?map=`) and corpus-wide (`?scope=all`,
 * the rail's "Match Analyser") entries - there is no "this match" there. Every
 * helper here has to stay honest about that: a bar that offers "This match"
 * with no match to mean, or a kind that produces no restriction while a narrow
 * segment lights up, is the same class of lie this module exists to prevent.
 *
 * `ctx.kind` is only the *default* - the landing shape. Once a query is in the
 * URL, the spec wins (a shared link has to reproduce its own result set), and
 * the kind comes from the spec. That is why `kindOf` takes the spec and not
 * the context's own kind.
 *
 * The kind is written INTO `spec.scope` as a sentinel rather than being derived
 * purely from `files`/`maps`, and that is not redundancy. "All maps" is the
 * absence of both keys - and `QueryLogic.toQueryString` emits no `?q=` at all
 * when the scope is empty and everything else is default, so a deliberately
 * widened page would come back from a reload snapped to its entry scope, with
 * nothing in the URL to say otherwise. `kind` is what makes the widest option
 * representable. It rides along safely because `_scope_ok` (analysis/query.py)
 * reads only the keys it knows and ignores the rest - there is a test pinning
 * that.
 *
 * Pure, so the fiddly parts - deriving the kind, swapping `files`↔`maps`
 * without leaving a stale key behind, counting what the rail should show - are
 * unit-tested under node (test/scope.logic.test.mjs) rather than clicked at.
 */
(function (root, factory) {
    if (typeof module === 'object' && module.exports) module.exports = factory(require('./maps.logic.js'));
    else root.ScopeLogic = factory(root.MapsLogic);
}(typeof self !== 'undefined' ? self : this, function (MapsLogic) {
    'use strict';

    const KINDS = ['match', 'set', 'map', 'all'];

    // Map names reach this module raw, straight off a spec or a match
    // document; everything it returns is read by a person, so a map is named
    // the way a person says it.
    const mapLabel = (m) => MapsLogic.label(m);

    function emptyCtx() {
        return { kind: 'map', files: [], map: '', entry: '' };
    }

    /** Normalise whatever the page handed over. Never mutates. */
    function normalizeCtx(ctx) {
        const c = Object.assign(emptyCtx(), ctx || {});
        c.files = Array.isArray(c.files) ? c.files.slice() : [];
        c.kind = KINDS.indexOf(c.kind) >= 0 ? c.kind : 'map';
        return c;
    }

    function scopeOf(spec) {
        return (spec && spec.scope && typeof spec.scope === 'object') ? spec.scope : {};
    }

    /**
     * The kind a spec currently expresses.
     *
     * The sentinel wins when present (see the module docstring); otherwise it
     * is derived, which is what keeps older shared links working - a `?q=`
     * written before this existed carries only `maps` or `files`.
     *
     * When deriving, `files` wins over `maps` because it is strictly narrower:
     * a spec carrying both is scoped to the files, and reporting it as "the
     * whole map" would be a lie the header would then repeat.
     */
    function kindOf(spec, ctx) {
        const c = normalizeCtx(ctx);
        const sc = scopeOf(spec);
        if (KINDS.indexOf(sc.kind) >= 0) return sc.kind;
        const files = Array.isArray(sc.files) ? sc.files : null;
        if (files && files.length) {
            return (files.length === 1 && files[0] === c.entry) ? 'match' : 'set';
        }
        const maps = Array.isArray(sc.maps) ? sc.maps : null;
        if (maps && maps.length) return 'map';
        return 'all';
    }

    /**
     * Rewrite a spec's scope to `kind`. Returns a NEW spec.
     *
     * Clearing the keys the new kind does not use is the whole reason this is
     * a function rather than two lines at the call site. `files` is strictly
     * narrower than `maps`, so writing `maps` while leaving a `files` behind
     * produces a query that stays narrow under a bar that says it widened.
     * Both keys are dropped first, then only the ones the kind means are set.
     */
    function applyKind(spec, kind, ctx) {
        const c = normalizeCtx(ctx);
        const s = spec && typeof spec === 'object'
            ? JSON.parse(JSON.stringify(spec)) : {};
        s.scope = Object.assign({}, scopeOf(s));
        delete s.scope.files;
        delete s.scope.maps;
        let want = KINDS.indexOf(kind) >= 0 ? kind : 'map';
        if (want === 'match') {
            if (c.entry) s.scope.files = [c.entry];
            else want = c.map ? 'map' : 'all';
        } else if (want === 'set') {
            // Falling back to the entry keeps "these matches" meaningful on a
            // page that arrived with no selection, instead of silently
            // widening to everything.
            if (c.files.length) s.scope.files = c.files.slice();
            else if (c.entry) s.scope.files = [c.entry];
            else want = c.map ? 'map' : 'all';
        }
        if (want === 'map') {
            if (c.map) s.scope.maps = [c.map];
            else want = 'all';
        }
        // The kind recorded is the one actually EXPRESSED, never the one that
        // was asked for: writing `kind:'match'` with no `files` behind it (the
        // corpus-wide page has no entry match) produced a bar segment that
        // said "this match" over a query that searched everything.
        s.scope.kind = want;
        return s;
    }

    /**
     * Scope one spec to a named map. Separate from `applyKind('map')`, which
     * can only ever mean the page's own map - this is what the map picker and
     * the by-map facet use, and the page it produces may need a different
     * radar (see `wantedMap`).
     */
    function applyMap(spec, map) {
        const s = spec && typeof spec === 'object'
            ? JSON.parse(JSON.stringify(spec)) : {};
        s.scope = Object.assign({}, scopeOf(s));
        delete s.scope.files;
        if (map) {
            s.scope.maps = [map];
            s.scope.kind = 'map';
        } else {
            delete s.scope.maps;
            s.scope.kind = 'all';
        }
        return s;
    }

    /**
     * The map this spec asks for when that is NOT the map the page is drawing,
     * else null. The canvas and the round rail are server-provided per map, so
     * this is the signal to navigate rather than to re-query - scoping the
     * question to de_dust2 while the radar keeps drawing de_mirage is exactly
     * the contradiction the scope bar exists to prevent.
     */
    function wantedMap(spec, ctx) {
        const c = normalizeCtx(ctx);
        const maps = scopeOf(spec).maps;
        if (!Array.isArray(maps) || maps.length !== 1) return null;
        return maps[0] && maps[0] !== c.map ? maps[0] : null;
    }

    /**
     * Scope one spec to an explicit list of match files - the by-match
     * breakdown row's click target. A single file only counts as "this match"
     * when it IS the entry match; otherwise it is a one-match set, because the
     * header must not say "this match" about a different one.
     */
    function applyFiles(spec, files, ctx) {
        const s = spec && typeof spec === 'object'
            ? JSON.parse(JSON.stringify(spec)) : {};
        s.scope = Object.assign({}, scopeOf(s));
        delete s.scope.maps;
        const list = (files || []).filter(Boolean);
        if (list.length) s.scope.files = list;
        else delete s.scope.files;
        const c = normalizeCtx(ctx);
        s.scope.kind = !list.length ? 'all'
            : (list.length === 1 && list[0] === c.entry) ? 'match' : 'set';
        return s;
    }

    /**
     * Which of the page's offered matches are in scope right now.
     *
     * `offered` is `window.MULTI_MATCHES` - every match on this page's map. A
     * map/all scope means all of them: the rail can only ever show this map's
     * matches anyway, so "all maps" and "this map" are the same rail.
     */
    function filesInScope(spec, ctx, offered) {
        const list = (offered || []).map(m => (typeof m === 'string' ? m : m.file));
        const kind = kindOf(spec, ctx);
        if (kind === 'map' || kind === 'all') return list.slice();
        const want = scopeOf(spec).files || [];
        const inScope = list.filter(f => want.indexOf(f) >= 0);
        // A scope naming nothing this page offers (a shared link from another
        // map) would otherwise empty the rail with no explanation; showing the
        // page's own matches is the honest degrade, and the header still
        // reports the real scope.
        return inScope.length ? inScope : list.slice();
    }

    /**
     * Button label for a scope kind. `counts` = {offered, corpus} - how many
     * matches this map has, and how many exist in total.
     */
    function label(kind, ctx, counts) {
        const c = normalizeCtx(ctx);
        const n = counts || {};
        if (kind === 'match') return 'This match';
        if (kind === 'set') {
            const k = n.inScope || c.files.length || 1;
            // A set of one is a real state (narrowing from the by-match
            // breakdown), and "These 1 matches" reads as a bug.
            return k === 1 ? 'One match' : `These ${k} matches`;
        }
        if (kind === 'map') {
            if (!c.map) return 'All maps';   // no radar loaded: there is no "this map"
            return n.offered ? `All ${n.offered} on ${mapLabel(c.map)}`
                             : `All of ${mapLabel(c.map)}`;
        }
        return n.corpus ? `All maps (${n.corpus})` : 'All maps';
    }

    /**
     * The kinds the bar should offer, in order. `set` only appears when the
     * page actually arrived with a selection - a "These 0 matches" button is
     * noise, and there is no sensible set to switch back to.
     *
     * `liveKind` is the kind currently in force. It is folded in even when the
     * landing context wouldn't have offered it - narrowing to one match from
     * the by-match breakdown produces a `set` on a page that arrived as a whole
     * map, and a bar with no segment lit is a bar claiming the page is
     * something it isn't.
     */
    function kindsFor(ctx, liveKind) {
        const c = normalizeCtx(ctx);
        const out = [];
        // No entry match ⇒ no "This match" button. The corpus-wide entry has
        // none until you pick one (the rail's per-match "only", or the
        // by-match breakdown row), at which point the live kind puts it back.
        if (c.files.length > 1) out.push('set');
        else if (c.entry) out.push('match');
        if (c.files.length > 1 && c.entry) out.splice(1, 0, 'match');
        if (KINDS.indexOf(liveKind) >= 0 && out.indexOf(liveKind) < 0
                && liveKind !== 'map' && liveKind !== 'all') {
            out.push(liveKind);
        }
        if (c.map) out.push('map');
        out.push('all');
        return out;
    }

    /**
     * One line naming what you are looking at, for the page header:
     * "Mirage · this match (09 Jun 13:06)".
     */
    function describe(spec, ctx, offered, counts) {
        const c = normalizeCtx(ctx);
        const kind = kindOf(spec, ctx);
        const list = offered || [];
        const meta = f => list.find(m => m.file === f);
        let what;
        if (kind === 'match') {
            const m = meta((scopeOf(spec).files || [])[0] || c.entry);
            what = 'this match' + (m && m.label ? ` (${m.label})` : '');
        } else if (kind === 'set') {
            const files = scopeOf(spec).files || [];
            if (files.length === 1) {
                const m = meta(files[0]);
                what = 'one match' + (m && m.label ? ` (${m.label})` : '');
            } else {
                what = `these ${files.length} matches`;
            }
        } else if (kind === 'map') {
            what = !c.map ? 'all maps'
                 : (counts && counts.offered) ? `all ${counts.offered} matches`
                 : 'all matches';
        } else {
            what = (counts && counts.corpus)
                ? `all maps (${counts.corpus} matches)` : 'all maps';
        }
        // No map prefix on an all-maps scope: "Mirage · all maps" reads as a
        // contradiction. The radar is still named - by the caller, as the
        // limitation it actually is ("overlay and heatmap draw Mirage only").
        return (c.map && kind !== 'all' ? mapLabel(c.map) + ' · ' : '') + what;
    }

    /**
     * The single match this page is currently about, or null.
     *
     * Not the same question as `ctx.entry`, which is where the page was
     * *opened*. This is where it is *now* - narrow to one match from the round
     * rail or the by-match breakdown and the page has become that match's
     * analyser, whatever URL it was opened at. The rail follows this: the MATCH
     * group (Stats / 2D Replay) appears for it, so "I've filtered down to this
     * match, now show me its scoreboard" is one click instead of a trip through
     * the match list.
     */
    function focusFile(spec, ctx) {
        const c = normalizeCtx(ctx);
        const files = scopeOf(spec).files;
        if (Array.isArray(files) && files.length === 1) return files[0];
        if (kindOf(spec, c) === 'match' && c.entry) return c.entry;
        return null;
    }

    /**
     * Reconcile a spec against the context it has landed in.
     *
     * A spec that names exactly the entry match is "this match", however it was
     * written: focusing a match rewrites the address to `?match=<it>`, so on the
     * next load that same file IS the entry, and a stale `kind:'set'` sentinel
     * would leave the bar saying "One match" about the match the page is now
     * built around.
     */
    function reconcile(spec, ctx) {
        const c = normalizeCtx(ctx);
        const s = spec && typeof spec === 'object'
            ? JSON.parse(JSON.stringify(spec)) : {};
        const sc = scopeOf(s);
        const files = sc.files;
        if (Array.isArray(files) && files.length === 1 && c.entry
                && files[0] === c.entry && sc.kind === 'set') {
            s.scope = Object.assign({}, sc, { kind: 'match' });
        }
        return s;
    }

    /**
     * The entry parameters the address bar should carry for this scope, so a
     * reload lands in the same mode rather than back where the page was first
     * opened. `?q=` still carries the query itself; this is the identity beside
     * it - and it is what makes the rail's two analyser entries reversible.
     */
    function entryParams(spec, ctx) {
        const c = normalizeCtx(ctx);
        const kind = kindOf(spec, c);
        const files = scopeOf(spec).files || [];
        const focus = focusFile(spec, c);
        if (focus) return { match: focus };
        if (kind === 'set' && files.length > 1) return { files: files.join(',') };
        if (kind === 'map' && c.map) return { map: c.map };
        // All maps: pin the radar, or a reload lands on whatever map is newest
        // that day and quietly changes what the overlay can draw.
        return c.map ? { scope: 'all', map: c.map } : { scope: 'all' };
    }

    /** True when the query can return rows the map cannot draw. */
    function spansOtherMaps(spec, ctx) {
        return kindOf(spec, ctx) === 'all';
    }

    return {
        KINDS, emptyCtx, normalizeCtx, kindOf, applyKind, applyFiles,
        filesInScope, label, kindsFor, describe, spansOtherMaps,
        applyMap, wantedMap, focusFile, reconcile, entryParams,
    };
}));
