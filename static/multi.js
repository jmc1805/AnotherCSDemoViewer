/**
 * multi.js - the canvas half of the Multi Match Analyser (`/multi`).
 *
 * Overlays rounds from one or many matches on the same map. A selection is a
 * (match, round) pair; every selected round is aligned to its own start
 * (round-relative t=0), so one playhead shows the same moment of each round
 * stacked on top of the others - players, trails and all utility.
 *
 * Two views:
 *   Playback  live round-relative playback with a single playhead
 *   Density   no playhead; accumulates paths, deaths and utility across every
 *             selected round inside a [from..to] round-time window
 *
 * This file owns the canvas, the round rail and playback. It does NOT build
 * queries - analyser.js does, and owns the scope. They meet at exactly three
 * hooks, all called by analyser.js:
 *   addQueryRounds(rows)  put matched moments' rounds on the map
 *   setQueryHeat(points)  hand over coordinates for the heatmap layer
 *   onScope(scope)        report the effective scope so the rail re-renders
 * Plus one going the other way: requestMap() -> goToMap(), because changing
 * map is a navigation (MATCHES and the radar are server-provided per map),
 * not a re-query.
 *
 * Two invariants that break things silently:
 *
 *   1. Tick numbers collide between demos, so tick data lives strictly per
 *      match (M.tickMap / M.sortedTicks). Only round-relative samples mix.
 *   2. MATCHES indices are load-bearing - matchColor, selectedRounds[].m, the
 *      `mhead-/mbody-/rgrid-<mi>` element ids and addQueryRounds' findIndex
 *      all key on position. Never rebuild MATCHES from an in-scope subset.
 *
 * Match documents load through `/data/slim/<file>` (the match minus
 * grenades[], ~1 MB instead of ~28 MB); ensureGrenades(M) upgrades one to the
 * full document only when nade trails are actually switched on.
 *
 * Render-order and multi-level-map conventions are the same as viewer.js -
 * see that file's header.
 */

// ─── DOM ──────────────────────────────────────────────────────────────────
const canvas        = document.getElementById('mapCanvas');
const ctx           = canvas.getContext('2d');
const timeline      = document.getElementById('timeline');
const timelineA     = document.getElementById('timelineA');
const timeLabel     = document.getElementById('timeLabel');
const loadingEl     = document.getElementById('loading');
const matchSections = document.getElementById('match-sections');
const playerGroups  = document.getElementById('player-groups');
const playerSearch  = document.getElementById('player-search');
const statusEl      = document.getElementById('status');
const legendEl      = document.getElementById('round-legend');
const heatLegendEl  = document.getElementById('heat-legend');
const scopeSegsEl   = document.getElementById('sb-segs');
const scopeWhatEl   = document.getElementById('sb-what');
const heroLoadedEl  = document.getElementById('hero-loaded');
const railScopeEl   = document.getElementById('rail-scope');
const scopeMapEl    = document.getElementById('sb-map');
const railCtxEl     = document.getElementById('rail-ctx');
const railAnalyserEl = document.getElementById('rail-analyser');

// ─── Map Library (shared with viewer.js/match.html - static/match2d.logic.js) ─
const MAP_LIBRARY = Match2DLogic.MAP_LIBRARY;

// ─── Constants ──────────────────────────────────────────────────────────────
const TICKRATE             = Match2DLogic.TICKRATE;
const DEFAULT_ROUND_TICKS  = 115 * TICKRATE;   // fallback round length
const HE_FADE_TICKS        = Match2DLogic.HE_FADE_TICKS;
const FLASH_FADE_TICKS     = TICKRATE * 1.5;
const GRENADE_LOOKBACK     = 24;
const SPRAY_DURATION_TICKS = Match2DLogic.SPRAY_DURATION_TICKS;
const PLAYBACK_SOFT_CAP    = 10;               // rounds; above this suggest Density
const PLAYER_TRAIL_TICKS   = Match2DLogic.PLAYER_TRAIL_TICKS;   // ~1.5s - same fixed window as viewer.js

// Footstep audibility (same model as viewer.js): CS2's silent-movement cutoff
// is ~135 world units/sec. Above it, a static circle scales with speed toward
// a full ~250-velocity sprint (RUN_SPEED_REF). Radii are true to the real
// audible range (confirmed in-game on de_dust2: a player running short/catwalk
// corner is audible from top-mid barrels, ~1400+ units away) - the ring is
// thin and mostly transparent, so a large true-to-scale circle still reads
// fine. Speed is smoothed over a long backward window (see smoothedSpeed) and
// FADE_MARGIN turns the on/off threshold into a fade - raw per-tick velocity
// is noisy enough that both were needed to stop the circle feeling twitchy.
const AUDIBLE_SPEED       = Match2DLogic.AUDIBLE_SPEED;
const RUN_SPEED_REF       = Match2DLogic.RUN_SPEED_REF;
const FOOTSTEP_RADIUS_MIN = Match2DLogic.FOOTSTEP_RADIUS_MIN;
const FOOTSTEP_RADIUS_MAX = Match2DLogic.FOOTSTEP_RADIUS_MAX;
const FOOTSTEP_FADE_MARGIN = Match2DLogic.FOOTSTEP_FADE_MARGIN;
const FOOTSTEP_SMOOTH_SAMPLES = 32;   // trailing samples to average (~1s at this track's sample spacing)

// Distinct hues, used for rounds (selection order) and matches (match index).
const ROUND_PALETTE = [
    '#4a9eff', '#ff5d5d', '#54d98c', '#ffb627', '#c77dff', '#22d3ee',
    '#ff7ac6', '#a3e635', '#fb923c', '#818cf8', '#f43f5e', '#2dd4bf',
];

// ─── Fixed team identity (ported verbatim from templates/match.html) ───────
// Team 1 = first-half-CT roster, Team 2 = first-half-T roster - stays fixed
// across the halftime side swap, unlike the literal per-round `winner` side.
const REGULATION_HALF   = Match2DLogic.REGULATION_HALF;
const REGULATION_ROUNDS = Match2DLogic.REGULATION_ROUNDS;
const OT_HALF_LEN       = Match2DLogic.OT_HALF_LEN;
const isSwappedAtRound  = Match2DLogic.isSwappedAtRoundSimple;
// Which roster team (1|2) occupies `side` ('ct'|'t') in round `rNum`.
/**
 * Fixed team identity for a side in a round, across the halftime swap.
 * @param {number} rNum  round number
 * @param {string} side  'ct' | 't'
 * @returns {1|2} 1 = first-half CT, 2 = first-half T
 */
function teamIdForSide(rNum, side) {
    const swapped = isSwappedAtRound(rNum);
    if (side === 'ct') return swapped ? 2 : 1;
    return swapped ? 1 : 2;
}

// ─── State ────────────────────────────────────────────────────────────────
let mapName       = null;
let mapConfig     = null;
let mapImg        = new Image();
let mapImageReady = false;

// ── Multi-level maps (Nuke, Vertigo) - see static/maplayers.logic.js ────────
// Same model as the 2D replayer: `layerMode` is the user's choice, `shownLayer`
// is what it resolves to for the frame being drawn, and it is null on every
// single-level map, which is what keeps all of this inert elsewhere.
let layerMode  = 'auto';
let shownLayer = null;
const mapImgLower = new Image();
let mapImgLowerReady = false, mapImgLowerRequested = false;

// Hero band totals (static/matchhero.js) - running sums across every match
// loaded so far, since the analyser can overlay several matches on one map.
let heroRoundsTotal = 0;
let heroKillsTotal  = 0;
let heroDmgTotal    = 0;
let heroRanksAcc    = [];

// One entry per same-map match offered by the server; master data lazy-loaded.
// Every match on this page's map - the OFFERED superset, not the in-scope
// subset. Indices are load-bearing (matchColor, selectedRounds[].m, the
// mhead-/mbody-/rgrid- DOM ids, addQueryRounds' findIndex), so scope is a flag
// over a fixed array rather than a rebuild; widening then costs no fetch and
// renumbers nothing.
const MATCHES = (window.MULTI_MATCHES || []).map((m, i) => ({
    idx: i, file: m.file, label: m.label || m.file, mode: m.mode || '',
    loaded: false, loadPromise: null,
    master: null, rounds: [], roundByNum: new Map(),
    source: null, chunkIndex: [], chunkCache: new Map(),
    tickMap: new Map(), sortedTicks: [],
    grenades: [], shots: [],
    firstHalfSide: null,
}));

let roster = [];                  // merged across loaded matches [{name, side, matches:Set}]

// Per-match round filters. A round matches a def iff its literal winner side
// equals `side` AND that side is occupied by roster team `team` in that round
// (teamIdForSide) - so the buckets follow a team across the halftime swap
// rather than following a colour. The four are mutually disjoint per match,
// which is what lets them be additive: any combination can be checked at once
// and each simply contributes its own rounds.
const ROUND_FILTER_DEFS = [
    { key: 'ct1', label: 'T1 CT-wins', team: 1, side: 'ct' },
    { key: 't1',  label: 'T1 T-wins',  team: 1, side: 't'  },
    { key: 'ct2', label: 'T2 CT-wins', team: 2, side: 'ct' },
    { key: 't2',  label: 'T2 T-wins',  team: 2, side: 't'  },
];
function roundMatchesDef(r, def) {
    return (r.winner || '').toLowerCase() === def.side && teamIdForSide(r.round_num, def.side) === def.team;
}

// Selections
let selectedRounds  = [];         // ordered [{m: matchIdx, rn: round_num}]
let selectedPlayers = new Set();  // player names
let focusedPlayers  = new Set();  // names drawn highlighted; everyone else dimmed
const LABEL_TRACK_CAP = 12;       // above this many tracks, labels only on focused players (Auto mode)
let trackCount = 0;               // total player tracks in the current selection
let includeFreeze   = false;
let showFootsteps   = false;   // pulse a ring on players moving loud enough to be heard
let colorMode       = 'round';    // 'round' | 'match' | 'team'
let showTrails      = false;      // same on/off model as the 2D viewer (display.trails)
let showUtil        = { smoke: true, fire: true, flash: true, he: true, nade: true, drops: true, bomb: true };

// HUD size for the in-canvas kill feed. SHARES viewer.js's storage key on
// purpose: both pages draw the same feed, from the same module, onto a canvas
// of the same 1024x1024 backing size that CSS then scales - so it is one
// per-user display preference ("the feed is too small on my monitor"), not
// two. That is also why it persists at all when every other toggle here is
// per-analysis and resets.
//
// Reads and writes are wrapped: private mode and blocked site data both throw
// on access, and a display preference must never cost the page.
const HUD_SCALE_KEY = 'cs2viewer.hudScale';
let hudScale = (() => {
    try {
        const v = parseFloat(localStorage.getItem(HUD_SCALE_KEY));
        return (v >= 0.6 && v <= 2) ? v : 1;
    } catch (e) { return 1; }
})();

/**
 * Run `draw` with the context scaled about a fixed anchor.
 *
 * One transform rather than a multiplier threaded through killfeed.js's ~dozen
 * hardcoded sizes - fonts, icons, row pitch and gaps all follow by
 * construction, and killfeed.js needs no change at all (its `gap`/`iconH` are
 * locals it does not expose). The anchor is the load-bearing part: the feed is
 * pinned to the top-right and must GROW FROM that corner, so scaling about it
 * leaves anything drawn at `ax` exactly at `ax`. A bare ctx.scale() would
 * multiply those positions and walk the feed off the canvas.
 */
function withHudScale(ax, ay, draw) {
    if (hudScale === 1) { draw(); return; }
    ctx.save();
    ctx.translate(ax, ay);
    ctx.scale(hudScale, hudScale);
    ctx.translate(-ax, -ay);
    draw();
    ctx.restore();
}
let viewMode        = 'play';     // 'play' | 'density'
// Query heatmap: the analyser's current result set as map positions. It is
// deliberately independent of `selectedRounds` - the rows already carry the
// subject's own coordinates, so "where does this happen" needs no round loaded,
// no chunk fetched and no playhead. {points, label, mode} or null.
let queryHeat       = null;
// What the page is analysing right now, as reported by the analyser (the only
// writer - see the `onScope` hook at the bottom). multi.js renders the header
// bar and filters the round rail from this, and never sets it itself: one
// writer plus one event is what keeps the header, the rail and the query from
// disagreeing about which matches are in scope.
let scopeState      = null;
const SCOPE_CTX     = (window.MULTI_SCOPE || {});
let nameMode        = 'all';      // 'auto' | 'all' | 'off' - player name label visibility
let utilityOwners   = new Set();  // player names; empty = everyone's utility (additive spotlight)

// Derived per rebuild - keyed by "m:rn"
let tracks    = {};               // key -> { t0, tEnd, len, players: Map(name->samples[]) }
let roundUtil = {};               // key -> { smokes, infernos, flashes, he, grenades:Map, shots }
let maxLen    = 0;                // longest selected round, in ticks
let relTick   = 0;                // playback playhead
let winA      = 0, winB = 0;      // density window [from..to], round-relative ticks

let isPlaying     = false;
let speedMult     = 1.0;
let lastFrameTime = 0;

// Shared pan/zoom view transform (static/viewport.js, §2) - imported verbatim so
// the overlay gets wheel-zoom-at-cursor, drag-to-pan and dbl-click reset. worldToCanvas()
// returns BASE (un-zoomed) coords; render() draws the map + entities under the
// transform, then clears it for the screen-space kill feed.
const view      = ViewportLogic.create({ minScale: 1, maxScale: 8 });
let feedKills   = [];             // kills across selected rounds, round-relative, asc by tick

const keyOf      = sel => `${sel.m}:${sel.rn}`;
const matchColor = mi  => ROUND_PALETTE[mi % ROUND_PALETTE.length];
const roundColor = i   => ROUND_PALETTE[i % ROUND_PALETTE.length];
// Colour for a selection's util/trails: match hue in match mode, else selection order.
const selColor   = (sel, k) => colorMode === 'match' ? matchColor(sel.m) : roundColor(k);

// ─── Helpers ────────────────────────────────────────────────────────────────
function worldToCanvas(wx, wy) { return Match2DLogic.worldToCanvas(mapConfig, wx, wy); }
function isOnCanvas(p, m = 40) {
    return ViewportLogic.isVisible(view, p.x, p.y, canvas.width, canvas.height, m);
}
const easeOut = Match2DLogic.easeOut;
function formatClock(secs) {
    const sign = secs < 0 ? '-' : '';
    secs = Math.abs(secs);
    const m = Math.floor(secs / 60);
    const s = Math.floor(secs % 60).toString().padStart(2, '0');
    return `${sign}${m}:${s}`;
}
function showLoading(msg) { loadingEl.textContent = msg || 'Loading…'; loadingEl.style.display = 'flex'; }
function hideLoading()    { loadingEl.style.display = 'none'; }
function setStatus(msg)   { statusEl.textContent = msg; statusEl.title = msg; }

function roundEndTick(M, r) {
    const idx = M.rounds.indexOf(r);
    const next = M.rounds[idx + 1];
    const t0 = r.freeze_end ?? r.start;
    return r.end ?? next?.start ?? (t0 + DEFAULT_ROUND_TICKS);
}
function roundStartTick(r) { return includeFreeze ? r.start : (r.freeze_end ?? r.start); }

// Largest index in ascending `arr` whose value <= x (binary search). -1 if none.
function lowerIdx(arr, x, key) {
    let lo = 0, hi = arr.length - 1, res = -1;
    while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        const v = key ? key(arr[mid]) : arr[mid];
        if (v <= x) { res = mid; lo = mid + 1; } else { hi = mid - 1; }
    }
    return res;
}

// ═══════════════════════════════════════════════════════════════════════════
// Match loading (lazy - master JSONs are large)
// ═══════════════════════════════════════════════════════════════════════════
/**
 * Fetch and register one match, lazily, on first expand.
 * @param {number} mi  index into MATCHES - load-bearing, see the file header
 * @returns {Promise<Object>} the match record M: {file, data, tickMap,
 *   sortedTicks, slim, sideMap, ...}. Loads `/data/slim/<file>`,
 *   so M.data has no grenades[] until ensureGrenades(M) upgrades it.
 */
function loadMatch(mi) {
    const M = MATCHES[mi];
    if (M.loadPromise) return M.loadPromise;
    M.loadPromise = (async () => {
        showLoading(`Loading ${M.label}…`);
        // Slim document: everything except grenades[], which is ~95% of a
        // match (205k trajectory points, ~28 MB parsed vs ~1.1 MB without).
        // Overlaying several matches at once would otherwise cost hundreds of
        // MB of heap for data that is only ever drawn as nade trails.
        // ensureGrenades() fetches the full document if and when it's needed.
        const res = await fetch(`/data/slim/${M.file}`);
        if (!res.ok) throw new Error(`${M.file}: HTTP ${res.status}`);
        const data = await res.json();

        M.master     = data;
        M.slim       = !data.grenades;
        M.rounds     = (data.rounds || []).slice().sort((a, b) => a.start - b.start);
        M.roundByNum = new Map(M.rounds.map(r => [r.round_num, r]));
        M.source     = ChunkSource.create(data);   // v2 binary chunks preferred
        M.chunkIndex = M.source.entries;
        M.grenades   = data.grenades || [];
        M.shots      = data.shots || [];
        M.items      = GroundItemsLogic.prepare(data.items);

        // Legacy flat-ticks fallback: treat as already-loaded.
        if (!M.chunkIndex.length && data.ticks) {
            for (const [k, v] of Object.entries(data.ticks)) M.tickMap.set(Number(k), v);
            M.sortedTicks = [...M.tickMap.keys()].sort((a, b) => a - b);
        }

        if (!mapName) {
            mapName = MapsLogic.canonical(data.mapName);
            mapConfig = Match2DLogic.mapConfigFor(mapName);
            mapImg.onload  = () => { mapImageReady = true;  render(); };
            mapImg.onerror = () => { mapImageReady = false; render(); };
            mapImg.src = MapsLogic.radarUrl(mapName);
            updateLayerButtons();   // mapName is only known now
        }

        // Hero band (map icon/name/mode, avg rank, rounds/kills/dmg pills) -
        // shared with the stats page / 2D viewer via matchhero.js. Totals run
        // across every match loaded so far, since this page can overlay several.
        // No faceitId for the same reason: one room link under a band that is
        // summing several matches would name whichever happened to load first.
        heroRoundsTotal += M.rounds.length;
        heroKillsTotal  += (data.kills || []).length;
        heroDmgTotal    += window.MatchHeroLogic ? MatchHeroLogic.totalDamage(data.damage) : 0;
        heroRanksAcc     = heroRanksAcc.concat(data.ranks || []);
        if (window.MatchHero) {
            MatchHero.update({
                mapRaw: mapName, mode: M.mode, ranks: heroRanksAcc,
                rounds: heroRoundsTotal, kills: heroKillsTotal, totalDmg: heroDmgTotal,
            });
            document.getElementById('match-hero').style.display = '';
        }

        M.loaded = true;          // before mergeRoster - buildPlayerGroups filters on it
        mergeRoster(data, mi);
        if (window.Assets) await Assets.load();   // round-grid icons need the manifest resolved once
        buildRoundGridFor(mi);
        hideLoading();
        return M;
    })().catch(err => {
        M.loadPromise = null;
        hideLoading();
        setStatus(`Failed to load ${M.label}`);
        console.error(err);
        throw err;
    });
    return M.loadPromise;
}

// Roster: union of player names from kills (covers every player, no chunk load
// needed). Side is a first-seen grouping hint; real side comes from tick data.
// Each entry records which matches the player appears in.
function mergeRoster(data, mi) {
    const byName = new Map(roster.map(p => [p.name, p]));
    const seen = new Map();
    for (const k of (data.kills || [])) {
        if (k.attacker_name && !seen.has(k.attacker_name)) seen.set(k.attacker_name, (k.attacker_side || '').toLowerCase());
        if (k.victim_name   && !seen.has(k.victim_name))   seen.set(k.victim_name,   (k.victim_side   || '').toLowerCase());
    }
    for (const [name, side] of seen) {
        let p = byName.get(name);
        if (!p) {
            p = { name, side, matches: new Set() };
            roster.push(p);
            byName.set(name, p);
            selectedPlayers.add(name);   // new names join the selection by default
        }
        p.matches.add(mi);
    }
    roster.sort((a, b) => (a.side === b.side ? a.name.localeCompare(b.name) : (a.side === 'ct' ? -1 : 1)));
    buildPlayerGroups();
    syncSelectionUI();
}

// Per-match fixed team identity for the Player panel (ported from
// templates/match.html's ensureSideMap/teamIdOf, scoped to a match object `M`
// instead of a page-global - this page can have several matches loaded at
// once, each with its own roster/halftime schedule). A player's SIDE swaps at
// halftime, so we derive each player's FIRST-HALF side once (from the side-
// carrying kill/damage events, translated back through isSwappedAtRound) and
// resolve their roster-team from that.
function normSide(s) {
    s = (s || '').toLowerCase();
    return s === 'ct' ? 'ct' : (s === 't' || s === 'terrorist') ? 't' : null;
}
function ensureMatchSideMap(M) {
    if (M.firstHalfSide) return M.firstHalfSide;
    const m = new Map();
    const note = (name, side, rnum) => {
        const s = normSide(side);
        if (!name || !s || rnum == null || m.has(name)) return;
        m.set(name, isSwappedAtRound(rnum) ? (s === 'ct' ? 't' : 'ct') : s);   // back to first-half frame
    };
    (M.master?.kills || []).forEach(k => { note(k.attacker_name, k.attacker_side, k.round_num); note(k.victim_name, k.victim_side, k.round_num); });
    (M.master?.damage || []).forEach(d => { note(d.attacker_name, d.attacker_side, d.round_num); note(d.victim_name, d.victim_side, d.round_num); });
    M.firstHalfSide = m;
    return m;
}
function matchTeamIdOf(M, name) {   // 1 = first-half CT, 2 = first-half T, 0 = unknown
    const fh = ensureMatchSideMap(M).get(name);
    return fh === 'ct' ? 1 : fh === 't' ? 2 : 0;
}

// ═══════════════════════════════════════════════════════════════════════════
// Grenade trajectories (deferred - see loadMatch)
// ═══════════════════════════════════════════════════════════════════════════

/** Does this match need its grenades[] resolved for what's currently drawn? */
function needsGrenades(M) {
    if (!M.slim) return false;
    if (showUtil.nade) return true;
    // Pre-2026-07-18 parses have no `weapon` on infernos, so molotov vs CT
    // incendiary can only be recovered by correlating fire-nade trajectories
    // (UtilFXLogic.classifyInfernos). Without them every fire would render as
    // a T molotov - wrong radius and wrong rim colour.
    return (M.master?.infernos || []).some(i => !i.weapon);
}

/** Upgrade a slim match to the full document, once. Idempotent. */
function ensureGrenades(M) {
    if (!M.slim) return Promise.resolve();
    if (!M.fullPromise) {
        M.fullPromise = fetch(`/data/${M.file}`)
            .then(r => r.ok ? r.json() : null)
            .then(full => {
                if (!full) return;
                M.grenades = full.grenades || [];
                M.master.grenades = M.grenades;
                M.slim = false;
            })
            .catch(() => {});   // trails simply stay absent; nothing else breaks
    }
    return M.fullPromise;
}

/** Resolve grenades for every match backing the current selection. */
function ensureGrenadesForSelection() {
    const need = [...new Set(selectedRounds.map(s => s.m))]
        .map(mi => MATCHES[mi])
        .filter(M => M && M.loaded && needsGrenades(M));
    if (!need.length) return Promise.resolve();
    return Promise.all(need.map(ensureGrenades));
}

// ═══════════════════════════════════════════════════════════════════════════
// Chunk loading (only chunks covering selected rounds, per match)
// ═══════════════════════════════════════════════════════════════════════════
function chunksForRounds(M, rnums) {
    const need = new Set();
    rnums.forEach(rn => {
        for (let i = 0; i < M.chunkIndex.length; i++) {
            const c = M.chunkIndex[i];
            if (rn >= c.roundStart && rn <= c.roundEnd) need.add(i);
        }
    });
    return need;
}

async function fetchChunk(M, idx) {
    if (M.chunkCache.has(idx)) return M.chunkCache.get(idx);
    const data = await M.source.fetchDict(idx);
    M.chunkCache.set(idx, data);
    return data;
}

async function ensureChunks() {
    const byMatch = new Map();   // matchIdx -> Set(round_num)
    for (const sel of selectedRounds) {
        if (!byMatch.has(sel.m)) byMatch.set(sel.m, new Set());
        byMatch.get(sel.m).add(sel.rn);
    }
    const tasks = [];
    for (const [mi, rset] of byMatch) {
        const M = MATCHES[mi];
        for (const ci of chunksForRounds(M, [...rset])) {
            if (!M.chunkCache.has(ci)) tasks.push(fetchChunk(M, ci));
        }
    }
    if (tasks.length) {
        showLoading(`Loading ${tasks.length} chunk${tasks.length > 1 ? 's' : ''}…`);
        await Promise.all(tasks);
        hideLoading();
    }
    for (const [mi, rset] of byMatch) mergeLoadedChunks(MATCHES[mi], [...rset]);
}

// Rebuild a match's tickMap from the chunks covering its selected rounds, so
// memory stays bounded to what's on screen.
function mergeLoadedChunks(M, rnums) {
    if (!M.chunkIndex.length) return;   // legacy flat-ticks: tickMap is already final
    M.tickMap.clear();
    for (const idx of chunksForRounds(M, rnums)) {
        const c = M.chunkCache.get(idx);
        if (!c) continue;
        for (const [k, v] of Object.entries(c)) M.tickMap.set(Number(k), v);
    }
    M.sortedTicks = [...M.tickMap.keys()].sort((a, b) => a - b);
}

// ═══════════════════════════════════════════════════════════════════════════
// Precompute tracks + utility for the current selection
// ═══════════════════════════════════════════════════════════════════════════
async function rebuild() {
    if (!selectedRounds.length) {
        tracks = {}; roundUtil = {}; maxLen = 0; relTick = 0;
        timeline.max = 0; timeline.value = 0; timelineA.max = 0;
        buildLegend(); render(); setStatus('Select one or more rounds');
        return;
    }
    await ensureChunks();
    await ensureGrenadesForSelection();
    buildTracks();
    buildUtil();
    buildFeedKills();
    timeline.max  = Math.max(0, maxLen);
    timelineA.max = Math.max(0, maxLen);
    relTick = Math.min(relTick, maxLen);
    winA = Math.min(winA, maxLen);
    if (winB <= 0 || winB > maxLen) winB = maxLen;
    syncSliders();
    buildLegend();
    render();

    const nM = new Set(selectedRounds.map(s => s.m)).size;
    let msg = `${selectedRounds.length} round${selectedRounds.length > 1 ? 's' : ''}`
            + (nM > 1 ? ` from ${nM} matches` : '')
            + ` · ${selectedPlayers.size} player${selectedPlayers.size > 1 ? 's' : ''}`;
    if (viewMode === 'play' && selectedRounds.length > PLAYBACK_SOFT_CAP)
        msg += ' · ⚠ many rounds - try Density view';
    setStatus(msg);
}

/**
 * Rebuild the per-selection player tracks that render()/density draw from.
 * Reads selectedRounds and each match's decoded ticks; produces round-relative
 * samples, which are the only thing that may mix across matches.
 */
function buildTracks() {
    tracks = {};
    maxLen = 0;
    for (const sel of selectedRounds) {
        const M = MATCHES[sel.m];
        const r = M.roundByNum.get(sel.rn);
        if (!r) continue;
        const t0 = roundStartTick(r), tEnd = roundEndTick(M, r);
        const players = new Map();
        let i = lowerIdx(M.sortedTicks, t0 - 1) + 1;   // first tick >= t0
        if (i < 0) i = 0;
        for (; i < M.sortedTicks.length; i++) {
            const tk = M.sortedTicks[i];
            if (tk > tEnd) break;
            const arr = M.tickMap.get(tk);
            if (!arr) continue;
            const rel = tk - t0;
            for (const p of arr) {
                if (!selectedPlayers.has(p.name)) continue;
                let s = players.get(p.name);
                if (!s) { s = []; players.set(p.name, s); }
                s.push({ rel, X: p.X, Y: p.Y, Z: p.Z, hp: p.health, side: p.side, yaw: p.yaw,
                         flash: p.flash_duration, active: p.active_weapon, inv: p.inventory, c4: p.has_c4,
                         speed: p.speed });
            }
        }
        const len = tEnd - t0;
        tracks[keyOf(sel)] = { t0, tEnd, len, players };
        if (len > maxLen) maxLen = len;
    }
    trackCount = 0;
    for (const k in tracks) trackCount += tracks[k].players.size;
}

function buildUtil() {
    roundUtil = {};
    for (const sel of selectedRounds) {
        const M = MATCHES[sel.m];
        const r = M.roundByNum.get(sel.rn);
        if (!r) continue;
        const t0 = roundStartTick(r), tEnd = roundEndTick(M, r), lo = r.start;
        const inWin = tk => tk >= lo && tk <= tEnd;

        // endRel is clamped to the real burn/screen duration - the demo's own
        // expire events fire well after the actual visual effect ends.
        const smokes = (M.master.smokes || []).filter(s => inWin(s.start_tick))
            .map(s => ({ X: s.X, Y: s.Y, startRel: s.start_tick - t0,
                         endRel: UtilFXLogic.clampEnd(s.start_tick, s.end_tick, UtilFXLogic.SMOKE_LIFETIME_TICKS) - t0,
                         thrower: s.thrower_name }));
        // Molotov vs CT incendiary (parser `weapon` field on new parses,
        // trajectory correlation otherwise) → fire size + rim colour; the
        // additive `cells` ignition timeline is remapped to round-relative.
        const infSrc = (M.master.infernos || []).filter(s => inWin(s.start_tick));
        const infWeapons = UtilFXLogic.classifyInfernos(infSrc, M.grenades);
        const infernos = infSrc.map((s, i) => ({
            X: s.X, Y: s.Y, startRel: s.start_tick - t0,
            endRel: UtilFXLogic.clampEnd(s.start_tick, s.end_tick, UtilFXLogic.FIRE_LIFETIME_TICKS) - t0,
            thrower: s.thrower_name,
            weapon: infWeapons[i],
            cells: (s.cells && s.cells.length)
                ? s.cells.map(c => ({ rel: c.tick - t0, X: c.X, Y: c.Y })) : null,
        }));
        const flashes = (M.master.flashes || []).filter(f => inWin(f.tick))
            .map(f => ({ X: f.X, Y: f.Y, rel: f.tick - t0, thrower: f.thrower_name }));
        const he = (M.master.he || []).filter(h => inWin(h.tick))
            .map(h => ({ X: h.X, Y: h.Y, rel: h.tick - t0, thrower: h.thrower_name }));

        const grenades = new Map();
        for (const g of M.grenades) {
            if (g.entity_id == null || !inWin(g.tick)) continue;
            let t = grenades.get(g.entity_id);
            if (!t) { t = []; grenades.set(g.entity_id, t); }
            t.push({ rel: g.tick - t0, X: g.X, Y: g.Y, type: g.grenade_type, thrower: g.thrower });
        }
        for (const t of grenades.values()) t.sort((a, b) => a.rel - b.rel);

        // Shots (for the firing spray flick), round-relative & sorted ascending.
        const shots = M.shots.filter(sh => inWin(sh.tick))
            .map(sh => ({ rel: sh.tick - t0, name: sh.player_name }))
            .sort((a, b) => a.rel - b.rel);

        // Dropped weapons/utility: kept as raw drop/pickup events (round-relative
        // `tick`) rather than pre-resolved, so GroundItemsLogic.resolveGroundItems
        // can answer "what's on the ground" at any relTick during playback.
        const items = (M.master.items || []).filter(it => inWin(it.tick))
            .map(it => ({ tick: it.tick - t0, event: it.event, item_id: it.item_id, X: it.X, Y: it.Y, weapon: it.weapon }))
            .sort((a, b) => a.tick - b.tick);

        // Bomb events kept RAW and round-relative, not pre-resolved - same
        // reason as `items` above: BombActionLogic answers "what is happening
        // at this tick" for any relTick during playback, and pre-computing a
        // single state would pin the overlay to one moment.
        const bomb = (M.master.bomb || []).filter(b => inWin(b.tick))
            .map(b => ({ tick: b.tick - t0, event: b.event, name: b.name,
                         X: b.X, Y: b.Y, has_kit: b.has_kit, bombsite: b.bombsite }))
            .sort((a, b) => a.tick - b.tick);

        roundUtil[keyOf(sel)] = { smokes, infernos, flashes, he, grenades, shots, items, bomb };
    }
}

// Kills across all selected rounds, round-relative and sorted ascending, feeding
// the in-canvas kill feed (§3). Same round window as buildUtil().
function buildFeedKills() {
    const all = [];
    for (const sel of selectedRounds) {
        const M = MATCHES[sel.m];
        const r = M.roundByNum.get(sel.rn);
        if (!r || !M.master) continue;
        const t0 = roundStartTick(r), tEnd = roundEndTick(M, r), lo = r.start;
        for (const k of (M.master.kills || [])) {
            if (k.tick < lo || k.tick > tEnd) continue;
            all.push({ tick: k.tick - t0,
                       attacker_name: k.attacker_name, victim_name: k.victim_name,
                       attacker_side: k.attacker_side, victim_side: k.victim_side,
                       weapon: k.weapon, headshot: k.headshot,
                       assister_name: k.assister_name, assist_flash: k.assist_flash,
                       wallbang: k.wallbang, through_smoke: k.through_smoke,
                       attacker_blind: k.attacker_blind, noscope: k.noscope,
                       attacker_air: k.attacker_air, suicide: k.suicide });
        }
    }
    all.sort((a, b) => a.tick - b.tick);
    feedKills = all;
}

// ═══════════════════════════════════════════════════════════════════════════
// Render
// ═══════════════════════════════════════════════════════════════════════════
// Every player visible in the current frame across every selected round -
// the electorate for the automatic floor choice. The overlay can be showing
// several rounds at once, so this is not one roster but all of them.
/**
 * Every player record currently drawn, across all selections.
 * @returns {Array<Object>} tick records - used for the multi-level-map
 *   majority vote, which only counts the living.
 */
function playersOnScreen() {
    const out = [];
    for (const sel of selectedRounds) {
        const tr = tracks[keyOf(sel)];
        if (!tr) continue;
        for (const [, samples] of tr.players) {
            const idx = lowerIdx(samples, relTick, x => x.rel);
            if (idx < 0) continue;
            const s = samples[idx];
            out.push({ Z: s.Z, health: s.hp });
        }
    }
    return out;
}

// Resolve layerMode to the floor on show, loading the lower radar the first
// time it is needed. Mirrors viewer.js's resolveLayer().
function resolveLayer() {
    if (!MapLayersLogic.isLayered(mapName)) { shownLayer = null; return; }
    const next = MapLayersLogic.resolve(mapName, layerMode, playersOnScreen(), shownLayer);
    if (next !== shownLayer) { shownLayer = next; updateLayerButtons(); }
    if (shownLayer === MapLayersLogic.LOWER && !mapImgLowerRequested) {
        mapImgLowerRequested = true;
        mapImgLower.onload  = () => { mapImgLowerReady = true;  render(); };
        mapImgLower.onerror = () => { mapImgLowerReady = false; render(); };
        mapImgLower.src = MapsLogic.radarUrl(MapLayersLogic.radarName(mapName, MapLayersLogic.LOWER));
    }
}

function layerAlpha(z) { return MapLayersLogic.alphaFor(mapName, z, shownLayer); }

// Show the Layer row only on a map that has floors; in 'auto' the button says
// which floor it settled on, so the map never shows a level the UI can't name.
function updateLayerButtons() {
    const row = document.getElementById('layer-row');
    if (!row) return;
    const layered = MapLayersLogic.isLayered(mapName);
    row.style.display = layered ? '' : 'none';
    if (!layered) return;
    row.querySelectorAll('[data-layer]').forEach(b =>
        b.classList.toggle('active', b.dataset.layer === layerMode));
    const auto = row.querySelector('[data-layer="auto"]');
    if (auto) auto.textContent = layerMode === 'auto' && shownLayer
        ? 'Auto \u00b7 ' + (shownLayer === MapLayersLogic.LOWER ? 'lower' : 'upper')
        : 'Auto';
}

/**
 * Draw one frame. Same order contract as viewer.js: viewport transform, then
 * background and all world entities in base 1024² coords, then
 * Viewport.clear(ctx) before any screen-space HUD.
 */
function render() {
    // Draw the map + all world entities under the shared pan/zoom transform (§2/§3)
    // so every worldToCanvas() call inherits zoom for free; then clear the transform
    // and draw the screen-space HUD (kill feed / messages).
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    resolveLayer();

    Viewport.apply(ctx, view);
    const radar = (shownLayer === MapLayersLogic.LOWER && mapImgLowerReady) ? mapImgLower
                : mapImageReady ? mapImg : null;
    if (radar) ctx.drawImage(radar, 0, 0, canvas.width, canvas.height);
    else { ctx.fillStyle = '#0d1117'; ctx.fillRect(0, 0, canvas.width, canvas.height); }

    // Under the rounds, over the radar: the heatmap is context for whatever
    // is being replayed, not a layer that hides it.
    const heat = drawQueryHeat();

    if (!selectedRounds.length) {
        Viewport.clear(ctx);
        // A heatmap IS a result - telling someone to pick a round while their
        // query is painted across the map would be nonsense.
        if (!heat) drawNoRoundsMessage();
        return;
    }

    if (viewMode === 'density') {
        const lo = Math.min(winA, winB), hi = Math.max(winA, winB);
        timeLabel.textContent = `${formatClock(lo / TICKRATE)}–${formatClock(hi / TICKRATE)}`;
        renderDensity(lo, hi);
        Viewport.clear(ctx);
        return;
    }

    timeLabel.textContent = formatClock(relTick / TICKRATE);
    timeline.value = relTick;

    selectedRounds.forEach((sel, k) => {
        if (showUtil.smoke || showUtil.fire || showUtil.flash || showUtil.he
            || showUtil.nade || showUtil.drops || showUtil.bomb)
            drawRoundUtil(sel, selColor(sel, k));
    });
    selectedRounds.forEach((sel, k) => drawRoundPlayers(sel, k));

    // Screen-space HUD: in-canvas kill feed (shared killfeed.js), top-right, fading.
    // Rounds share a round-relative timeline, so the feed shows the recent kills
    // across all overlaid rounds at the current playhead.
    Viewport.clear(ctx);
    if (feedKills.length)
        withHudScale(canvas.width, 0, () =>          // pinned to the top-right corner
            KillFeed.draw(ctx, { kills: feedKills, tick: relTick, canvasW: canvas.width,
                weaponLabel: w => (w || '').replace('weapon_', '').replace(/_/g, ' ') }));
}

/**
 * Paint the analyser's current result set as a density layer. Points from
 * other maps are dropped here rather than by the query side: which radar is
 * loaded is a property of this canvas, not of the question that was asked.
 */
function drawQueryHeat() {
    if (!queryHeat || !queryHeat.points || !queryHeat.points.length) {
        if (heatLegendEl) heatLegendEl.innerHTML = '';
        return null;
    }
    // No radar loaded yet means no projection: worldToCanvas reads mapConfig,
    // and plotting map positions with nothing to plot them on is not a
    // degraded picture, it is a crash.
    if (!mapConfig) {
        if (heatLegendEl)
            heatLegendEl.innerHTML = '<span class="heat-legend-r">open a match to plot these on the map</span>';
        return null;
    }
    const pts = mapName
        ? queryHeat.points.filter(p => !p.map || p.map === mapName)
        : queryHeat.points;
    const binned = MomentHeat.draw(ctx, {
        points: pts, worldToCanvas,
        mode: queryHeat.mode || 'heat',
        w: canvas.width, h: canvas.height,
    });
    if (heatLegendEl) {
        const off = queryHeat.points.length - pts.length;
        heatLegendEl.innerHTML = MomentHeat.legendHtml(binned, queryHeat.label)
            + (off ? `<span class="heat-legend-r">${off} on other maps</span>` : '');
    }
    return binned;
}

/** Called by analyser.js whenever the query result changes while the heatmap
 *  is on (and with null when it is switched off). */
function setQueryHeat(heat) {
    queryHeat = heat;
    render();
}

function drawNoRoundsMessage() {
    ctx.save();
    ctx.fillStyle = 'rgba(10,12,15,0.55)';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.textAlign = 'center';
    ctx.fillStyle = '#c8d0dc';
    ctx.font = 'bold 28px ' + FmtLogic.FONT_UI;
    ctx.fillText('No rounds selected', canvas.width / 2, canvas.height / 2 - 16);
    ctx.fillStyle = '#556070';
    ctx.font = '19px ' + FmtLogic.FONT_UI;
    ctx.fillText('Pick rounds in panel 1 on the right - overlay as many as you like, across matches', canvas.width / 2, canvas.height / 2 + 18);
    ctx.restore();
}

/**
 * The bomb for ONE selected round: where it is, and any plant/defuse running.
 *
 * The replayer draws its progress ring around the acting PLAYER; here it is
 * drawn around the bomb instead, and that is deliberate rather than a
 * shortcut. The parser records an action's position as the bomb's own
 * (`BombDefuseStart` fires with the defuser in arm's reach of it), so the two
 * are the same spot to within a few units - and this canvas can be showing
 * ten rounds at once, where hunting the matching player record per round buys
 * nothing but a per-frame lookup. One marker per round, in that round's
 * overlay colour, is also what keeps several planted bombs distinguishable.
 *
 * Round-relative throughout: `u.bomb` ticks were rebased in buildUtil(), so
 * `relTick` is the right clock and roundStart is 0.
 */
function drawRoundBomb(u, col) {
    let state = null;
    for (const b of u.bomb) {
        if (b.tick > relTick) break;
        if (BombActionLogic.isStateEvent(b.event)) state = b;
    }
    const action = BombActionLogic.actionAt(u.bomb, relTick, 0);

    if (state && (state.event === 'plant' || state.event === 'drop')
        && state.X != null && state.Y != null) {
        const p = worldToCanvas(state.X, state.Y);
        if (isOnCanvas(p)) {
            const planted = state.event === 'plant';
            ctx.save();
            ctx.globalAlpha = planted ? 0.9 : 0.55;
            ctx.beginPath();
            ctx.arc(p.x, p.y, planted ? 5 : 3.5, 0, Math.PI * 2);
            ctx.fillStyle = planted ? '#ff3333' : col;
            ctx.fill();
            ctx.strokeStyle = 'rgba(255,255,255,0.85)';
            ctx.lineWidth = 1;
            ctx.stroke();
            ctx.restore();
        }
    }

    if (action && action.x != null && action.y != null) {
        const p = worldToCanvas(action.x, action.y);
        if (!isOnCanvas(p)) return;
        // Shared with the replayer - see static/bombaction.js. Only the
        // anchor differs between the two pages, and that is on purpose.
        BombAction.drawRing(ctx, {
            x: p.x, y: p.y, kind: action.kind, progress: action.progress,
        });
    }
}

function drawRoundUtil(sel, col) {
    const u = roundUtil[keyOf(sel)];
    if (!u) return;

    if (showUtil.bomb && u.bomb && u.bomb.length) drawRoundBomb(u, col);

    if (showUtil.smoke) for (const s of u.smokes) {
        if (utilityOwners.size && !utilityOwners.has(s.thrower)) continue;
        if (relTick < s.startRel || relTick > s.endRel) continue;
        const p = worldToCanvas(s.X, s.Y); if (!isOnCanvas(p)) continue;
        const rPx = UtilFXLogic.SMOKE_RADIUS / mapConfig.scale;
        const bloom = UtilFXLogic.smokeBloom(relTick - s.startRel);
        const fade = (s.endRel - relTick) < 128 ? (s.endRel - relTick) / 128 : 1;
        // HE detonations from the same selected round carve holes (rel ticks)
        const holes = UtilFXLogic.smokeHoles(s, u.he, relTick, {
            tickOf: h => h.rel, startOf: sm => sm.startRel, endOf: sm => sm.endRel,
        }).map(h => {
            const hp = worldToCanvas(h.x, h.y);
            return { x: hp.x, y: hp.y, r: h.r / mapConfig.scale, strength: h.strength };
        });
        UtilFX.drawSmokeCloud(ctx, {
            x: p.x, y: p.y, rPx, tick: relTick,
            seed: (s.startRel * 31 + Math.round(s.X)) | 0,
            bloom, fade: fade * 0.85, holes,
        });
        // per-selection identity ring (overlay mode shows several rounds at once)
        ctx.strokeStyle = col; ctx.globalAlpha = bloom * fade * 0.55;
        ctx.beginPath(); ctx.arc(p.x, p.y, rPx * bloom + 2, 0, Math.PI * 2);
        ctx.lineWidth = 1; ctx.stroke(); ctx.globalAlpha = 1;
    }
    if (showUtil.fire) for (const s of u.infernos) {
        if (utilityOwners.size && !utilityOwners.has(s.thrower)) continue;
        if (relTick < s.startRel || relTick > s.endRel) continue;
        const p = worldToCanvas(s.X, s.Y); if (!isOnCanvas(p)) continue;
        const maxR = UtilFXLogic.infernoMaxRadius(s.weapon);
        const rPx = UtilFXLogic.fireSpreadRadius(relTick - s.startRel, maxR) / mapConfig.scale;
        const fade = (s.endRel - relTick) < 96 ? (s.endRel - relTick) / 96 : 1;
        let cells = null;
        if (s.cells) {
            cells = [];
            for (const c of s.cells) {
                if (c.rel > relTick) break;
                const cp = worldToCanvas(c.X, c.Y);
                cells.push({ x: cp.x, y: cp.y });
            }
        }
        UtilFX.drawFire(ctx, {
            x: p.x, y: p.y, rPx, tick: relTick,
            seed: (s.startRel * 17 + Math.round(s.X)) | 0,
            weapon: s.weapon, alpha: fade * 0.85, cells,
        });
    }
    if (showUtil.nade) for (const trail of u.grenades.values()) {
        if (utilityOwners.size && trail[0] && !utilityOwners.has(trail[0].thrower)) continue;
        const pts = trail.filter(pt => pt.rel <= relTick && pt.rel >= relTick - GRENADE_LOOKBACK);
        if (pts.length < 2) continue;
        ctx.save(); ctx.lineJoin = 'round'; ctx.lineCap = 'round';
        for (let i = 1; i < pts.length; i++) {
            const a0 = worldToCanvas(pts[i - 1].X, pts[i - 1].Y);
            const a1 = worldToCanvas(pts[i].X, pts[i].Y);
            const al = Math.max(0, 1 - (relTick - pts[i].rel) / GRENADE_LOOKBACK);
            ctx.beginPath(); ctx.moveTo(a0.x, a0.y); ctx.lineTo(a1.x, a1.y);
            ctx.strokeStyle = col; ctx.globalAlpha = al * 0.9; ctx.lineWidth = 2; ctx.stroke();
        }
        const head = worldToCanvas(pts[pts.length - 1].X, pts[pts.length - 1].Y);
        ctx.globalAlpha = 1; ctx.beginPath(); ctx.arc(head.x, head.y, 3.5, 0, Math.PI * 2);
        ctx.fillStyle = col; ctx.fill(); ctx.restore();
    }
    if (showUtil.flash) for (const f of u.flashes) {
        if (utilityOwners.size && !utilityOwners.has(f.thrower)) continue;
        const age = relTick - f.rel; if (age < 0 || age > FLASH_FADE_TICKS) continue;
        const p = worldToCanvas(f.X, f.Y); if (!isOnCanvas(p)) continue;
        const r = 40 * (0.4 + 0.6 * easeOut(Math.min(age / 10, 1)));
        const a = easeOut(1 - age / FLASH_FADE_TICKS);
        const grad = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, r);
        grad.addColorStop(0, `rgba(255,255,255,${a})`);
        grad.addColorStop(0.5, `rgba(235,245,255,${a * 0.5})`);
        grad.addColorStop(1, 'rgba(200,230,255,0)');
        ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, Math.PI * 2); ctx.fillStyle = grad; ctx.fill();
    }
    if (showUtil.he) for (const h of u.he) {
        if (utilityOwners.size && !utilityOwners.has(h.thrower)) continue;
        const age = relTick - h.rel; if (age < 0 || age > HE_FADE_TICKS) continue;
        const p = worldToCanvas(h.X, h.Y); if (!isOnCanvas(p)) continue;
        const t = age / HE_FADE_TICKS, a = easeOut(1 - t), r = 10 + easeOut(t) * 36;
        ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
        ctx.strokeStyle = `rgba(255,180,60,${a * 0.9})`; ctx.lineWidth = 3; ctx.stroke();
    }
    if (showUtil.drops && u.items && u.items.length) {
        const items = GroundItemsLogic.resolveGroundItems(u.items, relTick, -Infinity);
        GroundItems.draw(ctx, { items, worldToCanvas, isOnCanvas });
    }
}

// Blend a #rrggbb colour toward white by t∈[0,1] (flash tint, like viewer.js).
function tintWhite(hex, t) {
    const m = hex.replace('#', '');
    const r = parseInt(m.slice(0, 2), 16),
          g = parseInt(m.slice(2, 4), 16),
          b = parseInt(m.slice(4, 6), 16);
    return `rgb(${Math.round(r + (255 - r) * t)},${Math.round(g + (255 - g) * t)},${Math.round(b + (255 - b) * t)})`;
}

function drawSpray(pos, color, yaw, dim = 1) { Match2D.drawSpray(ctx, pos, color, yaw, dim); }

// Average a player's speed over a short trailing window of samples ending at
// idx, to smooth out per-tick velocity noise (same rationale as viewer.js).
function smoothedSpeed(samples, idx) {
    const start = Math.max(0, idx - FOOTSTEP_SMOOTH_SAMPLES);
    let sum = 0, n = 0;
    for (let i = start; i <= idx; i++) {
        const s = samples[i];
        if (!s || s.speed == null || s.hp <= 0) continue;
        sum += s.speed; n++;
    }
    return n ? sum / n : null;
}

function drawRoundPlayers(sel, k) {
    const tr = tracks[keyOf(sel)];
    if (!tr) return;
    const col = selColor(sel, k);
    const multiMatch = new Set(selectedRounds.map(s => s.m)).size > 1;
    const tag = multiMatch ? ` ·M${sel.m + 1}R${sel.rn}`
              : selectedRounds.length > 1 ? ` ·R${sel.rn}` : '';

    // Players firing right now in this round (recent shot within the spray
    // window), mirroring viewer.js's firingPlayers scan.
    const firing = new Set();
    const shots = (roundUtil[keyOf(sel)] && roundUtil[keyOf(sel)].shots) || [];
    for (let i = shots.length - 1; i >= 0; i--) {
        if (shots[i].rel > relTick) continue;
        if (relTick - shots[i].rel > SPRAY_DURATION_TICKS) break;
        firing.add(shots[i].name);
    }

    const anyFocus = focusedPlayers.size > 0;

    for (const [name, samples] of tr.players) {
        const idx = lowerIdx(samples, relTick, s => s.rel);
        if (idx < 0) continue;
        const s = samples[idx];
        const pos = worldToCanvas(s.X, s.Y);
        if (!isOnCanvas(pos)) continue;

        const focused = focusedPlayers.has(name);
        // Two independent reasons to fade a player, multiplied together: not
        // being the spotlighted one, and standing on the floor of a
        // multi-level map that isn't the one on show. Every draw below already
        // multiplies by `dim`, so the floor rides along for free.
        const dim     = (anyFocus && !focused ? 0.25 : 1) * layerAlpha(s.Z);
        const dead = s.hp <= 0;
        const teamCol = s.side === 'ct' ? '#4a9eff' : '#ffaa22';
        const drawCol = colorMode === 'team' ? teamCol : col;

        // Trail (short window of path travelled up to now) - same on/off model
        // and fixed ~1.5s window as the 2D viewer's Trails toggle.
        if (showTrails && idx > 0) {
            const from = lowerIdx(samples, s.rel - PLAYER_TRAIL_TICKS, x => x.rel) + 1;
            ctx.save(); ctx.lineJoin = 'round'; ctx.lineCap = 'round'; ctx.lineWidth = 2;
            for (let i = Math.max(1, from); i <= idx; i++) {
                const a0 = worldToCanvas(samples[i - 1].X, samples[i - 1].Y);
                const a1 = worldToCanvas(samples[i].X, samples[i].Y);
                const al = Math.max(0, 1 - (s.rel - samples[i].rel) / PLAYER_TRAIL_TICKS);
                ctx.beginPath(); ctx.moveTo(a0.x, a0.y); ctx.lineTo(a1.x, a1.y);
                ctx.strokeStyle = drawCol; ctx.globalAlpha = al * 0.7 * dim; ctx.stroke();
            }
            ctx.restore(); ctx.globalAlpha = 1;
        }

        if (dead) {
            // Death marker (×) where they fell.
            ctx.save(); ctx.strokeStyle = drawCol; ctx.globalAlpha = 0.8 * dim; ctx.lineWidth = 2;
            ctx.beginPath();
            ctx.moveTo(pos.x - 4, pos.y - 4); ctx.lineTo(pos.x + 4, pos.y + 4);
            ctx.moveTo(pos.x + 4, pos.y - 4); ctx.lineTo(pos.x - 4, pos.y + 4);
            ctx.stroke(); ctx.restore(); ctx.globalAlpha = 1;
            continue;
        }

        // ── Player marker, matching viewer.js's drawPlayers design ──────────
        // Flash intensity → body tints toward white and the halo swells/whitens
        let intensity = 0;
        if (s.flash > 0) intensity = Math.pow(Math.min(s.flash / 2.5, 1.0), 0.8);
        const activeCol = intensity > 0 ? tintWhite(drawCol, intensity) : drawCol;

        // Footstep audibility: static circle sized to the real hearing range,
        // fading in/out over FOOTSTEP_FADE_MARGIN (drawn beneath the body, as
        // in viewer.js)
        const footstepSpeed = showFootsteps ? smoothedSpeed(samples, idx) : null;
        if (footstepSpeed != null && footstepSpeed >= AUDIBLE_SPEED - FOOTSTEP_FADE_MARGIN) {
            const fadeIn = Math.max(0, Math.min(1, (footstepSpeed - (AUDIBLE_SPEED - FOOTSTEP_FADE_MARGIN)) / FOOTSTEP_FADE_MARGIN));
            const t = Math.max(0, Math.min(1, (footstepSpeed - AUDIBLE_SPEED) / (RUN_SPEED_REF - AUDIBLE_SPEED)));
            const radiusUnits = FOOTSTEP_RADIUS_MIN + t * (FOOTSTEP_RADIUS_MAX - FOOTSTEP_RADIUS_MIN);
            const rPx = radiusUnits / mapConfig.scale;
            ctx.beginPath();
            ctx.arc(pos.x, pos.y, rPx, 0, Math.PI * 2);
            ctx.fillStyle = `rgba(255,235,150,${(0.08 * fadeIn * dim).toFixed(3)})`;
            ctx.fill();
            ctx.strokeStyle = `rgba(255,235,150,${(0.5 * fadeIn * dim).toFixed(3)})`;
            ctx.lineWidth = 1.5;
            ctx.stroke();
        }

        // Firing spray flick (drawn beneath the body, as in viewer.js)
        if (firing.has(name)) drawSpray(pos, activeCol, s.yaw, dim);

        // Non-focused players draw dimmed while any focus is active
        ctx.save();
        ctx.globalAlpha = dim;

        // Soft halo - team-tinted normally, white & enlarged while flashed
        const bodyR = focused ? 9 : 7;
        ctx.beginPath(); ctx.arc(pos.x, pos.y, (focused ? 16 : 13) + intensity * 5, 0, Math.PI * 2);
        ctx.fillStyle = intensity > 0
            ? `rgba(255,255,255,${intensity * 0.35})`
            : (s.side === 'ct' ? 'rgba(74,158,255,0.15)' : 'rgba(255,170,34,0.15)');
        ctx.fill();
        // Solid body dot
        ctx.beginPath(); ctx.arc(pos.x, pos.y, bodyR, 0, Math.PI * 2);
        ctx.fillStyle = activeCol; ctx.fill();
        ctx.strokeStyle = intensity > 0.4 ? 'white' : 'rgba(255,255,255,0.85)';
        ctx.lineWidth = (focused ? 2.5 : 1.5) + intensity * 1.5; ctx.stroke();
        // Direction triangle (same construction as viewer.js)
        if (s.yaw != null) {
            const yawRad   = (-s.yaw) * (Math.PI / 180);
            const circleR  = bodyR;
            const tipDist  = circleR + 5;
            const baseHalf = 3.5;
            const tipX  = pos.x + Math.cos(yawRad) * tipDist;
            const tipY  = pos.y + Math.sin(yawRad) * tipDist;
            const perpX = -Math.sin(yawRad);
            const perpY =  Math.cos(yawRad);
            const baseX = pos.x + Math.cos(yawRad) * circleR;
            const baseY = pos.y + Math.sin(yawRad) * circleR;
            ctx.save();
            ctx.beginPath();
            ctx.moveTo(tipX, tipY);
            ctx.lineTo(baseX + perpX * baseHalf, baseY + perpY * baseHalf);
            ctx.lineTo(baseX - perpX * baseHalf, baseY - perpY * baseHalf);
            ctx.closePath();
            ctx.fillStyle   = activeCol;
            ctx.strokeStyle = 'rgba(255,255,255,0.85)';
            ctx.lineWidth   = 1;
            ctx.fill();
            ctx.stroke();
            ctx.restore();
        }
        // Name label - Auto: with many tracks on screen, only focused players
        // keep their label (dots/trails remain for everyone) to cut clutter.
        // All: always show. Off: never show.
        const showName = nameMode === 'all' || (nameMode === 'auto' && (trackCount <= LABEL_TRACK_CAP || focused));
        if (showName) {
            ctx.fillStyle   = 'white';
            ctx.font        = (focused ? 'bold 13px ' : 'bold 11px ') + FmtLogic.FONT_NARROW;
            ctx.shadowColor = 'rgba(0,0,0,0.95)'; ctx.shadowBlur = 4;
            ctx.fillText(name + tag, pos.x + bodyR + 3, pos.y + 4);
            ctx.shadowBlur = 0;
        }
        ctx.restore();   // pop the dim globalAlpha
    }
}

// ═══════════════════════════════════════════════════════════════════════════
// Density view - accumulate everything inside the [lo..hi] round-time window
// ═══════════════════════════════════════════════════════════════════════════
function renderDensity(lo, hi) {
    // Utility footprints (drawn first, under the paths)
    selectedRounds.forEach((sel, k) => {
        const u = roundUtil[keyOf(sel)];
        if (!u) return;
        const col = selColor(sel, k);

        if (showUtil.smoke) for (const s of u.smokes) {
            if (utilityOwners.size && !utilityOwners.has(s.thrower)) continue;
            if (s.startRel > hi || s.endRel < lo) continue;
            const p = worldToCanvas(s.X, s.Y); if (!isOnCanvas(p)) continue;
            const rPx = UtilFXLogic.SMOKE_RADIUS / mapConfig.scale;   // true footprint
            ctx.beginPath(); ctx.arc(p.x, p.y, rPx, 0, Math.PI * 2);
            ctx.fillStyle = 'rgba(170,210,170,0.08)'; ctx.fill();
            ctx.strokeStyle = col; ctx.globalAlpha = 0.45; ctx.lineWidth = 1.5; ctx.stroke();
            ctx.globalAlpha = 1;
        }
        if (showUtil.fire) for (const s of u.infernos) {
            if (utilityOwners.size && !utilityOwners.has(s.thrower)) continue;
            if (s.startRel > hi || s.endRel < lo) continue;
            const p = worldToCanvas(s.X, s.Y); if (!isOnCanvas(p)) continue;
            // true footprint per weapon; rim colour marks CT incendiary vs T molly
            const rPx = UtilFXLogic.infernoMaxRadius(s.weapon) / mapConfig.scale;
            ctx.beginPath(); ctx.arc(p.x, p.y, rPx, 0, Math.PI * 2);
            ctx.fillStyle = 'rgba(255,100,20,0.10)'; ctx.fill();
            ctx.strokeStyle = s.weapon === 'Incendiary'
                ? 'rgba(150,200,255,0.55)' : 'rgba(255,140,40,0.5)';
            ctx.lineWidth = 1.5; ctx.stroke();
        }
        if (showUtil.nade) for (const trail of u.grenades.values()) {
            if (utilityOwners.size && trail[0] && !utilityOwners.has(trail[0].thrower)) continue;
            const pts = trail.filter(pt => pt.rel >= lo && pt.rel <= hi);
            if (pts.length < 2) continue;
            ctx.save(); ctx.lineJoin = 'round'; ctx.lineCap = 'round';
            ctx.strokeStyle = col; ctx.globalAlpha = 0.22; ctx.lineWidth = 1.5;
            ctx.beginPath();
            const p0 = worldToCanvas(pts[0].X, pts[0].Y);
            ctx.moveTo(p0.x, p0.y);
            for (let i = 1; i < pts.length; i++) {
                const p = worldToCanvas(pts[i].X, pts[i].Y);
                ctx.lineTo(p.x, p.y);
            }
            ctx.stroke(); ctx.restore(); ctx.globalAlpha = 1;
        }
        if (showUtil.flash) for (const f of u.flashes) {
            if (utilityOwners.size && !utilityOwners.has(f.thrower)) continue;
            if (f.rel < lo || f.rel > hi) continue;
            const p = worldToCanvas(f.X, f.Y); if (!isOnCanvas(p)) continue;
            ctx.beginPath(); ctx.arc(p.x, p.y, 5, 0, Math.PI * 2);
            ctx.strokeStyle = 'rgba(255,255,255,0.55)'; ctx.lineWidth = 1.5; ctx.stroke();
        }
        if (showUtil.he) for (const h of u.he) {
            if (utilityOwners.size && !utilityOwners.has(h.thrower)) continue;
            if (h.rel < lo || h.rel > hi) continue;
            const p = worldToCanvas(h.X, h.Y); if (!isOnCanvas(p)) continue;
            ctx.beginPath(); ctx.arc(p.x, p.y, 6, 0, Math.PI * 2);
            ctx.strokeStyle = 'rgba(255,180,60,0.6)'; ctx.lineWidth = 1.5; ctx.stroke();
        }
        // Density view has no single playhead, so just mark where drops happened
        // inside the window rather than resolving on-ground/picked-up state.
        if (showUtil.drops && u.items && u.items.length) {
            const drops = u.items.filter(it => it.event === 'drop' && it.tick >= lo && it.tick <= hi);
            if (drops.length) GroundItems.draw(ctx, { items: drops, worldToCanvas, isOnCanvas, alpha: 0.55 });
        }
    });

    // Path density: low-alpha trails accumulate into a heatmap where routes
    // repeat. Deaths inside the window get an × marker. Focused players draw
    // stronger; everyone else fades while any focus is active.
    const anyFocus = focusedPlayers.size > 0;
    selectedRounds.forEach((sel, k) => {
        const tr = tracks[keyOf(sel)];
        if (!tr) return;
        const col = selColor(sel, k);

        for (const [name, samples] of tr.players) {
            const focused   = focusedPlayers.has(name);
            const pathAlpha = anyFocus ? (focused ? 0.32 : 0.04) : 0.14;
            const markAlpha = anyFocus ? (focused ? 1.0  : 0.15) : 0.9;
            const startI = Math.max(0, lowerIdx(samples, lo - 1, s => s.rel) + 1);
            ctx.save(); ctx.lineJoin = 'round'; ctx.lineCap = 'round'; ctx.lineWidth = 2;
            let death = null;
            let started = false;
            ctx.beginPath();
            for (let i = startI; i < samples.length; i++) {
                const s = samples[i];
                if (s.rel > hi) break;
                if (s.hp <= 0) {
                    if (!death && (i === 0 || samples[i - 1].hp > 0)) death = s;
                    continue;   // don't draw the corpse position as a path
                }
                const p = worldToCanvas(s.X, s.Y);
                if (!started) { ctx.moveTo(p.x, p.y); started = true; }
                else ctx.lineTo(p.x, p.y);
            }
            const drawCol = colorMode === 'team'
                ? ((samples[startI] || samples[0] || {}).side === 'ct' ? '#4a9eff' : '#ffaa22')
                : col;
            ctx.strokeStyle = drawCol; ctx.globalAlpha = pathAlpha;
            ctx.stroke();
            ctx.restore(); ctx.globalAlpha = 1;

            if (death) {
                const p = worldToCanvas(death.X, death.Y);
                if (isOnCanvas(p)) {
                    ctx.save(); ctx.strokeStyle = drawCol; ctx.globalAlpha = markAlpha; ctx.lineWidth = 2;
                    ctx.beginPath();
                    ctx.moveTo(p.x - 5, p.y - 5); ctx.lineTo(p.x + 5, p.y + 5);
                    ctx.moveTo(p.x + 5, p.y - 5); ctx.lineTo(p.x - 5, p.y + 5);
                    ctx.stroke(); ctx.restore(); ctx.globalAlpha = 1;
                }
            }
        }
    });
}

// ═══════════════════════════════════════════════════════════════════════════
// UI building
// ═══════════════════════════════════════════════════════════════════════════
function buildMatchSections() {
    matchSections.innerHTML = '';
    MATCHES.forEach((M, mi) => {
        const sec = document.createElement('div');
        sec.className = 'match-sec';
        sec.id = 'msec-' + mi;
        // The match you arrived on gets a mark. Until now the only cues were
        // that it auto-expanded and happened to sort first, which is not the
        // same as being told.
        const entry = M.file === SCOPE_CTX.entry
            ? '<span class="mentry" title="the match you came from">⌂</span>' : '';
        // The way down from a map-wide or corpus-wide page to one match. Text,
        // not a third glyph on a row that already carries ⌂ and ▸.
        const only = '<span class="mscope" data-mi="' + mi
            + '" title="Analyse only this match">only</span>';
        sec.innerHTML = `
            <div class="match-head" id="mhead-${mi}">
                <span class="mtag" style="--mc:${matchColor(mi)}">M${mi + 1}</span>
                ${entry}
                <span class="mlabel">${M.label}</span>
                <span class="mmode">${M.mode}</span>
                <span class="mscore" id="mscore-${mi}"></span>
                <span class="mcount" id="mcount-${mi}"></span>
                ${only}
                <span class="chev" id="mchev-${mi}">▸</span>
            </div>
            <div class="match-body" id="mbody-${mi}" style="display:none">
                <div class="rfilters" id="rfilters-${mi}"></div>
                <div class="round-grid" id="rgrid-${mi}"></div>
            </div>`;
        sec.querySelector('.match-head').onclick = (e) => {
            // "only" scopes the page; it must not also expand the section it
            // sits in, which is what the rest of the head does.
            if (e.target.closest('.mscope')) {
                if (typeof Analyser !== 'undefined') Analyser.setScopeFiles([M.file]);
                return;
            }
            toggleSection(mi);
        };
        matchSections.appendChild(sec);
    });
    // One row standing in for everything the current scope leaves out. Hiding
    // matches with no way back is how a page loses the user; this is the way
    // back, and it says how many.
    const more = document.createElement('div');
    more.className = 'match-more';
    more.id = 'match-more';
    more.style.display = 'none';
    more.onclick = () => { if (typeof Analyser !== 'undefined') Analyser.setScope('map'); };
    matchSections.appendChild(more);
    syncScopeRail();
}

/**
 * Show only the in-scope matches, with the rest behind the widen row. The
 * sections themselves are never rebuilt - see the note on MATCHES.
 */
function syncScopeRail() {
    const files = scopeState ? scopeState.files : null;
    let hidden = 0;
    MATCHES.forEach((M, mi) => {
        const sec = document.getElementById('msec-' + mi);
        if (!sec) return;
        const inScope = !files || files.indexOf(M.file) >= 0;
        sec.style.display = inScope ? '' : 'none';
        if (!inScope) hidden++;
        const only = sec.querySelector('.mscope');
        if (only) only.classList.toggle('on', !!files && files.length === 1 && files[0] === M.file);
    });
    const more = document.getElementById('match-more');
    if (more) {
        more.style.display = hidden ? '' : 'none';
        more.textContent = `+ ${hidden} more on ${MapsLogic.label(SCOPE_CTX.map) || 'this map'} - widen scope`;
    }
    if (railScopeEl) {
        const shown = MATCHES.length - hidden;
        railScopeEl.textContent = hidden
            ? `${shown} of ${MATCHES.length} matches in scope`
            : `all ${MATCHES.length} match${MATCHES.length === 1 ? '' : 'es'} on ${MapsLogic.label(SCOPE_CTX.map) || 'this map'}`;
    }
}

/**
 * Render the scope bar: the one control that says what this page is analysing
 * and switches it. It drives BOTH halves of the page (the query and the rail),
 * which is why it lives above them rather than inside the Find tab.
 */
function renderScopeBar() {
    if (!scopeSegsEl || !scopeState) return;
    const ctx = scopeState.ctx, counts = scopeState.counts;
    const kinds = ScopeLogic.kindsFor(ctx, scopeState.kind);
    scopeSegsEl.innerHTML = kinds.map(k => {
        const on = k === scopeState.kind ? ' on' : '';
        const title = k === 'all'
            ? 'Search every match in the corpus - the overlay can still only draw '
              + (ctx.map || 'one map')
            : '';
        return `<button type="button" class="sb-seg${on}" data-kind="${k}" title="${title}">`
             + ScopeLogic.label(k, ctx, counts) + '</button>';
    }).join('');
    if (scopeMapEl) {
        // Which radar this page draws. On an all-maps scope it repoints the
        // canvas without narrowing the question; otherwise it IS the map scope.
        const maps = scopeState.maps || [];
        scopeMapEl.innerHTML = maps.map(m =>
            `<option value="${m.map}">${MapsLogic.label(m.map)} (${m.n})</option>`).join('');
        scopeMapEl.value = ctx.map || '';
        scopeMapEl.style.display = maps.length > 1 ? '' : 'none';
        scopeMapEl.title = scopeState.kind === 'all'
            ? 'Which map the overlay and heatmap draw - the query still covers every map'
            : 'Which map to analyse';
    }
    const what = ScopeLogic.describe(
        { scope: scopeState.scope }, ctx, MATCHES.map(M => ({ file: M.file, label: M.label })), counts);
    if (scopeWhatEl) {
        // Under an all-maps scope the results outrun the canvas. Say so here
        // rather than letting "+ overlay" refuse rows with no warning.
        scopeWhatEl.textContent = what + (scopeState.kind === 'all'
            ? ` · overlay and heatmap draw ${ctx.map || 'this map'} only` : '');
    }
    document.title = 'Multi Round Analyser · ' + what;
}

/**
 * The hero's rounds/kills/damage pills are running sums over every match that
 * has been LOADED (expanded), which is not the same as what is in scope. With
 * one match loaded that distinction doesn't exist; from the second onwards it
 * does, so it gets stated instead of leaving the numbers to be read as this
 * match's.
 */
function syncLoadedPill() {
    if (!heroLoadedEl) return;
    const loaded = MATCHES.filter(M => M.loaded);
    // Shown from the second match on... and always on an entry-less page,
    // where the hero's rounds/kills/damage otherwise read as the scope's
    // totals when they are really one match's - the one loaded to give the
    // canvas a radar.
    const say = loaded.length > 1 || !SCOPE_CTX.entry;
    heroLoadedEl.style.display = say && loaded.length ? '' : 'none';
    // Name the match when there is only one: the match whose totals these are
    // is not necessarily the match in focus (focusing does not load anything),
    // and "1 loaded match" left the reader to assume it was.
    heroLoadedEl.textContent = loaded.length === 1
        ? `TOTALS: ${loaded[0].label}`
        : `TOTALS OVER ${loaded.length} MATCHES`;
    heroLoadedEl.title = loaded.map(M => M.label).join(' · ');
}

/**
 * Move the page to another map, carrying the query with it.
 *
 * `location.assign`, not `history.replaceState` - the deliberate contrast with
 * how the analyser syncs a filter edit (editing a filter is not a navigation;
 * changing the map is, and Back must return to the map you were on). Returning
 * true tells the analyser the host took it, so it doesn't also re-query in
 * place against a radar that is about to be replaced.
 */
function goToMap(map, q, keepAll) {
    setStatus(`Loading ${map}…`);
    const url = new URL('/multi', location.origin);
    if (keepAll) url.searchParams.set('scope', 'all');
    url.searchParams.set('map', map);
    if (q) url.searchParams.set('q', q);
    location.assign(url.toString());
    return true;
}

/**
 * Make the left rail describe what the page is analysing *now*.
 *
 * The rail follows the scope, not the address the page was opened at. Widen a
 * single match to its map and the page becomes the general analyser, so the
 * global "Match Analyser" entry lights instead; narrow the general analyser to
 * one match and the MATCH group appears with that match's Stats and Replay one
 * click away. Deriving this from the scope on every change is what keeps the
 * rail from describing where you arrived rather than where you are.
 *
 * The MATCH group is shown for whichever single match is in focus - which may
 * be one the page was never opened at, so it comes from
 * `ScopeLogic.focusFile`, not from the immutable `ctx.entry`.
 */
function syncRailFocus(state) {
    const focus = state && state.focus;
    if (railCtxEl) {
        railCtxEl.style.display = focus ? '' : 'none';
        if (focus) {
            const f = encodeURIComponent(focus);
            const set = (id, href, active) => {
                const el = document.getElementById(id);
                if (!el) return;
                el.href = href;
                el.classList.toggle('active', !!active);
            };
            set('rail-ctx-stats', '/match?match=' + f);
            set('rail-ctx-viewer', '/viewer?match=' + f);
            set('rail-ctx-multi', '/multi?match=' + f, true);
        }
    }
    // Exactly one of the two analyser entries is lit: the contextual one when a
    // match is in focus, the global one otherwise.
    if (railAnalyserEl) railAnalyserEl.classList.toggle('active', !focus);
}

/**
 * Keep the address describing the current scope, so a reload - or a link you
 * send someone - lands in the mode you are in rather than the one you started
 * in. `?q=` (written by the analyser) carries the query; these params carry the
 * identity beside it. replaceState, not assign: this is the same page.
 */
function syncScopeUrl(state) {
    if (!state || !state.entryParams) return;
    const url = new URL(location.href);
    ['match', 'files', 'map', 'scope'].forEach(k => url.searchParams.delete(k));
    Object.keys(state.entryParams).forEach(k => url.searchParams.set(k, state.entryParams[k]));
    history.replaceState(null, '', url);
}

/** Called by the analyser whenever the effective scope changes. */
function onScopeChanged(state) {
    scopeState = state;
    renderScopeBar();
    syncScopeRail();
    syncRailFocus(state);
    syncScopeUrl(state);
}

async function toggleSection(mi, forceOpen) {
    const body = document.getElementById('mbody-' + mi);
    const chev = document.getElementById('mchev-' + mi);
    const open = body.style.display !== 'none';
    if (open && !forceOpen) {
        body.style.display = 'none';
        chev.textContent = '▸';
        return;
    }
    try { await loadMatch(mi); } catch { return; }
    body.style.display = '';
    chev.textContent = '▾';
    syncLoadedPill();
}

// Win-condition icon/label for a round's `reason` field - see
// Match2DLogic.roundReasonIcon/Label (static/match2d.logic.js) for the mapping
// rationale, shared with viewer.js and templates/match.html.
function roundReasonIcon(reason)  { return Match2DLogic.roundReasonIcon(reason, window.Assets); }
function roundReasonLabel(reason) { return Match2DLogic.roundReasonLabel(reason); }

function buildRoundGridFor(mi) {
    const grid = document.getElementById('rgrid-' + mi);
    if (!grid) return;
    grid.innerHTML = '';
    const WIN_COLOR = { ct: 'var(--ct)', t: 'var(--t)' };
    MATCHES[mi].rounds.forEach(r => {
        const btn = document.createElement('button');
        btn.className = 'rbtn';
        btn.dataset.mi = mi;
        btn.dataset.rn = r.round_num;
        const w = (r.winner || '').toLowerCase();
        const reasonIcon = roundReasonIcon(r.reason);
        const reasonTxt  = roundReasonLabel(r.reason);
        const sideLetter = w === 'ct' ? 'C' : w === 't' ? 'T' : '';
        btn.style.setProperty('--wc', WIN_COLOR[w] || 'transparent');
        btn.innerHTML = `<span class="rn">${r.round_num}</span>` +
            (sideLetter ? `<span class="rside ${w}">${sideLetter}</span>` : '') +
            (reasonIcon ? `<span class="rreason">${reasonIcon}</span>` : '');
        btn.title = `M${mi + 1} round ${r.round_num} - ${w ? w.toUpperCase() + ' win' : 'winner unknown'}${reasonTxt ? ' · ' + reasonTxt : ''}`;
        btn.onclick = () => toggleRound(mi, r.round_num);
        grid.appendChild(btn);
    });

    // Header score (CT-side wins : T-side wins)
    const score = document.getElementById('mscore-' + mi);
    if (score) {
        const ct = MATCHES[mi].rounds.filter(r => (r.winner || '').toLowerCase() === 'ct').length;
        const t  = MATCHES[mi].rounds.filter(r => (r.winner || '').toLowerCase() === 't').length;
        score.innerHTML = `<span class="ct">${ct}</span>:<span class="t">${t}</span>`;
    }

    buildRoundFiltersFor(mi);
}

// Render one match's T1/T2 CT/T-win filter checkboxes. Each adds or removes
// exactly the rounds matching its def (ROUND_FILTER_DEFS), and because the
// four buckets are disjoint, checking several is a union with no double-count.
function buildRoundFiltersFor(mi) {
    const box = document.getElementById('rfilters-' + mi);
    if (!box) return;
    box.innerHTML = '';
    ROUND_FILTER_DEFS.forEach(def => {
        const label = document.createElement('label');
        label.className = 'chk-inline';
        label.innerHTML = `<input type="checkbox" data-key="${def.key}"> ${def.label}`;
        label.querySelector('input').onchange = e => applyRoundFilter(mi, def, e.target.checked);
        box.appendChild(label);
    });
    syncRoundFilterCheckboxes(mi);
}

// Union (on) or remove (off) def's matching rounds in match mi - other
// matches, and this match's rounds matching OTHER defs, are untouched.
function applyRoundFilter(mi, def, on) {
    const matching = MATCHES[mi].rounds.filter(r => roundMatchesDef(r, def));
    if (on) {
        matching.forEach(r => {
            if (!selectedRounds.some(s => s.m === mi && s.rn === r.round_num))
                selectedRounds.push({ m: mi, rn: r.round_num });
        });
    } else {
        const rns = new Set(matching.map(r => r.round_num));
        selectedRounds = selectedRounds.filter(s => !(s.m === mi && rns.has(s.rn)));
    }
    syncSelectionUI();
    rebuild();
}

// Standard "select-all" tri-state: checked if every one of def's rounds is
// selected, indeterminate if some are, unchecked if none are.
function syncRoundFilterCheckboxes(mi) {
    const box = document.getElementById('rfilters-' + mi);
    if (!box || !MATCHES[mi].rounds.length) return;
    const selRns = new Set(selectedRounds.filter(s => s.m === mi).map(s => s.rn));
    ROUND_FILTER_DEFS.forEach(def => {
        const input = box.querySelector(`input[data-key="${def.key}"]`);
        if (!input) return;
        const matching = MATCHES[mi].rounds.filter(r => roundMatchesDef(r, def));
        const nSel = matching.filter(r => selRns.has(r.round_num)).length;
        input.checked = matching.length > 0 && nSel === matching.length;
        input.indeterminate = nSel > 0 && nSel < matching.length;
    });
}

// Wipe match mi's round selection (used by the collapsed-legend "×" summary chip).
function clearMatchRounds(mi) {
    selectedRounds = selectedRounds.filter(s => s.m !== mi);
    syncSelectionUI();
    rebuild();
}

// Player panel: one block per LOADED match, each split into fixed T1/T2
// (roster-identity) columns via matchTeamIdOf - same convention as the round
// filters above, so a player always shows under the team they actually played
// for in that match, not just their side in whichever kill first named them.
// Checkbox/name toggles visibility on the map; ◎ toggles spotlight (additive -
// several players can be spotlighted at once); 💣 toggles utility spotlight
// (also additive - several players' smokes/flashes/nades can show at once).
function buildPlayerGroups() {
    playerGroups.innerHTML = '';
    const loaded = MATCHES.filter(M => M.loaded);

    for (const M of loaded) {
        const ps = roster.filter(p => p.matches.has(M.idx));
        if (!ps.length) continue;
        const t1 = ps.filter(p => matchTeamIdOf(M, p.name) === 1);
        const t2 = ps.filter(p => matchTeamIdOf(M, p.name) === 2);
        const unk = ps.filter(p => matchTeamIdOf(M, p.name) === 0);

        const block = document.createElement('div');
        block.className = 'pmatch-block';
        block.innerHTML = `
            <div class="pmatch-head"><span class="mtag" style="--mc:${matchColor(M.idx)}">M${M.idx + 1}</span><span>${M.label}</span></div>
            <div class="pgroup-pair">
                <div class="pgroup"><div class="pgroup-head ct"><span>Team 1</span></div><div class="pgroup-rows" data-team="1"></div></div>
                <div class="pgroup"><div class="pgroup-head t"><span>Team 2</span></div><div class="pgroup-rows" data-team="2"></div></div>
            </div>`;
        if (unk.length) {
            const extra = document.createElement('div');
            extra.className = 'pgroup';
            extra.innerHTML = `<div class="pgroup-head"><span>Unassigned</span></div><div class="pgroup-rows" data-team="0"></div>`;
            block.appendChild(extra);
        }

        [[t1, '1'], [t2, '2'], [unk, '0']].forEach(([players, teamAttr]) => {
            const rowsEl = block.querySelector(`.pgroup-rows[data-team="${teamAttr}"]`);
            if (!rowsEl) return;
            players.forEach(p => rowsEl.appendChild(buildPlayerRow(p)));
        });
        playerGroups.appendChild(block);
    }
    applyPlayerSearch();
}

function buildPlayerRow(p) {
    const row = document.createElement('div');
    row.className = `player-row ${p.side === 't' ? 't' : 'ct'}`;
    row.dataset.name = encodeURIComponent(p.name);
    row.innerHTML = `
        <input type="checkbox" title="Show on map">
        <span class="player-name" title="Show/hide on map">${p.name}</span>
        <span class="util-btn" title="Utility spotlight - additive, toggle this player's smokes/flashes/HE/nades">💣</span>
        <span class="focus-btn" title="Spotlight - additive, highlight this player and dim everyone else">◎</span>`;
    const setSelected = on => {
        if (on) selectedPlayers.add(p.name);
        else { selectedPlayers.delete(p.name); focusedPlayers.delete(p.name); }
        syncSelectionUI();
        rebuild();
    };
    row.querySelector('input').onchange = e => setSelected(e.target.checked);
    row.querySelector('.player-name').onclick = () => setSelected(!selectedPlayers.has(p.name));
    row.querySelector('.focus-btn').onclick = () => {
        const wasHidden = !selectedPlayers.has(p.name);
        selectedPlayers.add(p.name);   // spotlighting an off player turns it on
        if (focusedPlayers.has(p.name)) focusedPlayers.delete(p.name);
        else focusedPlayers.add(p.name);
        syncSelectionUI();
        if (wasHidden) rebuild(); else render();
    };
    row.querySelector('.util-btn').onclick = () => {
        if (utilityOwners.has(p.name)) utilityOwners.delete(p.name);
        else utilityOwners.add(p.name);
        syncSelectionUI();
        render();
    };
    return row;
}

function applyPlayerSearch() {
    const q = (playerSearch.value || '').trim().toLowerCase();
    document.querySelectorAll('.player-row').forEach(row => {
        const name = decodeURIComponent(row.dataset.name).toLowerCase();
        row.style.display = !q || name.includes(q) ? '' : 'none';
    });
}

// Enter in the search box adds every player currently passing the substring
// filter to the selection (additive - repeat with a different query to build
// up a multi-player selection) without touching spotlight/utility state.
function addMatchingPlayers() {
    const q = (playerSearch.value || '').trim().toLowerCase();
    if (!q) return;
    let added = 0;
    roster.forEach(p => {
        if (p.name.toLowerCase().includes(q) && !selectedPlayers.has(p.name)) {
            selectedPlayers.add(p.name);
            added++;
        }
    });
    if (!added) return;
    playerSearch.value = '';
    applyPlayerSearch();
    syncSelectionUI();
    rebuild();
}

const LEGEND_CHIP_CAP = 16;   // above this, one summary chip per match instead

function buildLegend() {
    legendEl.innerHTML = '';
    if (selectedRounds.length < 1) return;
    const multiMatch = new Set(selectedRounds.map(s => s.m)).size > 1 || MATCHES.length > 1;

    // With many rounds selected, per-round chips become a wall - collapse to
    // one summary chip per match (× clears that match's rounds).
    if (selectedRounds.length > LEGEND_CHIP_CAP) {
        const byMatch = new Map();
        for (const sel of selectedRounds) byMatch.set(sel.m, (byMatch.get(sel.m) || 0) + 1);
        for (const [mi, n] of [...byMatch].sort((a, b) => a[0] - b[0])) {
            const item = document.createElement('div');
            item.className = 'leg-item';
            item.innerHTML = `<span class="leg-dot" style="background:${matchColor(mi)}"></span>
                M${mi + 1} <span class="leg-sub">${n} rounds</span>
                <span class="leg-x" title="Remove this match's rounds">×</span>`;
            item.querySelector('.leg-x').onclick = () => clearMatchRounds(mi);
            legendEl.appendChild(item);
        }
        return;
    }
    selectedRounds.forEach((sel, k) => {
        const M = MATCHES[sel.m];
        const r = M.roundByNum.get(sel.rn);
        const item = document.createElement('div');
        item.className = 'leg-item';
        const len = tracks[keyOf(sel)] ? (tracks[keyOf(sel)].len / TICKRATE).toFixed(0) : '?';
        const lab = multiMatch ? `M${sel.m + 1}·R${sel.rn}` : `R${sel.rn}`;
        item.innerHTML = `<span class="leg-dot" style="background:${selColor(sel, k)}"></span>
            ${lab} <span class="leg-sub">${(r && r.winner ? r.winner.toUpperCase() : '')} · ${len}s</span>
            <span class="leg-x" title="Remove this round">×</span>`;
        item.querySelector('.leg-x').onclick = () => toggleRound(sel.m, sel.rn);
        legendEl.appendChild(item);
    });
}

function toggleRound(mi, rn) {
    const i = selectedRounds.findIndex(s => s.m === mi && s.rn === rn);
    if (i >= 0) selectedRounds.splice(i, 1);
    else selectedRounds.push({ m: mi, rn });
    syncSelectionUI();
    rebuild();
}

function syncSelectionUI() {
    document.querySelectorAll('.rbtn').forEach(b => {
        const mi = Number(b.dataset.mi), rn = Number(b.dataset.rn);
        const i = selectedRounds.findIndex(s => s.m === mi && s.rn === rn);
        b.classList.toggle('sel', i >= 0);
        b.style.setProperty('--rc', i >= 0 ? selColor(selectedRounds[i], i) : 'transparent');
    });
    MATCHES.forEach((M, mi) => {
        const el = document.getElementById('mcount-' + mi);
        if (el) {
            const n = selectedRounds.filter(s => s.m === mi).length;
            el.textContent = n ? `${n} sel` : '';
        }
        syncRoundFilterCheckboxes(mi);
    });
    document.querySelectorAll('.player-row').forEach(row => {
        const name = decodeURIComponent(row.dataset.name);
        row.classList.toggle('sel', selectedPlayers.has(name));
        row.classList.toggle('focused', focusedPlayers.has(name));
        row.classList.toggle('util-only', utilityOwners.has(name));
        const box = row.querySelector('input');
        if (box) box.checked = selectedPlayers.has(name);
    });
    const pc = document.getElementById('player-count');
    if (pc) pc.textContent = roster.length ? `- ${selectedPlayers.size}/${roster.length} shown` : '';
}

function syncSliders() {
    if (viewMode === 'density') {
        timelineA.style.display = '';
        timelineA.value = winA;
        timeline.value  = winB;
    } else {
        timelineA.style.display = 'none';
        timeline.value = relTick;
    }
}

// ═══════════════════════════════════════════════════════════════════════════
// Controls
// ═══════════════════════════════════════════════════════════════════════════
function wireControls() {
    // Sidebar tab bar (Rounds/Player/Display) - same click pattern as the
    // stats page's .tab-bar/.tab-btn: only one .tab-panel shows at a time.
    document.querySelectorAll('.tab-btn[data-side-tab]').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.tab-btn[data-side-tab]').forEach(b => b.classList.remove('active'));
            document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
            btn.classList.add('active');
            document.getElementById('side-tab-' + btn.dataset.sideTab).classList.add('active');
        });
    });

    // Shared pan/zoom (wheel = zoom-at-cursor, drag = pan, dbl-click = reset).
    // Redraw on interaction while paused; the play loop already redraws each frame.
    Viewport.bindPanZoom(canvas, view, () => { if (!isPlaying) render(); });

    document.getElementById('btnPlay').onclick  = play;
    document.getElementById('btnPause').onclick = pause;
    document.getElementById('btnStop').onclick  = () => { pause(); relTick = 0; render(); };

    timeline.oninput = e => {
        const v = Number(e.target.value);
        if (viewMode === 'density') winB = v;
        else relTick = v;
        render();
    };
    timelineA.oninput = e => { winA = Number(e.target.value); render(); };

    document.querySelectorAll('.speed-btn').forEach(b => b.onclick = function () {
        speedMult = parseFloat(this.dataset.speed);
        document.querySelectorAll('.speed-btn').forEach(x => x.classList.remove('active'));
        this.classList.add('active');
    });

    document.querySelectorAll('[data-view]').forEach(b => b.onclick = function () {
        viewMode = this.dataset.view;
        document.querySelectorAll('[data-view]').forEach(x => x.classList.toggle('active', x === this));
        if (viewMode === 'density') {
            pause();
            if (winB <= 0 || winB > maxLen) winB = maxLen;
        }
        syncSliders();
        render();
    });

    document.querySelectorAll('[data-util]').forEach(b => b.onclick = function () {
        const key = this.dataset.util;
        showUtil[key] = !showUtil[key];
        this.classList.toggle('active', showUtil[key]);
        this.classList.toggle('off', !showUtil[key]);
        // Switching trails ON may need the grenade trajectories the slim match
        // document leaves out - that's a fetch, so go through rebuild() rather
        // than render(). Every other toggle is pure display.
        if (key === 'nade' && showUtil.nade && selectedRounds.some(s => needsGrenades(MATCHES[s.m]))) {
            rebuild();
            return;
        }
        render();
    });

    document.querySelectorAll('[data-color]').forEach(b => b.onclick = function () {
        colorMode = this.dataset.color;
        document.querySelectorAll('[data-color]').forEach(x => x.classList.remove('active'));
        this.classList.add('active');
        syncSelectionUI();
        buildLegend();
        render();
    });

    document.querySelectorAll('[data-names]').forEach(b => b.onclick = function () {
        nameMode = this.dataset.names;
        document.querySelectorAll('[data-names]').forEach(x => x.classList.remove('active'));
        this.classList.add('active');
        render();
    });

    // HUD size. The slider carries whole percents; hudScale is the multiplier.
    // Seeded from the stored value BEFORE the listener is attached, or a
    // remembered 140% would render at 140% behind a control reading 100.
    const hudScaleEl  = document.getElementById('hud-scale');
    const hudScaleLbl = document.getElementById('hud-scale-val');
    const syncHudLbl  = () => {
        if (hudScaleLbl) hudScaleLbl.textContent = Math.round(hudScale * 100) + '%';
    };
    if (hudScaleEl) hudScaleEl.value = Math.round(hudScale * 100);
    syncHudLbl();
    if (hudScaleEl) hudScaleEl.oninput = e => {
        hudScale = (+e.target.value) / 100;
        syncHudLbl();
        try { localStorage.setItem(HUD_SCALE_KEY, String(hudScale)); } catch (err) { /* storage blocked - the session still works */ }
        render();
    };

    document.getElementById('chkTrails').onchange = e => {
        showTrails = e.target.checked;
        render();
    };

    document.getElementById('chkFreeze').onchange = e => {
        includeFreeze = e.target.checked;
        relTick = 0;
        rebuild();
    };

    document.getElementById('chkFootsteps').onchange = e => {
        showFootsteps = e.target.checked;
        render();
    };

    playerSearch.oninput = applyPlayerSearch;
    playerSearch.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); addMatchingPlayers(); } });

    // Space = play/pause, ←/→ = step one tick (Shift = 1 second). Ignored while typing.
    document.addEventListener('keydown', e => {
        if (e.target.tagName === 'INPUT' && e.target.type === 'text') return;
        if (e.code === 'Space') {
            e.preventDefault();
            isPlaying ? pause() : play();
        } else if ((e.key === 'ArrowLeft' || e.key === 'ArrowRight') && viewMode === 'play') {
            e.preventDefault();
            const step = (e.shiftKey ? TICKRATE : 1) * (e.key === 'ArrowLeft' ? -1 : 1);
            relTick = Math.max(0, Math.min(maxLen, relTick + step));
            render();
        }
    });

    document.getElementById('btnAllPlayers').onclick = () => {
        selectedPlayers = new Set(roster.map(p => p.name));
        syncSelectionUI(); rebuild();
    };
    document.getElementById('btnNoPlayers').onclick = () => {
        selectedPlayers.clear(); focusedPlayers.clear();
        syncSelectionUI(); rebuild();
    };
    document.getElementById('btnClearRounds').onclick = clearOverlayRounds;

    const layerRow = document.getElementById('layer-row');
    if (layerRow) layerRow.addEventListener('click', (e) => {
        const btn = e.target.closest('[data-layer]');
        if (!btn) return;
        layerMode = btn.dataset.layer;
        shownLayer = MapLayersLogic.resolve(mapName, layerMode, playersOnScreen(), shownLayer);
        updateLayerButtons();
        render();
    });
    updateLayerButtons();
}

// Take every round back off the map. Wired to the Rounds tab's "Clear all"
// and to the results header's "Clear overlay" (Analyser's onClearRounds hook),
// which is the counterpart to "+ Overlay all" - that button could fill the map
// from the results band but there was no way to empty it again without
// switching tabs.
function clearOverlayRounds() {
    selectedRounds = []; syncSelectionUI(); rebuild();
}

function play() {
    if (isPlaying || !selectedRounds.length || viewMode === 'density') return;
    if (relTick >= maxLen) relTick = 0;
    isPlaying = true;
    document.getElementById('btnPlay').classList.add('active');
    lastFrameTime = performance.now();
    requestAnimationFrame(animate);
}
function pause() {
    isPlaying = false;
    document.getElementById('btnPlay').classList.remove('active');
}
function animate(ts) {
    if (!isPlaying) return;
    const interval = 1000 / (TICKRATE * speedMult);
    if (ts - lastFrameTime >= interval) {
        const steps = Math.max(1, Math.floor((ts - lastFrameTime) / interval));
        lastFrameTime = ts;
        relTick += steps;
        if (relTick >= maxLen) { relTick = maxLen; render(); pause(); return; }
        render();
    }
    requestAnimationFrame(animate);
}

// ═══════════════════════════════════════════════════════════════════════════
// Query results → overlay
// ═══════════════════════════════════════════════════════════════════════════

/**
 * Take moments matched by the query panel (static/analyser.js) and put their
 * rounds on the map.
 *
 * The overlay's selection unit is still a whole round, so a moment is added as
 * the round it happened in - the next pass makes a time *segment* the unit so
 * tracks can be aligned on the moment itself rather than on the round start.
 *
 * Two constraints the query side deliberately doesn't enforce, because they're
 * properties of the canvas rather than of the question:
 *   - the overlay draws one map, so rows from other maps are reported and
 *     skipped rather than silently drawn against the wrong radar;
 *   - a match the page wasn't opened with isn't loaded, and loading it is a
 *     fetch, so this is async.
 *
 * @param {Array<{match: string, round: number, player?: string}>} picks
 *        matched moment rows
 * @returns {Promise<void>} reports skipped rows through setStatus()
 */
async function addQueryRounds(picks) {
    if (!picks || !picks.length) return;
    const wanted = [], skipped = { map: 0, unknown: 0 };
    for (const p of picks) {
        const mi = MATCHES.findIndex(M => M.file === p.match);
        if (mi < 0) { skipped.unknown++; continue; }
        wanted.push({ mi, rn: p.round, map: p.map, player: p.player });
    }
    if (!wanted.length) {
        setStatus(`Those moments are on another map - ${skipped.unknown} skipped. `
            + `The overlay draws ${SCOPE_CTX.map || 'one map'} only.`);
        return;
    }

    showLoading('Loading matched rounds…');
    // Expanding the section is what loads the match; forceOpen keeps the
    // round grid in sync with what's about to appear on the map.
    for (const mi of [...new Set(wanted.map(w => w.mi))]) {
        await toggleSection(mi, true);
    }

    let added = 0;
    for (const w of wanted) {
        const M = MATCHES[w.mi];
        if (!M.loaded) continue;
        // mapName latches to the first loaded match; anything else would be
        // drawn against the wrong radar. Compare canonical names, or two
        // spellings of one map read as two different maps.
        if (mapName && M.master && M.master.mapName &&
            MapsLogic.canonical(M.master.mapName) !== mapName) {
            skipped.map++;
            continue;
        }
        if (!M.roundByNum.has(w.rn)) continue;
        if (selectedRounds.some(s => s.m === w.mi && s.rn === w.rn)) continue;
        selectedRounds.push({ m: w.mi, rn: w.rn });
        added++;
    }

    // Focus the players the query was about, so an overlay of 12 rounds isn't
    // 60 indistinguishable dots.
    const names = [...new Set(picks.map(p => p.player).filter(Boolean))];
    names.forEach(n => { selectedPlayers.add(n); focusedPlayers.add(n); });

    hideLoading();
    syncSelectionUI();
    await rebuild();
    if (skipped.map || skipped.unknown) {
        setStatus(`${added} round${added === 1 ? '' : 's'} added · `
            + [skipped.map ? `${skipped.map} on another map` : '',
               skipped.unknown ? `${skipped.unknown} in unopened matches` : '']
              .filter(Boolean).join(', ') + ' skipped');
    }
}

// ═══════════════════════════════════════════════════════════════════════════
// Init
// ═══════════════════════════════════════════════════════════════════════════
if (typeof Analyser !== 'undefined') {
    Analyser.init({ onAddRounds: addQueryRounds, onClearRounds: clearOverlayRounds,
                    onHeat: setQueryHeat,
                    onScope: onScopeChanged, onMapChange: goToMap });
    if (scopeSegsEl) {
        scopeSegsEl.addEventListener('click', e => {
            const seg = e.target.closest('.sb-seg');
            if (seg) Analyser.setScope(seg.dataset.kind);
        });
    }
    if (scopeMapEl) {
        scopeMapEl.addEventListener('change', () => {
            // Under an all-maps scope the picker only repoints the radar; under
            // any narrower scope it means "analyse that map".
            Analyser.requestMap(scopeMapEl.value,
                                scopeState && scopeState.kind === 'all');
        });
    }
}

// The sidebar tabs, the transport and the canvas gestures touch only static
// DOM, so they are wired even with nothing loaded - a page with no matches used
// to have no working tabs at all.
wireControls();

if (!MATCHES.length) {
    showLoading(SCOPE_CTX.map ? 'No processed matches for this map'
                              : 'No processed matches yet');
} else {
    buildMatchSections();
    const initIdx = Math.max(0, MATCHES.findIndex(M => M.file === SCOPE_CTX.entry));
    toggleSection(initIdx, true).then(() => {
        const M = MATCHES[initIdx];
        // A match still loads, because that is what latches the radar - but on
        // the general entry nobody asked for a particular round, and putting
        // one on the map would make an arbitrary match's tracks the page's
        // opening statement. The query's own result heatmap fills that space
        // instead.
        if (!SCOPE_CTX.entry) return;
        if (M.loaded && M.rounds.length) {
            selectedRounds = [{ m: initIdx, rn: M.rounds[0].round_num }];
            selectedPlayers = new Set(roster.map(p => p.name));
            syncSelectionUI();
            rebuild();
        }
    });
}
