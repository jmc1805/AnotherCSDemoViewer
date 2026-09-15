/**
 * analyser.js - the Multi Match Analyser's query panel and result list.
 *
 * The page asks questions of the whole corpus and shows the answer three ways:
 * query → moments → views.
 *
 * A question is a boolean tree over pre-derived moments (analysis/moments.py),
 * not a list of rounds someone picked by hand, so "rounds where I got an AWP
 * opening kill AND we lost" is one query rather than an impossible
 * intersection of two selections. What the panel offers:
 *
 *   query panel     built entirely from /analyser/fields, so the filter
 *                   surface is defined once in analysis/query.py
 *   facets          value counts within the current result set, returned with
 *                   every response, so filters read as a distribution
 *   result list     moment rows, each previewable / clippable / jumpable via
 *                   the shared momentrow.js
 *   aggregates      rates carry raw k/n and a Wilson interval
 *   OR + negation   reaches the engine's `any` and `not` nodes; a panel that
 *                   can only build `where.all` cannot ask "opening or clutch"
 *   breakdown       the same result set split by any facet dimension, each
 *                   bucket with its own n; clicking one narrows the query
 *   heatmap         matched moments' own coordinates on the radar - needs no
 *                   round loaded and no playhead
 *
 * Separation from multi.js: multi.js owns the canvas overlay and playback,
 * this owns the query and the page's **scope** (which matches are in play).
 * Scope lives in `spec.scope` and has exactly one writer - here. multi.js
 * issues commands and re-renders from the onScope callback, which is what
 * stops the header, the rail and the query disagreeing about what the page
 * is showing.
 *
 * Exposes: init(opts), getSpec(), setSpec(spec, push), setScope(kind),
 * setScopeFiles(files), getScope(). The heat layer is deliberately NOT
 * exported - the map owns what it draws, and the only way in is the onHeat
 * hook.
 *
 * Requires (loaded first): query.logic.js, scope.logic.js, momentrow.logic.js,
 * momentrow.js.
 */
const Analyser = (() => {
    'use strict';

    const PAGE = 60;                 // rows rendered before "show more"
    const DEBOUNCE_MS = 220;

    let fields = {};                 // field registry from the server
    let presets = [];
    let breakdownDims = [];          // dimensions the result set can be split by
    let scopeCtx = ScopeLogic.emptyCtx();   // the page's scope context (server-computed)
    let lastKind = null;             // last kind reported through onScope
    let bdSort = null;               // {key, dir} - which breakdown column sorts
    let corpus = {};                 // maps / modes / players / matches
    let units = [];
    let spec = QueryLogic.emptySpec();
    let last = null;                 // last result payload
    let shown = PAGE;
    let hooks = {};
    let timer = null;
    let el = {};
    // "any of" mode: while it's on, clicking a value adds it as an ALTERNATIVE
    // to the condition being built instead of narrowing. Sticky rather than a
    // modifier key, because building a three-way OR one alt-click at a time is
    // where a modifier stops being discoverable.
    let orMode = false;
    // Which question a picked name answers. Three roles, because "who did it",
    // "who was with them" and "who they were up against" are three different
    // fields, and picking a name is meaningless until you say which.
    let pickRole = 'player';
    // Heatmap: sticky, so tightening a filter repaints the map instead of
    // making you ask for it again.
    let heatOn = false;

    const esc = s => MomentRowLogic.esc(s);

    // ── boot ──────────────────────────────────────────────────────────────────

    /**
     * Boot the panel: read scope + any ?q= spec from the URL, fetch
     * /analyser/fields, wire the controls, run the first query.
     * @param {Object} options
     * @param {Function} options.onRows       rows -> void, for the map overlay
     * @param {Function} [options.onHeat]     points -> void; absent hides the
     *        heatmap button entirely (a dead button is worse than none)
     * @param {Function} options.onScope      scope -> void, on every change
     * @param {Function} [options.onMapChange] map -> truthy to navigate away.
     *        Hangs off intent (a click), never off onScope - that channel is
     *        also the boot path, so a shared ?q= would navigate on load.
     */
    function init(options) {
        hooks = options || {};
        el = {
            unit:    document.getElementById('q-unit'),
            sort:    document.getElementById('q-sort'),
            presets: document.getElementById('q-presets'),
            active:  document.getElementById('q-active'),
            facets:  document.getElementById('q-facets'),
            summary: document.getElementById('q-summary'),
            count:   document.getElementById('q-count'),
            agg:     document.getElementById('q-aggregates'),
            list:    document.getElementById('q-results'),
            more:    document.getElementById('q-more'),
            status:  document.getElementById('q-status'),
            clear:   document.getElementById('btnClearQuery'),
            addAll:  document.getElementById('btnAddAll'),
            any:     document.getElementById('q-any'),
            group:   document.getElementById('q-group'),
            bd:      document.getElementById('q-breakdown'),
            heat:    document.getElementById('btnHeat'),
            clearOv: document.getElementById('btnClearOverlay'),
            pickSearch: document.getElementById('q-player-search'),
            pickRoles:  document.getElementById('q-player-roles'),
            pickList:   document.getElementById('q-player-list'),
            pickHint:   document.getElementById('q-player-hint'),
        };

        // Scope precedence: a ?q= spec in the URL wins (that is what makes a
        // result set shareable); otherwise the page's ENTRY decides -
        // ?match= means that match, ?files= means those, ?map= means the map.
        // Never default to the whole map, or the page cannot say whether it is
        // showing one match or every match on it.
        scopeCtx = ScopeLogic.normalizeCtx(window.MULTI_SCOPE);
        const fromUrl = QueryLogic.fromQueryString(
            new URLSearchParams(location.search).get('q'));
        // `reconcile` matters on the way back in: focusing a match rewrites the
        // address to ?match=<it>, so on the next load that file is the entry
        // and a spec still labelled 'set' would read as "One match" about the
        // match the page is now built around.
        if (fromUrl) spec = ScopeLogic.reconcile(fromUrl, scopeCtx);
        else spec = ScopeLogic.applyKind(spec, scopeCtx.kind, scopeCtx);

        MomentRow.wire(el.list, { onOverlay: onOverlayClick });
        el.clear.addEventListener('click', () => {
            orMode = false;
            const next = QueryLogic.emptySpec(spec.unit);
            // The breakdown is a lens on the results, not part of the
            // question - clearing the filters shouldn't also collapse the
            // table you were reading. Scope is not a filter either: it is
            // WHERE YOU ARE, so clearing must not silently widen the page from
            // one match to the whole corpus.
            if (spec.group_by) next.group_by = spec.group_by;
            next.scope = QueryLogic.clean(spec).scope;
            setSpec(next);
        });
        el.any.addEventListener('click', () => { orMode = !orMode; renderControls(); renderFacets(); });
        el.group.addEventListener('change', () => {
            const s2 = QueryLogic.clean(spec);
            if (el.group.value) s2.group_by = el.group.value; else delete s2.group_by;
            bdSort = null;
            setSpec(s2, true);
        });
        el.bd.addEventListener('click', onBreakdownClick);
        if (el.heat) {
            // Only offered when there is a canvas to paint on - the module is
            // shared, the seam is one hook, and a dead button is worse than no
            // button.
            el.heat.style.display = hooks.onHeat ? '' : 'none';
            el.heat.addEventListener('click', () => {
                heatOn = !heatOn;
                el.heat.classList.toggle('on', heatOn);
                // Switching it on needs the coordinate payload, which the
                // previous response didn't carry - so re-run rather than
                // paint a partial answer from the rows on screen.
                el.heat.classList.toggle('active', heatOn);
                if (heatOn) run(); else syncHeat();
            });
        }
        el.unit.addEventListener('change', () => setSpec(Object.assign({}, spec, { unit: el.unit.value })));
        el.sort.addEventListener('change', () => setSpec(Object.assign({}, spec, { sort: el.sort.value })));
        el.more.addEventListener('click', () => { shown += PAGE; renderList(); });
        el.addAll.addEventListener('click', () => addToOverlay(last ? last.rows : []));
        if (el.clearOv) {
            // Same seam as onAddRounds: the analyser asks, multi.js owns the
            // overlay. Offered only when the host can actually clear one - a
            // dead button is worse than no button (same rule as the heatmap).
            el.clearOv.style.display = hooks.onClearRounds ? '' : 'none';
            el.clearOv.addEventListener('click', () => {
                if (hooks.onClearRounds) hooks.onClearRounds();
            });
        }
        if (el.pickSearch) el.pickSearch.addEventListener('input', renderPicker);
        if (el.pickRoles) el.pickRoles.addEventListener('click', (e) => {
            const b = e.target.closest('[data-role]');
            if (!b) return;
            pickRole = b.dataset.role;
            renderPicker();
        });
        if (el.pickList) el.pickList.addEventListener('click', (e) => {
            const b = e.target.closest('.q-pick');
            if (b) applyValue(pickRole, b.dataset.name, e.altKey);
        });

        el.facets.addEventListener('click', onFacetClick);
        el.active.addEventListener('click', onActiveClick);
        el.presets.addEventListener('click', onPresetClick);

        fetch('/analyser/fields').then(r => r.json()).then(d => {
            if (!d.ok) throw new Error('fields');
            fields = d.fields; presets = d.presets; corpus = d.corpus; units = d.units;
            breakdownDims = d.breakdowns || [];
            buildStatics();
            // Must run before the first query: the summary, the condition
            // chips and the selects are all rendered from the spec, and on a
            // shared-URL load that spec arrived fully formed.
            renderControls();
            run();
        }).catch(() => {
            el.status.textContent = 'Could not load the query fields.';
        });
    }

    function buildStatics() {
        el.unit.innerHTML = units.map(u =>
            `<option value="${esc(u)}">${esc(QueryLogic.unitLabel(u))}</option>`).join('');
        el.presets.innerHTML = presets.map(p =>
            `<button type="button" class="q-preset" data-id="${esc(p.id)}">${esc(p.name)}</button>`).join('');
        el.group.innerHTML = '<option value="">nothing</option>' + breakdownDims.map(b =>
            `<option value="${esc(b.key)}">${esc(b.label)}</option>`).join('');
        // Corpus size is worth stating up front - it's the denominator behind
        // every number on the page.
        const n = (corpus.matches || []).length;
        el.status.textContent = `${n} match${n === 1 ? '' : 'es'} · ${FmtLogic.num(corpus.n_moments)} moments indexed`;
    }

    // ── query lifecycle ───────────────────────────────────────────────────────

    /**
     * Replace the working spec, sync the URL, and re-run.
     * @param {Object} next        query spec
     * @param {boolean} immediate  skip the input debounce
     */
    function setSpec(next, immediate) {
        spec = QueryLogic.normalize(next);
        syncUrl();
        renderControls();
        clearTimeout(timer);
        if (immediate) run();
        else timer = setTimeout(run, DEBOUNCE_MS);
    }

    function syncUrl() {
        const q = QueryLogic.toQueryString(spec);
        const url = new URL(location.href);
        if (q) url.searchParams.set('q', q); else url.searchParams.delete('q');
        // replaceState, not pushState: editing a filter is not a navigation,
        // and a Back button that walks every keystroke is useless.
        history.replaceState(null, '', url);
    }

    /**
     * POST the current spec to /analyser/query and render rows, facets,
     * aggregates and (when group_by is set) the breakdown table.
     * @returns {Promise<void>} failures render inline, never throw to the page.
     */
    function run() {
        const q = QueryLogic.toQueryString(spec);
        el.list.classList.add('q-busy');
        fetch('/analyser/query?q=' + (q || encodeURIComponent(JSON.stringify(QueryLogic.clean(spec))))
              // The map has to show the whole result set, not the page of rows
              // the list happens to render.
              + (heatOn ? '&points=1' : ''))
            .then(r => r.json())
            .then(d => {
                el.list.classList.remove('q-busy');
                if (!d.ok) { el.status.textContent = d.error || 'Query failed.'; return; }
                last = d;
                shown = PAGE;
                renderCount();
                renderAggregates();
                renderBreakdown();
                renderFacets();
                renderList();
                syncHeat();
            })
            .catch(() => {
                el.list.classList.remove('q-busy');
                el.status.textContent = 'Query failed.';
            });
    }

    // ── controls ──────────────────────────────────────────────────────────────

    function renderControls() {
        el.unit.value = spec.unit;
        el.sort.value = spec.sort;
        el.group.value = spec.group_by || '';
        emitScope();
        el.summary.textContent = QueryLogic.describe(spec, fields);
        const n = QueryLogic.conditionCount(spec);
        el.clear.style.display = n ? '' : 'none';
        el.any.classList.toggle('on', orMode);
        renderPicker();
        renderActive();
    }

    /** The current conditions as chips - the only way to see at a glance what
     *  is actually being asked, and to edit one piece of it. Each chip carries
     *  a ¬ (negate in place) and an × (drop it); an OR group additionally
     *  spells out its alternatives, each individually removable, because a
     *  mis-clicked alternative inside a five-way OR should not cost you the
     *  whole group. */
    function renderActive() {
        const conds = QueryLogic.clean(spec).where.all;
        if (!conds.length) { el.active.innerHTML = ''; return; }
        el.active.innerHTML = conds.map((n, i) => {
            const neg = QueryLogic.isNegated(n);
            const inner = n.not || n;
            const head = `<span class="q-chip-neg" title="Negate this condition">¬</span>`;
            const tail = `<span class="q-chip-x" title="Remove">×</span>`;
            const cls = 'q-chip' + (neg ? ' neg' : '');
            if (Array.isArray(inner.any)) {
                const alts = inner.any.map((leaf, j) =>
                    `<span class="q-alt" data-j="${j}">${esc(QueryLogic.describeNode(leaf, fields))}<span class="q-alt-x" title="Remove this alternative">×</span></span>`
                ).join('<span class="q-or">or</span>');
                return `<span class="${cls} grp" data-i="${i}">${head}<span class="q-grp-kind">${neg ? 'none of' : 'any of'}</span>${alts}${tail}</span>`;
            }
            return `<span class="${cls}" data-i="${i}">${head}${esc(QueryLogic.describeNode(n, fields))}${tail}</span>`;
        }).join('');
    }

    function onActiveClick(e) {
        const chip = e.target.closest('.q-chip');
        if (!chip) return;
        const i = +chip.dataset.i;
        if (e.target.closest('.q-alt-x')) {
            const j = +e.target.closest('.q-alt').dataset.j;
            setSpec(QueryLogic.removeAnyLeaf(QueryLogic.clean(spec), i, j), true);
            return;
        }
        if (e.target.closest('.q-chip-neg')) {
            setSpec(QueryLogic.negateAt(QueryLogic.clean(spec), i), true);
            return;
        }
        if (e.target.closest('.q-chip-x')) {
            setSpec(QueryLogic.removeAt(QueryLogic.clean(spec), i), true);
        }
    }

    function onPresetClick(e) {
        const btn = e.target.closest('.q-preset');
        if (!btn) return;
        const p = presets.find(x => x.id === btn.dataset.id);
        if (!p) return;
        // A preset keeps whatever map scope is set - you almost always want
        // "that question, on this map" rather than losing where you were.
        const next = QueryLogic.normalize(p.spec);
        next.scope = Object.assign({}, spec.scope, next.scope || {});
        setSpec(next, true);
    }

    // ── scope ─────────────────────────────────────────────────────────────────

    /**
     * Tell the page what it is looking at. Fired from renderControls(), which
     * runs on every spec change AND on the shared-URL boot path, so a scope
     * that arrived in a `?q=` propagates through the same single channel as one
     * clicked in the bar. Only actual changes are announced - the rail rebuild
     * on the other end is not free.
     */
    function emitScope(force) {
        if (!hooks.onScope) return;
        const kind = ScopeLogic.kindOf(spec, scopeCtx);
        const key = kind + '|' + JSON.stringify(QueryLogic.clean(spec).scope || {});
        if (!force && key === lastKind) return;
        lastKind = key;
        hooks.onScope({
            kind: kind,
            maps: mapOptions(),
            // Which single match the page is about now (null when it is about
            // several) and how the address bar should describe that.
            focus: ScopeLogic.focusFile(spec, scopeCtx),
            entryParams: ScopeLogic.entryParams(spec, scopeCtx),
            scope: QueryLogic.clean(spec).scope || {},
            ctx: scopeCtx,
            files: ScopeLogic.filesInScope(spec, scopeCtx, window.MULTI_MATCHES || []),
            counts: scopeCounts(),
        });
    }

    /** Maps in the corpus with their match counts - the scope bar's picker. */
    function mapOptions() {
        const counts = {};
        (corpus.matches || []).forEach(m => {
            if (m.map) counts[m.map] = (counts[m.map] || 0) + 1;
        });
        return (corpus.maps || []).map(m => ({ map: m, n: counts[m] || 0 }));
    }

    /**
     * How many matches each scope kind would cover, for the scope bar labels.
     * @returns {{match: number, set: number, map: number, all: number}}
     */
    function scopeCounts() {
        return {
            offered: (window.MULTI_MATCHES || []).length,
            corpus: (corpus.matches || []).length,
            inScope: ((QueryLogic.clean(spec).scope || {}).files || []).length,
        };
    }

    /** Switch the page's scope. The one entry point multi.js uses. */
    function setScope(kind) {
        setSpec(ScopeLogic.applyKind(spec, kind, scopeCtx), true);
    }

    /** Scope to an explicit set of match files (the by-match breakdown row). */
    function setScopeFiles(files) {
        setSpec(ScopeLogic.applyFiles(spec, files, scopeCtx), true);
    }

    // ── facets ────────────────────────────────────────────────────────────────
    // Server-side facet keys are dimension names (by_weapon); the field they
    // filter on is a different name (weapon). This is the mapping, kept here
    // because it is presentation, not data.
    const FACET_FIELD = {
        by_player: 'player', by_weapon: 'weapon', by_weapon_class: 'weapon_class',
        by_side: 'side', by_situation: 'situation', by_phase: 'round_phase',
        by_site: 'bomb_site', by_zone: 'zone', by_buy: 'buy_self', by_map: null, by_match: null,
        by_kind: null,
        // The teamplay pair dimensions. Both are multi-valued: one moment can
        // involve two teammates and belongs under each of them, so their counts
        // deliberately sum past the result count and the UI says so.
        by_mate: 'with_player', by_opp: 'vs_player', by_team: null,
    };

    /** A list field takes `has`/`nhas` and APPENDS, so its clicks route to
     *  QueryLogic.toggleHas instead of toggleFacet. Read off the registry
     *  rather than a second hardcoded list - FIELDS is the source of truth for
     *  the filter surface, and a name kept in two places drifts. */
    function isListField(field) {
        return !!(field && fields[field] && fields[field].type === 'playerlist');
    }

    function facetActive(field, value) {
        return isListField(field) ? QueryLogic.isListActive(spec, field, value)
                                  : QueryLogic.isFacetActive(spec, field, value);
    }

    function facetNegated(field, value) {
        return isListField(field) ? QueryLogic.isListNegated(spec, field, value)
                                  : QueryLogic.isFacetNegated(spec, field, value);
    }

    /** Apply a click on a value of `field`. One place, so the picker, the facet
     *  chips and the breakdown rows can never disagree about what a click means. */
    function applyValue(field, value, exclude) {
        setSpec(isListField(field) ? QueryLogic.toggleHas(spec, field, value, exclude)
                                   : QueryLogic.toggleFacet(spec, field, value, exclude), true);
    }

    // "Which matches" and "which map" are SCOPE, not filters, so clicking
    // either of these two dimensions moves `spec.scope` rather than adding a
    // `where` condition. Both express the same question - which matches am I
    // looking at - and answering it in two places at once lets the page state
    // one scope in its header while querying another. Routing them here also
    // gives the by-match breakdown column something to do: clicking a bucket
    // is how you narrow to a single match.
    const SCOPE_FACET = { by_map: 'map', by_match: 'match' };

    /** A by_match bucket is keyed on the match STEM; scope keys on the file. */
    function fileForStem(stem) {
        const m = (corpus.matches || []).find(x => x.stem === stem || x.file === stem);
        return m ? m.file : null;
    }

    /**
     * Ask the host to move to another map, and report whether it took it.
     *
     * The radar, `MULTI_MATCHES` and the round rail are all server-provided per
     * map, so a map change is a page change - scoping the question to de_dust2
     * while the canvas keeps drawing de_mirage is exactly the contradiction the
     * scope bar exists to prevent. This module still knows nothing about
     * canvases or URLs: it asks, the host navigates (multi.js), and a host
     * without a radar just returns falsy and keeps the in-place behaviour.
     *
     * `keepAll` preserves an all-maps scope while repointing the radar - the
     * scope bar's map picker on the general entry.
     */
    function requestMap(map, keepAll) {
        if (!hooks.onMapChange || !map || QueryLogic.same(map, scopeCtx.map)) return false;
        const next = keepAll ? ScopeLogic.applyKind(spec, 'all', scopeCtx)
                             : ScopeLogic.applyMap(spec, map);
        return hooks.onMapChange(map, QueryLogic.toQueryString(next), keepAll) === true;
    }

    function applyScopeFacet(dim, value) {
        if (SCOPE_FACET[dim] === 'match') {
            const file = fileForStem(value);
            if (!file) return;
            const cur = (QueryLogic.clean(spec).scope || {}).files || [];
            // Clicking the match you are already scoped to widens back out -
            // the same click-twice-undoes rule the facets follow.
            setScopeFiles(cur.length === 1 && cur[0] === file ? [] : [file]);
            return;
        }
        const cur = (QueryLogic.clean(spec).scope || {}).maps || [];
        // Clicking the map you are already scoped to widens back out - no
        // navigation, since the radar doesn't change.
        if (cur.length === 1 && QueryLogic.same(cur[0], value)) { setScope('all'); return; }
        if (requestMap(value, false)) return;
        setSpec(ScopeLogic.applyMap(spec, value), true);
    }

    // Boolean flags worth one-click access, in the order they're most asked
    // about. These aren't facets (a bool has no interesting distribution) but
    // they belong in the same panel.
    const FLAGS = ['opening', 'trade', 'hs', 'through_smoke', 'wallbang', 'noscope',
                   'attacker_blind', 'bomb_planted', 'won_round', 'flash_assist'];

    // ── player picker ─────────────────────────────────────────────────────────
    // The facet list can only offer players who already appear in the current
    // result set, and only the top 12 of them - everyone else sits behind a
    // deliberately unclickable "+N more". With 131 names in the corpus that
    // makes most players unreachable, and a two-player question unaskable
    // without hand-editing ?q=. This is the way in.

    const PICKER_MAX = 24;

    /** The single subject currently filtered on, if there is exactly one. */
    function subjectName() {
        const n = QueryLogic.get(spec, 'player');
        if (!n) return null;
        if (typeof n.v === 'string') return n.v;
        return Array.isArray(n.v) && n.v.length === 1 ? n.v[0] : null;
    }

    /**
     * Who the picker offers, for the role being picked.
     *
     * Scoped, not corpus-wide: standing in one match, a global list is ten
     * names you want and 121 strangers. And once a subject is chosen, "with"
     * offers their teammates and "against" their opponents - the rosters come
     * from /analyser/fields, grouped by fixed team identity, precisely so the
     * list can answer the question being asked instead of listing everybody.
     */
    /**
     * The matches the picker draws its names from: exactly the ones the query
     * is running over.
     *
     * Deliberately derived from spec.scope rather than from
     * ScopeLogic.filesInScope, which is bounded by MULTI_MATCHES - the matches
     * on THIS PAGE'S map. On `scope=all` that would offer the ten players of
     * whichever match happens to be latching the radar while the query ran
     * across the whole corpus. Mirrors query.py's _scope_ok so the picker's
     * population and the query's population are the same set.
     */
    function pickerMatches() {
        const sc = QueryLogic.clean(spec).scope || {};
        const all = corpus.matches || [];
        if (sc.files && sc.files.length) return all.filter(m => sc.files.indexOf(m.file) >= 0);
        if (sc.maps && sc.maps.length)
            return all.filter(m => sc.maps.some(x => QueryLogic.same(x, m.map)));
        return all;
    }

    /**
     * The picker's population, kept as SIDES rather than flattened to names.
     *
     * A flat alphabetical list of ten names cannot answer "who was on my
     * team" - which is half of what the picker exists for, since "with" and
     * "against" only mean anything once you can see the two sides. The
     * rosters arrive from /analyser/fields already split by fixed team
     * identity (1 = first-half CT), so that split is carried through to the
     * UI instead of being thrown away here and guessed at by the reader.
     *
     * Grouped per MATCH, not corpus-wide: team 1 in one match has nothing to
     * do with team 1 in another, so a corpus-wide "Team 1" heading would be a
     * label with no referent. One match in scope renders as two plain team
     * groups; several render as one block each, headed by the match.
     *
     * @returns {Array<Object>} [{file, label, teams: [{id, names}, ...]}]
     */
    function pickerGroups() {
        const subject = subjectName();
        const groups = [];
        pickerMatches().forEach(m => {
            const r = m.roster || {};
            const rosters = { 1: r['1'] || [], 2: r['2'] || [] };
            let ids = [1, 2];
            if (subject && pickRole !== 'player') {
                // Which side the subject is on decides which one is offered:
                // "with" is their own team, "against" is the other.
                const ownId = rosters[1].indexOf(subject) >= 0 ? 1
                    : (rosters[2].indexOf(subject) >= 0 ? 2 : null);
                if (!ownId) return;            // subject didn't play this match
                ids = [pickRole === 'with_player' ? ownId : (ownId === 1 ? 2 : 1)];
            }
            const teams = ids.map(id => ({
                id: id,
                names: rosters[id]
                    .filter(n => n && !(subject && pickRole !== 'player' && n === subject))
                    .slice()
                    .sort((a, b) => a.localeCompare(b)),
            })).filter(t => t.names.length);
            if (teams.length) groups.push({
                file: m.file,
                label: m.played_label || m.stem || m.file || '',
                teams: teams,
            });
        });
        // A corpus whose indexes predate the roster field still gets a picker,
        // just an ungrouped one.
        if (!groups.length && (corpus.players || []).length) {
            groups.push({
                file: '', label: '',
                teams: [{ id: 0, names: (corpus.players || []).slice().sort((a, b) => a.localeCompare(b)) }],
            });
        }
        return groups;
    }

    // Fixed team identity -> the side that team started on, which is what its
    // colour means everywhere else in the app (team 1 = first-half CT = blue,
    // team 2 = orange). Same convention as the Player tab's .pgroup-head.
    const TEAM_SIDE = { 1: 'ct', 2: 't' };

    function pickChip(n) {
        return `<button type="button" class="q-pick${facetActive(pickRole, n) ? ' on' : ''}`
            + `${facetNegated(pickRole, n) ? ' neg' : ''}" data-name="${esc(n)}"`
            + ` title="Click to filter · Alt-click to exclude">${esc(n)}</button>`;
    }

    function renderPicker() {
        if (!el.pickList) return;
        if (el.pickRoles) Array.prototype.forEach.call(
            el.pickRoles.querySelectorAll('[data-role]'),
            b => b.classList.toggle('on', b.dataset.role === pickRole));

        const q = ((el.pickSearch && el.pickSearch.value) || '').trim().toLowerCase();
        const groups = pickerGroups()
            .map(g => ({
                label: g.label,
                teams: g.teams
                    .map(t => ({ id: t.id, names: t.names.filter(n => !q || n.toLowerCase().indexOf(q) >= 0) }))
                    .filter(t => t.names.length),
            }))
            .filter(g => g.teams.length);

        // The cap is on CHIPS and is spent block by block: a scope of eighteen
        // matches must not render 180 buttons, but a block that IS rendered
        // has to be complete, or its team columns read as rosters with players
        // missing - which is the one thing this grouping exists to show.
        let budget = PICKER_MAX, dropped = 0, html = '';
        const multi = groups.length > 1;
        groups.forEach(g => {
            const n = g.teams.reduce((a, t) => a + t.names.length, 0);
            if (budget <= 0) { dropped += n; return; }
            budget -= n;
            const teams = g.teams.map(t => {
                const head = t.id ? `<div class="q-pick-team-head">Team ${t.id}</div>` : '';
                return `<div class="q-pick-team ${TEAM_SIDE[t.id] || ''}">${head}`
                    + `<div class="q-pick-chips">${t.names.map(pickChip).join('')}</div></div>`;
            }).join('');
            html += '<div class="q-pick-block">'
                + (multi && g.label ? `<div class="q-pick-match" title="${esc(g.label)}">${esc(g.label)}</div>` : '')
                + `<div class="q-pick-teams">${teams}</div></div>`;
        });
        const more = dropped
            ? `<span class="q-facet-more">+${dropped} more - keep typing, or narrow the scope</span>`
            : (html ? '' : '<span class="q-facet-more">no players match</span>');
        el.pickList.innerHTML = html + more;

        if (el.pickHint) {
            // The two roles behave differently on a second pick and it would
            // read as a bug unless said out loud: a subject is one person (so
            // two of them is "either"), while teammates are a set (so two of
            // them is "both were in it").
            el.pickHint.textContent = pickRole === 'player'
                ? 'Whose moment it is. Pick two for either of them.'
                : (pickRole === 'with_player'
                    ? 'A teammate in the moment. Pick two for plays that had both.'
                    : 'An opponent in the moment. Pick two for moments against both.');
        }
    }

    function renderFacets() {
        const f = (last && last.facets) || {};
        const flags = FLAGS.filter(k => fields[k]).map(k => {
            const n = QueryLogic.get(spec, k);
            const state = !n ? '' : (n.v === false ? ' neg' : ' on');
            const tip = orMode
                ? 'Click to add as an alternative (OR) to the condition being built'
                : 'Click: require · click again: exclude · again: clear';
            return `<button type="button" class="q-flag${state}" data-flag="${esc(k)}"
                title="${tip}">${esc(fields[k].label)}</button>`;
        }).join('');

        const groups = Object.keys(f).map(key => {
            const field = FACET_FIELD[key];
            const g = f[key];
            const scoped = SCOPE_FACET[key];
            const rows = g.values.map(v => {
                const active = scoped ? isScopeValueActive(key, v.value)
                                      : !!field && facetActive(field, v.value);
                const neg = !scoped && !!field && facetNegated(field, v.value);
                const clickable = !!(field || scoped);
                const cls = 'q-facet' + (active ? ' on' : '') + (neg ? ' neg' : '')
                          + (clickable ? '' : ' q-facet-ro') + (scoped ? ' q-facet-scope' : '');
                const tip = scoped
                    ? (scoped === 'match' ? 'Click to analyse only this match'
                                          : 'Click to analyse only this map')
                    : (field ? 'Click to filter · Alt-click to exclude' : 'Breakdown only');
                // The chip shows the readable form of a map ("Dust 2"); the
                // value it filters on stays the raw key the engine indexes by.
                const shown = key === 'by_map' ? MapsLogic.label(v.value) : v.value;
                return `<button type="button" class="${cls}" data-facet="${esc(key)}" data-value="${esc(v.value)}"
                    ${clickable ? '' : 'disabled'} title="${tip}"
                    ><span class="q-facet-v">${esc(shown)}</span><span class="q-facet-n">${v.n}</span></button>`;
            }).join('');
            // How many VALUES are hidden, not how many moments they hold -
            // "+349 more" next to a list of player names reads as 349 more
            // players when it was really 349 more kills across 23 of them.
            const hidden = Math.max(0, (g.distinct || 0) - g.values.length);
            const more = hidden
                ? `<span class="q-facet-more" title="${g.other} more moments across ${hidden} other values">+${hidden} more</span>`
                : '';
            // A multi-valued group's counts overlap, and a column of numbers
            // that adds up to more than the result count reads as a bug unless
            // it says why.
            const note = g.multi
                ? `<div class="q-group-note">counts overlap - ${FmtLogic.num(g.rows)} moment${g.rows === 1 ? '' : 's'}, each counted under everyone in it</div>`
                : '';
            return `<div class="q-group"><div class="q-group-title">${esc(g.label)}</div>
                ${note}<div class="q-group-body">${rows}${more}</div></div>`;
        }).join('');

        el.facets.innerHTML =
            `<div class="q-group"><div class="q-group-title">Flags</div>
             <div class="q-group-body">${flags}</div></div>` + groups;
    }

    /** Is this map/match facet value what the page is currently scoped to? */
    function isScopeValueActive(dim, value) {
        const sc = QueryLogic.clean(spec).scope || {};
        if (SCOPE_FACET[dim] === 'match') {
            const file = fileForStem(value);
            return !!file && (sc.files || []).indexOf(file) >= 0;
        }
        return (sc.maps || []).some(m => QueryLogic.same(m, value));
    }

    function onFacetClick(e) {
        const flag = e.target.closest('.q-flag');
        if (flag && orMode) {
            setSpec(QueryLogic.addAny(spec, { f: flag.dataset.flag, op: 'is', v: true }), true);
            return;
        }
        if (flag) {
            // Three-state cycle: require → exclude → clear. `is true/false`
            // rather than eq, so a moment kind that has no such attribute is
            // never swept up by the negative case.
            const k = flag.dataset.flag;
            const cur = QueryLogic.get(spec, k);
            let next;
            if (!cur) next = QueryLogic.setCondition(spec, k, 'is', true);
            else if (cur.v !== false) next = QueryLogic.setCondition(spec, k, 'is', false);
            else next = QueryLogic.removeCondition(spec, k);
            setSpec(next, true);
            return;
        }
        const btn = e.target.closest('.q-facet');
        if (!btn || btn.disabled) return;
        if (SCOPE_FACET[btn.dataset.facet]) {
            applyScopeFacet(btn.dataset.facet, btn.dataset.value);
            return;
        }
        const field = FACET_FIELD[btn.dataset.facet];
        if (!field) return;
        if (orMode) {
            setSpec(QueryLogic.addAny(spec, {
                f: field, op: isListField(field) ? 'has' : 'eq', v: btn.dataset.value,
            }), true);
            return;
        }
        applyValue(field, btn.dataset.value, e.altKey);
    }

    // ── results ───────────────────────────────────────────────────────────────

    function renderCount() {
        const d = last;
        el.count.textContent = d.total
            ? `${FmtLogic.num(d.total)} moment${d.total === 1 ? '' : 's'}`
            : 'no moments match';
        // The denominator is the SCOPED moment count (`scanned`, which the
        // engine only increments for in-scope matches), not the whole corpus:
        // "1,204 of 12,143" against a corpus the query was never drawn from is
        // a number that means nothing.
        const scope = ScopeLogic.label(ScopeLogic.kindOf(spec, scopeCtx), scopeCtx, scopeCounts())
            .toLowerCase();
        const inScope = FmtLogic.num(d.scanned);
        el.status.textContent = d.truncated
            ? `showing the first ${d.rows.length} of ${FmtLogic.num(d.total)} · ${scope}`
            : `${FmtLogic.num(d.total)} of ${inScope} moments in scope · ${scope}`;
        el.addAll.style.display = d.total ? '' : 'none';
        el.addAll.textContent = `+ Overlay all ${Math.min(d.rows.length, d.total)}`;
        if (el.clearOv) el.clearOv.style.display = hooks.onClearRounds ? '' : 'none';
    }

    /**
     * The stat strip. Every rate prints its raw k/n beside the percentage and
     * draws its Wilson interval as a lighter span behind the point - over a
     * 16-match corpus a bare percentage invites being quoted as if it were
     * solid, and most of these are built on a few dozen events.
     */
    function renderAggregates() {
        const a = last.aggregates || {};
        if (!a.n) { el.agg.innerHTML = ''; return; }
        // Value first, unit under it. These were .meta-pill chips - the hero
        // band's 11px "label: value" - which set the headline count of the
        // whole query in the same size as the word "moments" beside it.
        const tile = (v, label, cls, title) =>
            `<div class="q-stat${cls ? ' ' + cls : ''}"${title ? ` title="${esc(title)}"` : ''}>`
            + `<span class="q-stat-v">${esc(v)}</span>`
            + `<span class="q-stat-l">${esc(label)}</span></div>`;
        const pills = [
            tile(FmtLogic.num(a.n), 'moments', 'accent'),
            tile(FmtLogic.num(a.n_rounds), a.n_rounds === 1 ? 'round' : 'rounds'),
            tile(FmtLogic.num(a.n_matches), a.n_matches === 1 ? 'match' : 'matches'),
            tile(FmtLogic.num(a.n_players), a.n_players === 1 ? 'player' : 'players'),
        ].join('');

        const rates = (a.rates || []).map(r => {
            const lo = r.ci ? r.ci[0] * 100 : r.p * 100;
            const hi = r.ci ? r.ci[1] * 100 : r.p * 100;
            // Fewer than 5 observations is not a percentage, it's an anecdote.
            const thin = r.n < 5;
            return `<div class="q-rate${thin ? ' thin' : ''}">
                <span class="q-rate-l">${esc(r.label)}</span>
                <span class="q-rate-bar"><i class="ci" style="left:${lo}%;width:${Math.max(hi - lo, 0.6)}%"></i><i class="pt" style="left:${r.p * 100}%"></i></span>
                <span class="q-rate-v">${thin ? '-' : (r.p * 100).toFixed(0) + '%'}</span>
                <span class="q-rate-n">${r.k}/${r.n}${thin ? ' · too few' : ''}</span>
            </div>`;
        }).join('');

        // A mean is a stat too, so it gets the same tile rather than a chip;
        // its n and sd stay on the tooltip, where they were.
        const means = (a.means || []).map(m =>
            tile(m.mean, m.label, '', `${m.label} - mean over ${m.n}, sd ${m.sd}`)
        ).join('');

        el.agg.innerHTML = `<div class="q-stats">${pills}${means}</div>
            <div class="q-rates">${rates}</div>` + histogram(a.time_hist);
    }

    /**
     * The breakdown table: one row per bucket, the same rates as the headline
     * strip but computed within the bucket. Sorting defaults to size, because
     * the biggest bucket is the one the aggregate numbers mostly describe;
     * sorting by a rate instead is one click, and the interval bar under each
     * percentage is what keeps a 2/3 from out-ranking a 200/300 in the reader's
     * head. Clicking a bucket name filters the query down to it - a comparison
     * you can't drill into is a screenshot.
     */
    function renderBreakdown() {
        const b = last && last.breakdown;
        if (!b) { el.bd.innerHTML = ''; return; }
        const scopeDim = SCOPE_FACET[b.by];
        if (!b.groups || !b.groups.length) {
            // An empty table with the select still reading "Weapon" looks
            // broken. Say why instead: a utility moment has no weapon, so
            // there is nothing to group by, and that is an answer.
            el.bd.innerHTML = `<div class="q-bd-note">Nothing to break down: no `
                + `${esc((b.label || '').toLowerCase())} on any of these moments.</div>`;
            return;
        }
        const field = FACET_FIELD[b.by];

        // Columns are whatever the buckets actually have - a utility query has
        // no headshot rate, and an empty column is a question mark, not data.
        const rateKeys = [];
        b.groups.forEach(g => (g.rates || []).forEach(r => {
            if (!rateKeys.some(x => x.key === r.key)) rateKeys.push({ key: r.key, label: r.label });
        }));

        const sort = bdSort || { key: 'n', dir: -1 };
        const value = (g, key) => {
            if (key === 'n') return g.n;
            if (key === 'value') return String(g.value).toLowerCase();
            const r = (g.rates || []).find(x => x.key === key);
            // Buckets too small to mean anything sort last whichever way the
            // column is pointed, instead of topping the table on a 1/1.
            return r && r.n >= 5 ? r.p : null;
        };
        const rows = b.groups.slice().sort((g1, g2) => {
            const a = value(g1, sort.key), c = value(g2, sort.key);
            if (a === c) return g2.n - g1.n;
            if (a === null || a === undefined) return 1;
            if (c === null || c === undefined) return -1;
            return a > c ? -sort.dir : sort.dir;
        });

        const th = (key, label, cls) =>
            `<th data-col="${esc(key)}" class="${sort.key === key ? 'sorted ' : ''}${cls || ''}">${esc(label)}${sort.key === key ? (sort.dir < 0 ? ' ▾' : ' ▴') : ''}</th>`;

        // "players: 1" in every row of a by-player breakdown is noise; the
        // column only says something when a bucket can span people.
        // "1" in every row of a by-player breakdown is noise; so is "2" in
        // every row of a by-teammate one.
        const showPlayers = ['by_player', 'by_mate', 'by_opp'].indexOf(b.by) < 0;
        const head = th('value', b.label) + th('n', 'moments') + '<th>rounds</th>'
            + (showPlayers ? '<th>players</th>' : '')
            + rateKeys.map(r => th(r.key, r.label)).join('');

        const clickable = !!(field || scopeDim);
        const body = rows.map(g => {
            const on = SCOPE_FACET[b.by] ? isScopeValueActive(b.by, g.value)
                                         : field && QueryLogic.isFacetActive(spec, field, g.value);
            const cells = rateKeys.map(rk => {
                const r = (g.rates || []).find(x => x.key === rk.key);
                if (!r) return '<td></td>';
                const thin = r.n < 5;
                const lo = r.ci ? r.ci[0] * 100 : r.p * 100;
                const hi = r.ci ? r.ci[1] * 100 : r.p * 100;
                return `<td title="${r.k} of ${r.n}"><span class="q-cell${thin ? ' thin' : ''}">
                    <span class="q-cell-v">${thin ? '-' : (r.p * 100).toFixed(0) + '%'} <span class="n">${r.k}/${r.n}</span></span>
                    <span class="q-cell-bar"><i class="ci" style="left:${lo}%;width:${Math.max(hi - lo, 0.8)}%"></i><i class="pt" style="left:${r.p * 100}%"></i></span>
                </span></td>`;
            }).join('');
            // As with the facet chips: the cell reads the map's readable name,
            // the data-value it filters on stays the raw key.
            const shown = b.by === 'by_map' ? MapsLogic.label(g.value) : g.value;
            return `<tr><td class="k${on ? ' on' : ''}${clickable ? '' : ' ro'}" data-value="${esc(g.value)}"
                        title="${scopeDim ? 'Click to analyse only this' : field ? 'Click to filter the query to this' : 'Breakdown only'}">${esc(shown)}</td>
                <td class="num">${FmtLogic.num(g.n)}</td><td class="num">${g.n_rounds}</td>
                ${showPlayers ? `<td class="num">${g.n_players}</td>` : ''}${cells}</tr>`;
        }).join('');

        const notes = [];
        if (b.hidden) notes.push(`${b.hidden} smaller bucket${b.hidden === 1 ? '' : 's'} not shown`);
        // Rows the dimension doesn't apply to are stated, never silently
        // dropped: a table whose column total doesn't match the result count
        // is a table nobody can trust.
        if (b.skipped) notes.push(`${b.skipped} moment${b.skipped === 1 ? '' : 's'} have no ${b.label.toLowerCase()}`);
        // On every other dimension the moments column is a partition and the
        // reader is entitled to add it up. Here it is not - a moment with two
        // teammates is counted under both - so the identity is spelled out
        // rather than left to be discovered as a discrepancy.
        if (b.multi) notes.push(
            `${FmtLogic.num(b.placements)} placements across ${FmtLogic.num(b.n_rows)} moments - one moment counts under everyone in it`);
        if (!field && !scopeDim) notes.push('this dimension is a breakdown only - it has no matching filter');
        if (scopeDim) notes.push('click a row to scope the whole page to it');

        el.bd.innerHTML = `<table class="q-bd"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`
            + (notes.length ? `<div class="q-bd-note">${esc(notes.join(' · '))}</div>` : '');
    }

    function onBreakdownClick(e) {
        const th = e.target.closest('th[data-col]');
        if (th) {
            const key = th.dataset.col;
            const cur = bdSort || { key: 'n', dir: -1 };
            bdSort = { key: key, dir: cur.key === key ? -cur.dir : -1 };
            renderBreakdown();
            return;
        }
        const cell = e.target.closest('td.k');
        if (!cell || !last || !last.breakdown) return;
        const dim = last.breakdown.by;
        if (SCOPE_FACET[dim]) { applyScopeFacet(dim, cell.dataset.value); return; }
        const field = FACET_FIELD[dim];
        if (!field) return;
        applyValue(field, cell.dataset.value, e.altKey);
    }

    /**
     * One aggregate distribution as an inline bar row.
     * @param {{buckets: Array<{label: string, n: number}>}} h
     * @returns {string} HTML
     */
    function histogram(h) {
        if (!h || !h.bins || !h.bins.length) return '';
        const max = Math.max.apply(null, h.bins) || 1;
        const bars = h.bins.map((n, i) =>
            `<i style="height:${Math.round(100 * n / max)}%" title="${i * h.bin_s}–${(i + 1) * h.bin_s}s: ${n}"></i>`
        ).join('');
        return `<div class="q-hist"><div class="q-hist-title">When in the round</div>
            <div class="q-hist-bars">${bars}</div>
            <div class="q-hist-axis"><span>0s</span><span>${h.bins.length * h.bin_s}s</span></div></div>`;
    }

    function renderList() {
        if (!last) return;
        const rows = last.rows.slice(0, shown);
        // Tear down any mounted preview before the list is replaced, or its
        // render loop keeps running against a detached node.
        MomentRow.teardown(el.list);
        el.list.innerHTML = rows.map((r, i) => MomentRowLogic.rowHtml(r, {
            idPrefix: 'q', index: i,
            showType: true, showPlayer: true, showMatch: true, showOther: true,
            actions: ['preview', 'record', 'demo', 'viewer', 'overlay'],
        })).join('') || '<div class="q-empty">Nothing matches this query.</div>';
        MomentRow.primeClips(el.list, rows, 'q');
        el.more.style.display = last.rows.length > shown ? '' : 'none';
        el.more.textContent = `show ${Math.min(PAGE, last.rows.length - shown)} more`;
    }

    /**
     * Push the current result set's coordinates at the map, or clear them.
     *
     * Only rows that HAVE coordinates go: clutch, multikill and round moments
     * carry none (they describe a round, not a spot), and silently plotting
     * them at 0,0 would put a permanent hotspot in the corner of every radar.
     * The count of what could not be placed is reported instead.
     */
    function syncHeat() {
        if (!hooks.onHeat || !el.heat) return;
        el.heat.classList.toggle('on', heatOn);
        if (!heatOn || !last) { hooks.onHeat(null); return; }
        // `points` covers every hit; the rows are only a page of them. Falling
        // back to the rows keeps the layer working if a response predates the
        // toggle (the very first click, before its re-run lands).
        const payload = last.points;
        const points = payload
            ? payload.pts
            : last.rows.filter(r => typeof r.x === 'number' && typeof r.y === 'number')
                       .map(r => ({ x: r.x, y: r.y, map: r.map }));
        const noCoords = payload ? payload.missing : (last.rows.length - points.length);
        hooks.onHeat({
            points: points,
            mode: points.length < 25 ? 'points' : 'heat',
            label: QueryLogic.unitLabel(spec.unit)
                + (noCoords ? ` · ${noCoords} without a position` : '')
                + (payload ? '' : ` · first ${last.rows.length} of ${last.total}`),
        });
    }

    // ── overlay hand-off ──────────────────────────────────────────────────────

    function onOverlayClick(mid) {
        const i = +String(mid).split('-').pop();
        const row = last && last.rows[i];
        if (row) addToOverlay([row]);
    }

    /**
     * Push matched moments onto the map overlay. The overlay's selection unit
     * is still a whole round, so a moment is added as its round; a later pass
     * makes the segment itself the unit so the tracks can be aligned on the
     * moment instead of the round start.
     */
    function addToOverlay(rows) {
        if (!hooks.onAddRounds || !rows || !rows.length) return;
        const seen = new Set();
        const picks = [];
        rows.forEach(r => {
            if (r.round == null) return;
            const key = r.match + ':' + r.round;
            if (seen.has(key)) return;
            seen.add(key);
            picks.push({ match: r.match, map: r.map, round: r.round,
                         tick: r.tick, player: r.player });
        });
        hooks.onAddRounds(picks);
    }

    return { init, getSpec: () => QueryLogic.clean(spec), setSpec,
             setScope, setScopeFiles, requestMap,
             getScope: () => ScopeLogic.kindOf(spec, scopeCtx) };
    // (heatOn is intentionally not exported: the map owns what it draws, and
    // the only way in is the onHeat hook.)
})();
