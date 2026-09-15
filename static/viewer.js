/**
 * viewer.js - the 2D replayer (`/viewer`, templates/index.html).
 *
 * Plays one match back on a top-down radar: player positions and facing,
 * sprays, utility at true in-game radii, grenade trails, ground items, bomb
 * state, an in-canvas kill feed, per-player sidebars, and a round timeline.
 *
 * Shape of the file (the `// ─── X ───` banners below match this order):
 *   DOM + constants + state   module-level `let`s; there is no framework and
 *                             no store - render() reads these directly
 *   data load                 one fetch for the match JSON, then ChunkSource
 *                             for tick data
 *   render                    the frame loop (see the convention below)
 *   draw helpers              bomb, ground items, trails, utility, HE,
 *                             players, flash overlay
 *   sidebar                   DOM rows, updated in place (see below)
 *   playback / interaction    timeline, pan-zoom, follow-lock, controls
 *
 * Ticks come from static/chunks.js (`ChunkSource.create`), which decodes v2
 * binary chunks in the shared decoder Web Worker and hands back the same
 * tick-dict shape the old JSON format used - so nothing here knows about the
 * binary format. A `data.ticks` branch remains for pre-2026-07 matches that
 * still carry flat ticks; it is dead for anything parsed since.
 *
 * Three conventions that are easy to break:
 *
 *   1. Render order. worldToCanvas() returns *base* (un-zoomed 1024²) coords.
 *      render() applies the viewport transform, draws the background and every
 *      world entity under it, then calls Viewport.clear(ctx) before drawing
 *      screen-space HUD (kill feed, scoreboard). Adding a world draw after the
 *      clear pins it to the screen instead of the map.
 *   2. Sidebar rows are updated IN PLACE, never rebuilt. A full innerHTML
 *      rebuild each frame broke click-to-follow during playback and made
 *      animation impossible.
 *   3. Per-player fade uses ctx.filter, not globalAlpha - the marker sets
 *      globalAlpha itself, so an outer value is simply overwritten.
 *
 * Guard every field the parser added later (`p.field != null`): old matches
 * are still expected to render.
 */

// ─── DOM ──────────────────────────────────────────────────────────────────
const canvas          = document.getElementById('mapCanvas');
const ctx             = canvas.getContext('2d');
const timeline        = document.getElementById('timeline');
const tickDisplay     = document.getElementById('tickDisplay');
const frameDisplay    = document.getElementById('frameDisplay');
const roundLabel      = document.getElementById('roundLabel');
const timeLabel       = document.getElementById('timeLabel');
const loadingEl       = document.getElementById('loading');
const timelineProg    = document.getElementById('timeline-progress');
// Team panels: fixed team identity (team1 = first-half CT, left; team2 = right).
const team1List       = document.getElementById('team1-list');
const team2List       = document.getElementById('team2-list');
const team1MoneyEl    = document.getElementById('team1-money');
const team2MoneyEl    = document.getElementById('team2-money');
const team1NameEl     = document.getElementById('team1-name');
const team2NameEl     = document.getElementById('team2-name');
const roundBtnScroll  = document.getElementById('round-btn-scroll');
const timelineSegs    = document.getElementById('timeline-segments');

// ─── Map Library (shared with multi.js/match.html - static/match2d.logic.js) ──
const MAP_LIBRARY = Match2DLogic.MAP_LIBRARY;

// ─── Constants ────────────────────────────────────────────────────────────
const TICKRATE             = Match2DLogic.TICKRATE;
const SPRAY_DURATION_TICKS = Match2DLogic.SPRAY_DURATION_TICKS;
const FLASH_MAX_TICKS      = TICKRATE * 3.4;
const HE_FADE_TICKS        = Match2DLogic.HE_FADE_TICKS;
const TRAIL_LOOKBACK_TICKS = 20;
const KILL_FEED_MAX        = 8;
const KILL_FRESH_TICKS     = 192;
const BOMB_TIME_TICKS      = 40  * TICKRATE;   // 40s fuse
const ROUND_TIME_TICKS     = 115 * TICKRATE;   // 1:55 regulation
const FREEZE_TIME_TICKS    = 15  * TICKRATE;   // 15s freeze
// Audibility (Display toggle). The radii, the silent-movement cutoff and the
// fade band all live in static/sound.logic.js, which also covers the sounds
// that are not footsteps - gunfire, detonations, the bomb. Speed is smoothed
// over a long backward-looking window (below) because raw per-tick velocity is
// noisy enough to make the ring flicker on and off at the threshold.
const FOOTSTEP_SMOOTH_WINDOW = 32;  // index-steps back to average (~1s, same loose convention as PLAYER_TRAIL_TICKS)
const REGULATION_HALF      = Match2DLogic.REGULATION_HALF;
const REGULATION_ROUNDS    = Match2DLogic.REGULATION_ROUNDS;
const OT_HALF_LEN          = Match2DLogic.OT_HALF_LEN;

// ─── State ────────────────────────────────────────────────────────────────
let matchData    = null;   // active chunk window: {tickStr: [players]}
let sortedTicks  = [];
let allRounds    = [];
let allKills     = [];
let allShots     = [];
let allSmokes    = [];
let allInfernos  = [];
let allFlashes   = [];
let allHE        = [];
let allGrenades  = [];
let allBomb      = [];
let allItems     = [];    // dropped weapons/utility on the ground (grounditems.logic.js)
let infernoWeapons = [];   // 'Molotov' | 'Incendiary' per allInfernos entry (utilfx)

let currentIdx    = 0;
let isPlaying     = false;
let otStartSwapped = null;  // null=undetected; true=OT1 first half has same sides as reg second half
let lastFrameTime = 0;
let speedMult     = 1.0;

let mapImg        = new Image();
let mapConfig     = null;
let mapImageReady = false;
// Canonical map name for this match. Module-scoped because the multi-level
// layer code needs it on every frame, not just at load.
let mapName       = '';

// Shared pan/zoom view transform (static/viewport.js). worldToCanvas() keeps
// returning base-space coords; render() applies this as the ctx transform around
// world drawing so every dot/trail/util inherits zoom for free. Bound to the
// canvas below; onChange re-renders (needed while paused).
const view = ViewportLogic.create({ minScale: 1, maxScale: 8 });

// ── Display / interaction state (same shape multi.js uses) ───────────────────
// Layer toggles, all read directly by render().
//   util   utility (smokes/molotovs/flashes) at true in-game radii
//   trails recent movement paths
//   sound  audibility rings - every audible event, not just footsteps
//          (static/sound.logic.js owns the radii)
//   drops  weapons/utility lying on the ground
//   freeze where clicking a round lands: false = round.freeze_end (live),
//          true = round.start (the buy)
const display = { util: true, trails: false, sound: false, drops: true, freeze: false,
                  bomb: true };   // plant/defuse ring + the HUD strip
const HUD_SCALE_KEY = 'cs2viewer.hudScale';

// ── Multi-level maps (Nuke, Vertigo) ─────────────────────────────────────────
// `layerMode` is what the user asked for ('auto' | 'upper' | 'lower');
// `shownLayer` is what that resolves to for the current frame, and is null on
// every single-level map - which is what keeps all of this inert everywhere
// else. See static/maplayers.logic.js.
let layerMode  = 'auto';
let shownLayer = null;
// The lower-floor radar is a second image, loaded lazily the first time that
// floor is shown so a flat map never fetches one.
const mapImgLower = new Image();
let mapImgLowerReady = false, mapImgLowerRequested = false;
const hiddenPlayers = new Set();                 // names opted out of rendering
let grenadeLookback  = TRAIL_LOOKBACK_TICKS;     // utility-time slider (10..160)
// In-canvas HUD scale (scoreboard + kill feed), 0.6..2.0 from the Display box.
// Persisted because it is a per-USER display preference - someone on a 4K
// panel wants a bigger HUD every session - unlike the other Display toggles,
// which are per-analysis and deliberately reset.
let hudScale = (() => {
    try {
        const v = parseFloat(localStorage.getItem(HUD_SCALE_KEY));
        return (isFinite(v) && v >= 0.6 && v <= 2) ? v : 1;
    } catch (e) { return 1; }   // private mode / blocked storage → default
})();
const PLAYER_TRAIL_TICKS = Match2DLogic.PLAYER_TRAIL_TICKS;   // ~1.5 s
let lockedName = null;                           // follow/POV lock target, or null
let panZoom    = null;                           // Viewport.bindPanZoom handle
let telestrator = null;                          // telestration overlay (lazy)

// ─── Chunk state ──────────────────────────────────────────────────────────
let chunkSource  = null;        // ChunkSource (chunks.js) - v2 binary or JSON chunks
let chunkIndex   = [];          // unified chunk index from the source
let chunkCache   = new Map();   // chunkIdx -> tick-dict object
let globalTicks  = [];          // [{tick, chunkIdx}] - virtual full-match timeline
let loadedChunks = new Set();

const PREFETCH_AHEAD = 1;       // chunks to fetch ahead of playhead

// ─── URL params ───────────────────────────────────────────────────────────
const urlParams = new URLSearchParams(window.location.search);
const matchFile = urlParams.get('match');

// ─── Weapon classification ────────────────────────────────────────────────
const WEAPON_CLASS = {
    ak47:'rifle', m4a1:'rifle', m4a4:'rifle', m4a1_silencer:'rifle', awp:'sniper',
    sg556:'rifle', aug:'rifle', famas:'rifle', galilar:'rifle', ssg08:'sniper',
    g3sg1:'sniper', scar20:'sniper', xm1014:'heavy', nova:'heavy', mag7:'heavy',
    sawedoff:'heavy', m249:'heavy', negev:'heavy',
    mp9:'smg', mp7:'smg', mp5sd:'smg', ump45:'smg', p90:'smg', bizon:'smg', mac10:'smg',
    glock:'pistol', usps:'pistol', usp_silencer:'pistol', p2000:'pistol', hkp2000:'pistol', p250:'pistol',
    deagle:'pistol', revolver:'pistol', tec9:'pistol', fiveseven:'pistol', cz75a:'pistol', dual_berettas:'pistol',
    flashbang:'util', smokegrenade:'util', hegrenade:'util',
    molotov:'util', incgrenade:'util', decoy:'util',
    c4explosive:'bomb', knife:'knife', knife_t:'knife', deserteagle:'pistol'
};

function classifyWeapon(raw) {
    if (!raw || typeof raw !== 'string') return 'unknown';
    const cleanKey = raw.toLowerCase()
                        .replace('weapon_', '')
                        .replace(/[\s-]/g, '');
    return WEAPON_CLASS[cleanKey] || 'unknown';
}

// Short, fixed-width-friendly labels (≤4 chars) so tags never overflow their slot.
const EQUIP_LABELS = {
    weapon_ak47:'AK',  weapon_m4a1_silencer:'M4S', weapon_m4a1:'M4A1', weapon_m4a4:'M4A4',
    weapon_sg556:'SG', weapon_aug:'AUG', weapon_famas:'FAM', weapon_galilar:'GAL',
    weapon_awp:'AWP',  weapon_ssg08:'SSG', weapon_g3sg1:'G3', weapon_scar20:'S20',
    weapon_xm1014:'XM', weapon_nova:'NOV', weapon_mag7:'MAG', weapon_sawedoff:'SAW',
    weapon_m249:'M249', weapon_negev:'NEG',
    weapon_mp9:'MP9', weapon_mp7:'MP7', weapon_mp5sd:'MP5',
    weapon_ump45:'UMP', weapon_p90:'P90', weapon_bizon:'BIZ', weapon_mac10:'MAC',
    weapon_glock:'GLK', weapon_usp_silencer:'USP', weapon_usps:'USP',
    weapon_hkp2000:'P2K', weapon_p2000:'P2K', weapon_p250:'P250',
    weapon_fiveseven:'FN', weapon_cz75a:'CZ', weapon_tec9:'T9',
    weapon_dual_berettas:'DUAL', weapon_deagle:'DEAG', weapon_revolver:'R8',
    weapon_smokegrenade:'SMK', weapon_flashbang:'FL', weapon_hegrenade:'HE',
    weapon_molotov:'MOL', weapon_incgrenade:'MOL', weapon_decoy:'DEC',
};
function formatWeaponLabel(raw) {
    if (!raw) return '';
    const key = String(raw).toLowerCase();
    return EQUIP_LABELS[key] ?? key.replace('weapon_', '').replace(/_/g, '').toUpperCase().slice(0, 4);
}
function formatWeaponFeed(raw) {
    if (!raw) return '?';
    return raw.replace('weapon_', '').replace(/_/g, ' ').toUpperCase();
}

// ─── Helpers ─────────────────────────────────────────────────────────────
function formatTime(seconds) {
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60).toString().padStart(2, '0');
    return `${m}:${s}`;
}
function worldToCanvas(wx, wy) { return Match2DLogic.worldToCanvas(mapConfig, wx, wy); }
function isOnCanvas(pos, margin = 40) {
    // View-aware: a base point is visible only after the pan/zoom transform, so
    // culling must test the transformed (screen) position, not the base one.
    return ViewportLogic.isVisible(view, pos.x, pos.y, canvas.width, canvas.height, margin);
}
const easeOut = Match2DLogic.easeOut;

function showLoading(msg) {
    loadingEl.innerText     = msg || 'Loading...';
    loadingEl.style.display = 'block';
}
function hideLoading() { loadingEl.style.display = 'none'; }

// ─── Round / Bomb lookups ─────────────────────────────────────────────────
function getRoundNum(tick) {
    for (let i = allRounds.length - 1; i >= 0; i--) {
        if (allRounds[i].start <= tick) return allRounds[i].round_num;
    }
    return null;
}
function getRound(tick) {
    for (let i = allRounds.length - 1; i >= 0; i--) {
        if (allRounds[i].start != null && allRounds[i].start <= tick) return allRounds[i];
    }
    return null;
}
/**
 * Bomb state at a tick, round-scoped.
 * @param {number} currentTick
 * @returns {{state: string, x?: number, y?: number, player?: string}}
 *   state is 'carried' | 'drop' | 'plant' | 'defuse' | 'none'; x/y are world
 *   coords and are only meaningful for 'drop' and 'plant'.
 */
// `bomb[]` is ONE timeline carrying TWO kinds of entry: where the bomb is
// (plant / defuse / pickup / drop / detonate) and what someone is doing to it
// (plant_begin / plant_abort / defuse_start / defuse_abort, added 2026-09-10).
//
// This function is a last-event-wins scan, so it MUST ignore the action
// events. Without the filter a `defuse_start` - which by definition arrives
// after the `plant` - would become "the bomb state", `bombPlantTick` would go
// null, and the HUD would drop the PLANTED label and the 40 s countdown at
// the exact moment they matter most; drawBombOnMap() would stop drawing the
// bomb too, since it gates on plant/drop. Ask getBombAction() for the other
// half. (moments.py and sound.logic.js already filter by explicit event name,
// so they needed no equivalent change.)
function getBombState(currentTick) {
    // Scope to the current round so stale events from previous rounds don't bleed through.
    const roundStart = currentRoundStart(currentTick);
    let state = null;
    for (const b of allBomb) {
        if (b.tick < roundStart) continue;
        if (b.tick > currentTick) break;
        if (!BombActionLogic.isStateEvent(b.event)) continue;
        state = b;
    }
    return state;
}

// The plant or defuse in progress right now, or null. All the arithmetic -
// progress, remaining time, and whether a defuse beats the fuse - lives in
// static/bombaction.logic.js so it can be unit-tested without a canvas.
function getBombAction(currentTick) {
    return BombActionLogic.actionAt(allBomb, currentTick, currentRoundStart(currentTick));
}
function currentRoundStart(currentTick) {
    for (let i = allRounds.length - 1; i >= 0; i--) {
        if (allRounds[i].start != null && allRounds[i].start <= currentTick) return allRounds[i].start;
    }
    return 0;
}

// ─── OT side detection ───────────────────────────────────────────────────
// CS2 overtime (MR3): sides alternate every OT_HALF_LEN rounds after regulation.
// Which team starts OT as CT is decided in-game, so we detect it from player data
// rather than assuming. Detection is lazy - it succeeds only once the OT chunk is loaded.
function tryDetectOtSide() {
    if (otStartSwapped !== null) return;                         // already resolved
    if (!allRounds.some(r => r.round_num > REGULATION_ROUNDS)) return; // no OT in this match

    const r1   = allRounds[0];
    const otR1 = allRounds.find(r => r.round_num === REGULATION_ROUNDS + 1);
    if (!r1 || !otR1) return;

    const findNearestPlayers = (refTick) => {
        if (refTick == null) return null;
        let best = null, bestDiff = Infinity;
        for (const t of sortedTicks) {
            const diff = Math.abs(Number(t) - refTick);
            if (diff < bestDiff) { bestDiff = diff; best = t; }
            if (Number(t) > refTick + 512) break;
        }
        return best && bestDiff <= 256 ? matchData[best] : null;
    };

    const r1Players  = findNearestPlayers(r1.freeze_end  ?? r1.start);
    const otPlayers  = findNearestPlayers(otR1.freeze_end ?? otR1.start);
    if (!r1Players || !otPlayers) return;  // OT chunk not loaded yet - try again later

    const r1Ct  = new Set(r1Players.filter(p => p.side === 'ct').map(p => p.name));
    const otCt  = new Set(otPlayers.filter(p => p.side === 'ct').map(p => p.name));
    const overlap = [...r1Ct].filter(n => otCt.has(n)).length;
    // If fewer than half the round-1 CTs are still CT in OT, sides have been swapped relative to round 1
    otStartSwapped = overlap < r1Ct.size / 2;
}

// Returns true when teamA (first-half CTs in round 1) is on the T side for the given round.
// Stable team identity across the halftime side-swap: team1 = the side that
// played CT in the first half, team2 = the first-half T side. Uses the same
// swap detection as the scoreboard so panels never shuffle at halftime.
/**
 * Fixed team identity for a side in a round - the thing that survives the
 * halftime swap, so Team 1 stays on the left panel all match.
 * @param {string} side 'ct' | 't'
 * @param {number} roundNum
 * @returns {1|2} 1 = first-half CT, 2 = first-half T
 */
function teamOf(side, roundNum) {
    const swapped = isSwappedAtRound(roundNum || 1);
    if (side === 'ct') return swapped ? 'team2' : 'team1';
    if (side === 't')  return swapped ? 'team1' : 'team2';
    return 'team1';
}

function isSwappedAtRound(roundNum) {
    if (roundNum <= REGULATION_HALF)   return false;
    if (roundNum <= REGULATION_ROUNDS) return true;
    tryDetectOtSide();
    // Fallback while OT tick data not yet loaded: assume OT1 first half = same as regulation second half (swapped)
    const baseSwapped = otStartSwapped ?? true;
    const otHalfIdx   = Math.floor((roundNum - REGULATION_ROUNDS - 1) / OT_HALF_LEN);
    return (otHalfIdx % 2 === 0) ? baseSwapped : !baseSwapped;
}

// ═══════════════════════════════════════════════════════════════════════════
// ─── Chunk helpers ────────────────────────────────────────────────────────
// ═══════════════════════════════════════════════════════════════════════════

function chunkIdxForTick(tick) {
    for (let i = 0; i < chunkIndex.length; i++) {
        const c = chunkIndex[i];
        if (tick >= c.tickStart && tick <= c.tickEnd) return i;
    }
    return -1;
}

async function fetchChunk(idx) {
    if (chunkCache.has(idx)) return chunkCache.get(idx);
    // ChunkSource handles both formats: v2 .bin.br via the decoder worker
    // (converted to the same tick-dict shape) or legacy JSON chunks.
    const data = await chunkSource.fetchDict(idx);
    chunkCache.set(idx, data);
    return data;
}

/**
 * Make sure the chunk containing `tick` (and its neighbour) is decoded and
 * merged into matchData.
 * @param {number} tick
 * @returns {Promise<boolean>} true when new ticks were added, so the caller
 *   must re-derive sortedTicks - indices shift when a chunk lands.
 */
async function ensureChunksForTick(tick) {
    const targetIdx = chunkIdxForTick(tick);
    if (targetIdx === -1) return false;

    const needed = new Set([targetIdx]);
    for (let i = 1; i <= PREFETCH_AHEAD; i++) {
        if (targetIdx + i < chunkIndex.length) needed.add(targetIdx + i);
    }

    let allPresent = true;
    for (const idx of needed) {
        if (!loadedChunks.has(idx)) { allPresent = false; break; }
    }
    if (allPresent) return false;

    await Promise.all([...needed].filter(i => !loadedChunks.has(i)).map(fetchChunk));

    const windowStart = Math.max(0, targetIdx - 1);
    const windowEnd   = targetIdx + PREFETCH_AHEAD;

    matchData = {};
    loadedChunks.clear();
    for (let i = windowStart; i <= windowEnd && i < chunkIndex.length; i++) {
        const cached = chunkCache.get(i);
        if (!cached) continue;
        Object.assign(matchData, cached);
        loadedChunks.add(i);
    }

    sortedTicks = Object.keys(matchData).sort((a, b) => Number(a) - Number(b));
    return true;
}

async function seekToGlobalFrame(globalFrame) {
    const entry = globalTicks[globalFrame];
    if (!entry) return;

    showLoading('Loading round data...');
    try {
        await ensureChunksForTick(entry.tick);
    } catch (e) {
        console.error('Chunk load error:', e);
    }
    hideLoading();

    const tickStr  = String(entry.tick);
    let   localIdx = sortedTicks.indexOf(tickStr);
    if (localIdx === -1) {
        localIdx = sortedTicks.findIndex(t => Number(t) >= entry.tick);
        if (localIdx === -1) localIdx = sortedTicks.length - 1;
    }
    currentIdx     = localIdx;
    timeline.value = globalFrame;
    render();
}

// ═══════════════════════════════════════════════════════════════════════════
// ─── Data Load ────────────────────────────────────────────────────────────
// ═══════════════════════════════════════════════════════════════════════════

if (!matchFile) {
    showLoading('Error: No match file specified in URL (?match=...)');
} else {
    fetch(`/data/${matchFile}`)
        .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
        .then(async data => {
            mapName = MapsLogic.canonical(data.mapName);
            mapConfig = Match2DLogic.mapConfigFor(mapName,
                canon => console.warn(`Map "${canon}" not in library - using the default calibration.`));

            // Hero band (map icon/name/mode, avg rank, rounds/kills/dmg pills) -
            // shared with the stats page / multi-round analyser via matchhero.js.
            if (window.MatchHero) {
                MatchHero.update({
                    mapRaw: mapName, mode: (matchFile.replace('.json','').split('_').pop())||'',
                    ranks: data.ranks, faceitId: data.faceitMatchId,
                    rounds: (data.rounds||[]).length, kills: (data.kills||[]).length,
                    totalDmg: window.MatchHeroLogic ? MatchHeroLogic.totalDamage(data.damage) : 0,
                });
                document.getElementById('match-hero').style.display = '';
            }

            // ── Non-tick data (never chunked) ──────────────────────────────
            allRounds   = (data.rounds   || []).sort((a, b) => a.start - b.start);
            allKills    = (data.kills    || []).sort((a, b) => a.tick - b.tick);
            allShots    = (data.shots    || []).sort((a, b) => a.tick - b.tick);
            // end_tick is clamped to the real burn/screen duration - the demo's
            // own SmokeExpired/InfernoExpired events fire well after the actual
            // visual effect ends (entity/ember lingering), not at 18s/7.03s.
            allSmokes = (data.smokes || []).map(s => ({
                ...s, end_tick: UtilFXLogic.clampEnd(s.start_tick, s.end_tick, UtilFXLogic.SMOKE_LIFETIME_TICKS),
            }));
            allInfernos = (data.infernos || []).map(s => ({
                ...s, end_tick: UtilFXLogic.clampEnd(s.start_tick, s.end_tick, UtilFXLogic.FIRE_LIFETIME_TICKS),
            }));
            allFlashes  = data.flashes   || [];
            allHE       = data.he        || [];
            allGrenades = data.grenades  || [];
            allBomb     = (data.bomb     || []).sort((a, b) => a.tick - b.tick);
            allItems    = GroundItemsLogic.prepare(data.items);

            window.grenadeTrails = new Map();
            for (const g of allGrenades) {
                if (g.entity_id == null) continue;
                if (!window.grenadeTrails.has(g.entity_id))
                    window.grenadeTrails.set(g.entity_id, []);
                window.grenadeTrails.get(g.entity_id).push(
                    { tick: g.tick, X: g.X, Y: g.Y, type: g.grenade_type }
                );
            }
            for (const trail of window.grenadeTrails.values())
                trail.sort((a, b) => a.tick - b.tick);

            // Molotov vs CT incendiary per inferno (parser `weapon` field on
            // new parses, trajectory correlation on old data) - drives fire
            // footprint size + rim colour in drawUtility.
            infernoWeapons = UtilFXLogic.classifyInfernos(allInfernos, allGrenades);

            // ── Chunked path (v6+) - v2 binary chunks preferred ────────────
            chunkSource = ChunkSource.create(data);
            if (chunkSource.entries.length > 0) {
                chunkIndex = chunkSource.entries;

                showLoading('Loading first round...');
                const firstChunk = await fetchChunk(0);
                hideLoading();

                matchData    = { ...firstChunk };
                loadedChunks.add(0);
                sortedTicks  = Object.keys(matchData).sort((a, b) => Number(a) - Number(b));

                globalTicks = [];
                for (let ci = 0; ci < chunkIndex.length; ci++) {
                    const c = chunkIndex[ci];
                    if (ci === 0) {
                        // Use real tick keys from the loaded chunk
                        for (const t of sortedTicks)
                            globalTicks.push({ tick: Number(t), chunkIdx: ci });
                    } else {
                        // Synthetic entries at stored-snapshot resolution for unloaded chunks
                        const step = chunkSource.step;
                        for (let t = c.tickStart; t <= c.tickEnd; t += step)
                            globalTicks.push({ tick: t, chunkIdx: ci });
                    }
                }

                timeline.max = globalTicks.length - 1;
                currentIdx   = 0;

            // ── Legacy flat-ticks path (v5 fallback) ──────────────────────
            } else if (data.ticks) {
                matchData   = data.ticks;
                sortedTicks = Object.keys(matchData).sort((a, b) => Number(a) - Number(b));
                globalTicks = sortedTicks.map(t => ({ tick: Number(t), chunkIdx: -1 }));
                timeline.max = sortedTicks.length - 1;
                currentIdx   = 0;

            } else {
                throw new Error('match_data.json contains neither ticks nor chunkIndex.');
            }

            showLoading('Loading map image...');
            mapImg.onload  = () => { mapImageReady = true;  hideLoading(); render(); };
            mapImg.onerror = () => { mapImageReady = false; hideLoading(); render(); };
            mapImg.src = MapsLogic.radarUrl(mapName);

            // Build UI after data is fully available. Round nav is built once
            // (not per-frame like the sidebar loadout), so it must wait for the
            // asset manifest itself rather than risk permanently rendering the
            // emoji fallback on a slow first load.
            if (window.Assets) await Assets.load();
            buildRoundNav();
            buildTimelineSegments();
            updateLayerButtons();   // mapName is only known now

            // Fallback re-measure for static/fitview.js: the hero band and
            // round nav (both watched via ResizeObserver) just reached their
            // final size, and the very first measurement - taken before this
            // fetch resolved - saw them at 0/empty. The ResizeObserver should
            // already catch this on its own; this is defense in depth for an
            // environment where that notification doesn't fire.
            window.__fitView?.refresh();

            // Deep link: /viewer?match=...&tick=N starts at the nearest frame
            // (used by the match-page frag links)
            const tickParam = parseInt(urlParams.get('tick'));
            if (!isNaN(tickParam) && globalTicks.length > 0) {
                let best = 0, bestDiff = Infinity;
                for (let i = 0; i < globalTicks.length; i++) {
                    const diff = Math.abs(globalTicks[i].tick - tickParam);
                    if (diff < bestDiff) { bestDiff = diff; best = i; }
                }
                await seekToGlobalFrame(best);
            }
        })
        .catch(err => {
            showLoading(`Error: ${err.message}`);
            console.error('Failed to load match data:', err);
        });
}

// Last view the annotation overlay was painted under. Compared rather than
// redrawn unconditionally: the overlay is only repainted when the map has
// actually moved, so a stationary camera costs nothing per frame.
const telesView = { scale: NaN, tx: NaN, ty: NaN };
function syncTelestrationView() {
    if (!telestrator || telestrator.isEmpty()) return;
    if (telesView.scale === view.scale && telesView.tx === view.tx && telesView.ty === view.ty) return;
    telesView.scale = view.scale; telesView.tx = view.tx; telesView.ty = view.ty;
    telestrator.redraw();
}

// One audibility ring, in world space. Colour says what made the noise;
// everything is drawn thin and mostly transparent so a true-to-scale 4000-unit
// gunshot circle still reads as an overlay rather than covering the map.
const SOUND_COLOR = {
    footstep:  '255,235,150',
    gunshot:   '255,120,90',
    explosion: '255,170,60',
    bomb:      '120,220,255',
};
function drawSoundRing(x, y, radiusUnits, kind, alpha) {
    if (!mapConfig || !(alpha > 0)) return;
    const rgb = SOUND_COLOR[kind] || SOUND_COLOR.footstep;
    const rPx = radiusUnits / mapConfig.scale;
    ctx.beginPath();
    ctx.arc(x, y, rPx, 0, Math.PI * 2);
    ctx.fillStyle = `rgba(${rgb},${(0.06 * alpha).toFixed(3)})`;
    ctx.fill();
    ctx.strokeStyle = `rgba(${rgb},${(0.5 * alpha).toFixed(3)})`;
    ctx.lineWidth = 1.5;
    ctx.stroke();
}

// Gunfire, grenade detonations and the bomb. Not per player: these are world
// events with their own positions, and a shot is heard from where it was
// fired even after the shooter has moved or died.
function drawSoundEvents(currentTick) {
    if (!display.sound || !mapConfig) return;
    const round = getRound(currentTick);
    const sources = SoundLogic.events({
        tick: currentTick,
        roundStart: round ? (round.start ?? null) : null,
        shots: allShots, he: allHE, flashes: allFlashes, bomb: allBomb,
    });
    for (const s of sources) {
        const pos = worldToCanvas(s.x, s.y);
        drawSoundRing(pos.x, pos.y, s.radius, s.kind, s.alpha);
    }
}

// ── Multi-level maps ────────────────────────────────────────────────────────
// Resolve layerMode to the floor actually shown this frame, and make sure the
// matching radar image is loaded. In 'auto' the floor follows wherever most of
// the living players are, holding steady on a tie so a 5-5 split doesn't make
// the view flicker (MapLayersLogic.autoLayer).
/**
 * Resolve layerMode ('auto'|'upper'|'lower') to the floor shown this frame and
 * lazily load that floor's radar. Sets shownLayer to null on single-level maps,
 * which is what keeps the whole feature inert everywhere else.
 */
function resolveLayer() {
    if (!MapLayersLogic.isLayered(mapName)) { shownLayer = null; return; }
    const players = matchData ? (matchData[sortedTicks[currentIdx]] || []) : [];
    const next = MapLayersLogic.resolve(mapName, layerMode, players, shownLayer);
    if (next !== shownLayer) {
        shownLayer = next;
        updateLayerButtons();
    }
    if (shownLayer === MapLayersLogic.LOWER && !mapImgLowerRequested) {
        mapImgLowerRequested = true;
        mapImgLower.onload  = () => { mapImgLowerReady = true;  render(); };
        mapImgLower.onerror = () => { mapImgLowerReady = false; render(); };
        mapImgLower.src = MapsLogic.radarUrl(MapLayersLogic.radarName(mapName, MapLayersLogic.LOWER));
    }
}

// The radar to draw for the floor on show. Falls back to the base image if the
// lower one is missing or still loading, so a map whose _lower.png the user
// never saved degrades to today's behaviour instead of a blank canvas.
function currentRadar() {
    if (shownLayer === MapLayersLogic.LOWER && mapImgLowerReady) return mapImgLower;
    return mapImageReady ? mapImg : null;
}

// Opacity for one player given the floor on show. Off-floor players are dimmed,
// never hidden - they are still alive and still about to matter.
/**
 * Opacity multiplier for a player at world height `z` on a multi-level map.
 * @param {number|null|undefined} z  null/undefined on pre-Z parses
 * @returns {number} 1 on a flat map, on the shown floor, or when z is unknown;
 *   dimmed otherwise. Off-floor players are faded, never hidden.
 */
function layerAlpha(z) {
    return MapLayersLogic.alphaFor(mapName, z, shownLayer);
}

// Show the Layer control only on a map that has floors, and keep the segmented
// buttons in step with both the mode and (in 'auto') the resolved floor.
function updateLayerButtons() {
    const box = document.getElementById('layer-box');
    if (!box) return;
    const layered = MapLayersLogic.isLayered(mapName);
    box.style.display = layered ? '' : 'none';
    if (!layered) return;
    box.querySelectorAll('[data-layer]').forEach(b => {
        b.classList.toggle('active', b.dataset.layer === layerMode);
    });
    const auto = box.querySelector('[data-layer="auto"]');
    // In auto mode the button says which floor it settled on, so the map is
    // never showing a level the UI doesn't name.
    if (auto) auto.textContent = layerMode === 'auto' && shownLayer
        ? 'Auto · ' + (shownLayer === MapLayersLogic.LOWER ? 'lower' : 'upper')
        : 'Auto';
}

// ─── Render ───────────────────────────────────────────────────────────────
/**
 * Draw one frame. Order is load-bearing:
 *   applyLockFollow() -> Viewport.apply(ctx, view) -> background + every world
 *   entity (all using base 1024² coords) -> Viewport.clear(ctx) -> screen-space
 *   HUD (kill feed, scoreboard).
 * A world draw added after the clear is pinned to the screen, not the map.
 */
function render() {
    if (!matchData || sortedTicks.length === 0) return;
    currentIdx = Math.max(0, Math.min(currentIdx, sortedTicks.length - 1));

    ctx.clearRect(0, 0, canvas.width, canvas.height);
    // Follow/POV lock: recenter the view on the locked player before the
    // transform is applied, so zooming keeps them centred ("zoom while locked").
    applyLockFollow();
    // Annotations are pinned to the map, and a follow lock moves the map every
    // frame - so the overlay (a separate canvas that never repaints with
    // playback) has to be repainted whenever the transform actually changed.
    syncTelestrationView();
    // Which floor of a multi-level map is on show. Resolved once here so the
    // radar image, the players and the utility all agree within one frame.
    resolveLayer();
    // World space: background + all map entities draw under the pan/zoom
    // transform. Cleared back to identity before the screen-space HUD below.
    Viewport.apply(ctx, view);
    const radar = currentRadar();
    if (radar) {
        ctx.drawImage(radar, 0, 0, canvas.width, canvas.height);
    } else {
        ctx.fillStyle = '#0d1117';
        ctx.fillRect(0, 0, canvas.width, canvas.height);
    }

    const tickKey     = sortedTicks[currentIdx];
    const currentTick = Number(tickKey);
    const players     = matchData[tickKey];

    // HUD
    tickDisplay.innerText  = tickKey;
    frameDisplay.innerText = `${currentIdx + 1} / ${sortedTicks.length}`;
    const firstTick = globalTicks.length > 0 ? globalTicks[0].tick : 0;
    timeLabel.innerText = formatTime(Math.max(0, currentTick - firstTick) / TICKRATE);

    // Progress bar mapped to the global (full-match) timeline
    const gFrame = globalTicks.findIndex(g => g.tick === currentTick);
    const gIdx   = gFrame >= 0 ? gFrame : currentIdx;
    timeline.value = gIdx;
    const pct = globalTicks.length > 1 ? (gIdx / (globalTicks.length - 1)) * 100 : 0;
    timelineProg.style.width = `${pct}%`;

    // Round number
    const roundNum = getRoundNum(currentTick);
    if (roundNum != null) {
        roundLabel.innerText = roundNum;
    } else if (players && players.length > 0) {
        const r = players.find(p => p.round_num != null);
        roundLabel.innerText = r ? r.round_num : '-';
    } else {
        roundLabel.innerText = '-';
    }

    if (!players || players.length === 0) { Viewport.clear(ctx); return; }

    // Resolved once per frame and read by both drawPlayerMarker() (the ring on
    // the actor) and drawScoreboard() (the HUD line). Scanning bomb[] twice a
    // frame would be harmless - it is a few hundred rows - but one value means
    // the ring and the readout can never disagree about what is happening.
    bombAction = display.bomb ? getBombAction(currentTick) : null;

    if (display.util) {
        drawGrenadeTails(currentTick);
        drawUtility(currentTick);
        drawHEExplosions(currentTick);
    }
    drawBombOnMap(currentTick);
    drawGroundItems(currentTick);
    drawSoundEvents(currentTick);   // gunfire / detonations / bomb - under the players
    if (display.trails) drawPlayerTrails(currentTick);
    drawPlayers(players, currentTick);
    drawFlashOverlay(currentTick);   // localized flash blooms at map positions - world space
    // Back to screen space for the HUD (scoreboard + in-canvas kill feed).
    Viewport.clear(ctx);
    if (window.KillFeed)
        withHudScale(canvas.width, 0, () =>          // pinned to the top-right corner
            KillFeed.draw(ctx, { kills: allKills, tick: currentTick, canvasW: canvas.width, weaponLabel: formatWeaponFeed }));

    // Group by FIXED team identity so each panel stays on its side across the
    // halftime swap. team1 → left panel, team2 → right; the panel colour follows
    // the team's CURRENT side (so it matches the map dots) and the title shows it.
    const byName = (a, b) => a.name.localeCompare(b.name);
    const roundForTeams = getRoundNum(currentTick) ?? 1;
    const swapped   = isSwappedAtRound(roundForTeams);
    const team1Side = swapped ? 't' : 'ct';           // team1's current side
    const CT = '#4a9eff', T = '#ffaa22';
    const team1Color = team1Side === 'ct' ? CT : T;
    const team2Color = team1Side === 'ct' ? T : CT;
    const team1 = players.filter(p => teamOf(p.side, roundForTeams) === 'team1').sort(byName);
    const team2 = players.filter(p => teamOf(p.side, roundForTeams) === 'team2').sort(byName);
    const stats = computeStats(currentTick);
    updateSidebar(team1List, team1, team1Color, stats, team1MoneyEl);
    updateSidebar(team2List, team2, team2Color, stats, team2MoneyEl);
    if (team1NameEl) team1NameEl.textContent = 'TEAM 1 · ' + team1Side.toUpperCase();
    if (team2NameEl) team2NameEl.textContent = 'TEAM 2 · ' + (team1Side === 'ct' ? 'T' : 'CT');
    withHudScale(canvas.width / 2, 0, () => drawScoreboard(currentTick));   // pinned to top-centre
    updateActiveRoundBtn(currentTick);
}

// ─── Bomb ─────────────────────────────────────────────────────────────────
// The plant/defuse in progress on the current frame, or null. Set by render().
let bombAction = null;

function drawBombOnMap(currentTick) {
    const state = getBombState(currentTick);
    if (!state || !state.X || !state.Y) return;
    if (state.event !== 'plant' && state.event !== 'drop') return;
    const pos = worldToCanvas(state.X, state.Y);
    if (!isOnCanvas(pos)) return;
    const planted = state.event === 'plant';
    // A defuse in progress makes the site marker beat faster - the same cue
    // the ring gives on the player, for when the camera is not on them.
    const beingDefused = !!(bombAction && bombAction.kind === 'defuse');
    const pulse   = 1 + 0.2 * Math.sin(Date.now() / (beingDefused ? 110 : 300));
    ctx.beginPath();
    ctx.arc(pos.x, pos.y, (planted ? 14 : 8) * pulse, 0, Math.PI * 2);
    ctx.fillStyle = planted ? 'rgba(255,50,50,0.25)' : 'rgba(255,170,0,0.2)';
    ctx.fill();
    ctx.beginPath();
    ctx.arc(pos.x, pos.y, planted ? 7 : 5, 0, Math.PI * 2);
    ctx.fillStyle = planted ? '#ff3333' : '#ffaa22';
    ctx.fill();
    ctx.strokeStyle = 'white'; ctx.lineWidth = 1.5; ctx.stroke();
    ctx.fillStyle   = 'white';
    ctx.font        = 'bold 10px ' + FmtLogic.FONT_NARROW;   // player name
    ctx.shadowColor = 'rgba(0,0,0,0.95)'; ctx.shadowBlur = 4;
    ctx.textAlign   = 'center';
    ctx.fillText(planted ? 'C4' : 'C4', pos.x, pos.y - 11);
    ctx.textAlign = 'left'; ctx.shadowBlur = 0;
}

// ─── Dropped weapons / utility on the ground ───────────────────────────────
function drawGroundItems(currentTick) {
    if (!display.drops || !allItems.length) return;
    const roundStart = currentRoundStart(currentTick);
    const items = GroundItemsLogic.resolveGroundItems(allItems, currentTick, roundStart);
    GroundItems.draw(ctx, { items, worldToCanvas, isOnCanvas, labelFn: formatWeaponLabel });
}

// ─── Grenade Trails ───────────────────────────────────────────────────────
const GRENADE_COLORS = {
    Flashbang:    '#ffffaa', HEGrenade:   '#ff6633',
    SmokeGrenade: '#aaccaa', Molotov:     '#ff8800',
    Incendiary:   '#ff8800', Decoy:       '#aaaaff',
};

function drawGrenadeTails(currentTick) {
    if (!window.grenadeTrails) return;
    for (const trail of window.grenadeTrails.values()) {
        const points = trail.filter(
            p => p.tick <= currentTick && p.tick >= currentTick - grenadeLookback
        );
        if (points.length < 2) continue;
        const color = GRENADE_COLORS[points[0].type] || '#ffffff';
        ctx.save();
        ctx.lineJoin = 'round'; ctx.lineCap = 'round';
        for (let i = 1; i < points.length; i++) {
            const p0 = worldToCanvas(points[i-1].X, points[i-1].Y);
            const p1 = worldToCanvas(points[i].X,   points[i].Y);
            if (!isOnCanvas(p0) && !isOnCanvas(p1)) continue;
            const alpha = Math.max(0, 1 - (currentTick - points[i].tick) / grenadeLookback);
            ctx.beginPath(); ctx.moveTo(p0.x, p0.y); ctx.lineTo(p1.x, p1.y);
            ctx.strokeStyle = color; ctx.globalAlpha = alpha * 0.85;
            ctx.lineWidth = 2; ctx.stroke();
        }
        const head = points[points.length - 1];
        const hpos = worldToCanvas(head.X, head.Y);
        if (isOnCanvas(hpos)) {
            ctx.globalAlpha = 1;
            ctx.beginPath(); ctx.arc(hpos.x, hpos.y, 4, 0, Math.PI * 2);
            ctx.fillStyle = color; ctx.fill();
            ctx.strokeStyle = 'rgba(255,255,255,0.6)'; ctx.lineWidth = 1; ctx.stroke();
        }
        ctx.globalAlpha = 1; ctx.restore();
    }
}

// ─── Utility ──────────────────────────────────────────────────────────────
// Radii are real world units scaled by the map calibration (previously
// hardcoded canvas px): smokes render at the CS2 volumetric radius with HE
// detonations carving temporary holes, fires spread outward over ~2 s with
// the T molotov footprint larger than the CT incendiary (utilfx.logic.js).
/**
 * Smokes, molotovs and their interactions, at true in-game radii.
 * @param {number} currentTick
 * Geometry and lifetimes come from UtilFXLogic - display duration is clamped
 * to the real game values, never to the parser's recorded end_tick.
 */
function drawUtility(currentTick) {
    for (const smoke of allSmokes) {
        // end_tick was already clamped to SMOKE_LIFETIME_TICKS at load time.
        const start = smoke.start_tick, end = smoke.end_tick;
        if (currentTick < start || currentTick > end) continue;
        const pos = worldToCanvas(smoke.X, smoke.Y);
        if (!isOnCanvas(pos)) continue;
        const rPx = UtilFXLogic.SMOKE_RADIUS / mapConfig.scale;
        const bloom = UtilFXLogic.smokeBloom(currentTick - start);
        const fade  = end - currentTick < 128 ? (end - currentTick) / 128 : 1;
        // volumetric interaction: HE detonations inside the smoke carve holes
        const holes = UtilFXLogic.smokeHoles(smoke, allHE, currentTick).map(h => {
            const hp = worldToCanvas(h.x, h.y);
            return { x: hp.x, y: hp.y, r: h.r / mapConfig.scale, strength: h.strength };
        });
        UtilFX.drawSmokeCloud(ctx, {
            x: pos.x, y: pos.y, rPx, tick: currentTick,
            seed: (start * 31 + Math.round(smoke.X)) | 0,
            bloom, fade, holes, label: true,
        });
    }
    for (let i = 0; i < allInfernos.length; i++) {
        const inf = allInfernos[i];
        // end_tick was already clamped to FIRE_LIFETIME_TICKS at load time.
        const start = inf.start_tick, end = inf.end_tick;
        if (currentTick < start || currentTick > end) continue;
        const pos = worldToCanvas(inf.X, inf.Y);
        if (!isOnCanvas(pos)) continue;
        const weapon = infernoWeapons[i] || 'Molotov';
        const maxR = UtilFXLogic.infernoMaxRadius(weapon);
        const rPx = UtilFXLogic.fireSpreadRadius(currentTick - start, maxR) / mapConfig.scale;
        const fade = end - currentTick < 96 ? (end - currentTick) / 96 : 1;
        // parser's additive `cells` ignition timeline (new parses) → true
        // spread geometry; absent on old data → radial approximation.
        let cells = null;
        if (inf.cells && inf.cells.length) {
            cells = [];
            for (const c of inf.cells) {
                if (c.tick > currentTick) break;   // cells are ignition-ordered
                const cp = worldToCanvas(c.X, c.Y);
                cells.push({ x: cp.x, y: cp.y });
            }
        }
        UtilFX.drawFire(ctx, {
            x: pos.x, y: pos.y, rPx, tick: currentTick,
            seed: (start * 17 + Math.round(inf.X)) | 0,
            weapon, alpha: fade, cells,
        });
    }
}

// ─── HE Explosions ────────────────────────────────────────────────────────
function drawHEExplosions(currentTick) {
    for (const he of allHE) {
        const age = currentTick - he.tick;
        if (age < 0 || age > HE_FADE_TICKS) continue;
        const pos = worldToCanvas(he.X, he.Y);
        if (!isOnCanvas(pos)) continue;
        const t = age / HE_FADE_TICKS;
        const alpha = easeOut(1 - t);
        const r     = 10 + easeOut(t) * 38;
        ctx.beginPath(); ctx.arc(pos.x, pos.y, r, 0, Math.PI * 2);
        ctx.strokeStyle = `rgba(255,180,60,${alpha * 0.9})`; ctx.lineWidth = 3; ctx.stroke();
        ctx.beginPath(); ctx.arc(pos.x, pos.y, r * 0.6, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(255,230,100,${alpha * 0.4})`; ctx.fill();
        if (age < 20) {
            ctx.fillStyle = `rgba(255,220,100,${alpha})`;
            ctx.font = 'bold 10px ' + FmtLogic.FONT_MONO;   // damage number
            ctx.textAlign = 'center';
            ctx.fillText('HE', pos.x, pos.y - r - 4);
            ctx.textAlign = 'left';
        }
    }
}

// Average a player's speed over a short backward-looking window ending at
// currentIdx, to smooth out per-tick velocity noise (footstep circle would
// otherwise visibly jitter frame to frame). Pure function of currentIdx, so
// it stays rewind-safe like drawPlayerTrails' own backward scan below.
/**
 * Player speed in units/s, averaged over nearby frames.
 * @param {string} name
 * @param {number} atIdx  index into sortedTicks
 * @returns {number} units/s; derived from position deltas because
 *   demoinfocs v5 exposes no velocity on Player.
 */
function smoothedSpeed(name, atIdx) {
    const start = Math.max(0, atIdx - FOOTSTEP_SMOOTH_WINDOW);
    let sum = 0, n = 0;
    for (let i = start; i <= atIdx; i++) {
        const rec = (matchData[sortedTicks[i]] || []).find(p => p.name === name);
        if (!rec || rec.speed == null || rec.health <= 0) continue;
        sum += rec.speed; n++;
    }
    return n ? sum / n : null;
}

// ─── Player trails ───────────────────────────────────────────────────────────
// Short movement trail per living player, built from the loaded tick window
// (matchData is keyed by tick). Gated behind the "Trails" display toggle.
function drawPlayerTrails(currentTick) {
    const start = Math.max(0, currentIdx - PLAYER_TRAIL_TICKS);
    ctx.save();
    ctx.lineWidth = 2; ctx.lineJoin = 'round'; ctx.lineCap = 'round';
    for (const player of matchData[sortedTicks[currentIdx]] || []) {
        if (hiddenPlayers.has(player.name)) continue;
        const col = player.side === 'ct' ? '#4a9eff' : '#ffaa22';
        let prev = null, drew = false;
        for (let i = start; i <= currentIdx; i++) {
            const rec = (matchData[sortedTicks[i]] || []).find(p => p.name === player.name);
            if (!rec || rec.health <= 0) { prev = null; continue; }
            const pt = worldToCanvas(rec.X, rec.Y);
            if (prev) {
                if (!drew) { ctx.beginPath(); ctx.moveTo(prev.x, prev.y); drew = true; }
                ctx.lineTo(pt.x, pt.y);
            }
            prev = pt;
        }
        if (drew) { ctx.strokeStyle = col; ctx.globalAlpha = 0.5; ctx.stroke(); }
    }
    ctx.globalAlpha = 1; ctx.restore();
}

// ─── Players ──────────────────────────────────────────────────────────────
/**
 * Draw every visible player for this frame.
 * @param {Array<Object>} players  tick records: {name, X, Y, Z, health, ...}
 * @param {number} currentTick
 * Skips hidden players and anything off-canvas, applies the multi-level fade,
 * then defers each marker to drawPlayerMarker().
 */
function drawPlayers(players, currentTick) {
    const firingPlayers = new Set();
    for (let i = allShots.length - 1; i >= 0; i--) {
        const s = allShots[i];
        if (s.tick > currentTick) continue;
        if (currentTick - s.tick > SPRAY_DURATION_TICKS) break;
        firingPlayers.add(s.player_name);
    }
    players.forEach(player => {
        if (hiddenPlayers.has(player.name)) return;
        const pos = worldToCanvas(player.X, player.Y);
        if (!isOnCanvas(pos)) return;
        // Multi-level maps: a player standing on the floor that is NOT on show
        // is faded rather than hidden - they are alive and about to matter, so
        // hiding them would make the replay lie. Always 1 on a flat map.
        //
        // Applied with ctx.filter rather than ctx.globalAlpha: the marker below
        // sets globalAlpha itself (the lock ring's pulse, the death cross), and
        // globalAlpha does not compose, so a value set here would simply be
        // overwritten. filter multiplies into every draw made while it is set.
        const lAlpha = layerAlpha(player.Z);
        ctx.save();
        if (lAlpha < 1) ctx.filter = `opacity(${Math.round(lAlpha * 100)}%)`;
        drawPlayerMarker(player, pos, currentTick, firingPlayers);
        ctx.restore();
    });
}

// One player's marker - lock ring, death cross or dot + direction + label.
// Lifted out of drawPlayers' loop body unchanged so the multi-level fade above
// can wrap the whole marker in one filter.
/**
 * One player's dot, facing cone, name and state ring.
 * @param {Object} player          tick record
 * @param {{x: number, y: number}} pos  base-space canvas coords
 * @param {number} currentTick
 * @param {Set<string>} firingPlayers  names shooting on this tick
 *
 * Split out of drawPlayers so the caller can wrap it in a ctx.filter for the
 * off-floor fade: this sets globalAlpha itself (lock-ring pulse, death cross)
 * and globalAlpha does not compose.
 */
function drawPlayerMarker(player, pos, currentTick, firingPlayers) {
        // Planting / defusing: a sweeping progress arc around the actor.
        //
        // Geometry and colours come from BombAction so this and the
        // analyser's ring are literally the same drawing - they used to be
        // two hand-rolled copies at different sizes (19/3px here, 11/2.5px
        // there). The analyser's proportions won: at radius 11 the ring
        // clears the 7px player dot and still sits well inside the 16px lock
        // ring below, where the old 19 crowded it.
        //
        // BombAction.drawRing sets no globalAlpha, because this function owns
        // it (lock-ring pulse, death cross) and the multi-level floor fade has
        // to use ctx.filter for exactly that reason - see CLAUDE.md.
        if (bombAction && bombAction.player === player.name && player.health > 0) {
            BombAction.drawRing(ctx, {
                x: pos.x, y: pos.y,
                kind: bombAction.kind, progress: bombAction.progress,
            });
        }
        // Locked (follow/POV) player gets a pulsing ring so it reads at a glance.
        if (player.name === lockedName && player.health > 0) {
            ctx.save();
            ctx.strokeStyle = '#ffd35c';
            ctx.globalAlpha = 0.6 + 0.4 * Math.sin(currentTick * 0.15);
            ctx.lineWidth = 2;
            ctx.beginPath(); ctx.arc(pos.x, pos.y, 16, 0, Math.PI * 2); ctx.stroke();
            ctx.restore();
        }
        // Dead players: draw a small × death marker where they fell (matches the
        // multi-round overlay) instead of hiding them.
        if (player.health <= 0) {
            const teamCol = player.side === 'ct' ? '#4a9eff' : '#ffaa22';
            ctx.save();
            ctx.strokeStyle = teamCol;
            ctx.globalAlpha = 0.8;
            ctx.lineWidth = 2;
            ctx.beginPath();
            ctx.moveTo(pos.x - 4, pos.y - 4); ctx.lineTo(pos.x + 4, pos.y + 4);
            ctx.moveTo(pos.x + 4, pos.y - 4); ctx.lineTo(pos.x - 4, pos.y + 4);
            ctx.stroke();
            ctx.restore();
            ctx.globalAlpha = 1;
            return;
        }
        const isCT      = player.side === 'ct';
        const baseColor = isCT ? '#4a9eff' : '#ffaa22';
        let intensity   = 0;
        if (player.flash_duration > 0)
            intensity = Math.pow(Math.min(player.flash_duration / 2.5, 1.0), 0.8);
        let activeColor = baseColor;
        if (intensity > 0) {
            const r = isCT ? 74 : 255, g = isCT ? 158 : 170, b = isCT ? 255 : 34;
            activeColor = `rgb(${Math.round(r+(255-r)*intensity)},${Math.round(g+(255-g)*intensity)},${Math.round(b+(255-b)*intensity)})`;
        }
        // Movement audibility: a circle at the real hearing range (world units
        // / mapConfig.scale, same convention as the utility radii) whenever
        // the player's smoothed ground speed is near/above CS2's
        // silent-movement cutoff. Radius and fade come from the shared
        // audibility model (static/sound.logic.js); the non-movement sounds
        // that model also covers - gunfire, detonations, the bomb - are drawn
        // once per frame in drawSoundEvents(), not per player.
        const mv = display.sound ? SoundLogic.movement(smoothedSpeed(player.name, currentIdx)) : null;
        if (mv) drawSoundRing(pos.x, pos.y, mv.radius, mv.kind, mv.alpha);
        if (firingPlayers.has(player.name)) drawSpray(pos, activeColor, player.yaw);
        ctx.beginPath();
        ctx.arc(pos.x, pos.y, 13 + intensity * 5, 0, Math.PI * 2);
        ctx.fillStyle = intensity > 0
            ? `rgba(255,255,255,${intensity * 0.35})`
            : (isCT ? 'rgba(74,158,255,0.15)' : 'rgba(255,170,34,0.15)');
        ctx.fill();
        ctx.beginPath(); ctx.arc(pos.x, pos.y, 7, 0, Math.PI * 2);
        ctx.fillStyle   = activeColor; ctx.fill();
        ctx.strokeStyle = intensity > 0.4 ? 'white' : 'rgba(255,255,255,0.85)';
        ctx.lineWidth   = 1.5 + intensity * 1.5; ctx.stroke();
        // ── Direction triangle ──────────────────────────────────────────────
        if (player.yaw != null) {
            const yawRad   = (-player.yaw) * (Math.PI / 180);
            const circleR  = 7;          // matches the arc radius above
            const tipDist  = circleR + 5; // tip of triangle beyond circle edge
            const baseHalf = 3.5;        // half-width of triangle base on circle edge
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
            ctx.fillStyle   = activeColor;
            ctx.strokeStyle = intensity > 0.4 ? 'white' : 'rgba(255,255,255,0.85)';
            ctx.lineWidth   = 1;
            ctx.fill();
            ctx.stroke();
            ctx.restore();
        }
        // ────────────────────────────────────────────────────────────────────
        ctx.fillStyle   = 'white';
        ctx.font        = 'bold 11px ' + FmtLogic.FONT_NARROW;   // player name
        ctx.shadowColor = 'rgba(0,0,0,0.95)'; ctx.shadowBlur = 4;
        ctx.fillText(player.name, pos.x + 10, pos.y + 4);
        ctx.shadowBlur  = 0;
}

function drawSpray(pos, color, yaw) { Match2D.drawSpray(ctx, pos, color, yaw); }

// ─── Flash Overlay ────────────────────────────────────────────────────────
function drawFlashOverlay(currentTick) {
    const MAX_VISUAL_RADIUS = 50;
    const FLASH_FADE_TICKS  = TICKRATE * 1.5;
    for (const f of allFlashes) {
        const age = currentTick - f.tick;
        if (age < 0 || age > FLASH_FADE_TICKS) continue;
        const pos           = worldToCanvas(f.X, f.Y);
        if (!isOnCanvas(pos)) continue;
        const currentRadius = MAX_VISUAL_RADIUS * (0.4 + 0.6 * easeOut(Math.min(age / 10, 1)));
        const baseAlpha     = easeOut(1 - age / FLASH_FADE_TICKS);
        const alpha         = baseAlpha * (0.85 + Math.sin(age * 0.04) * 0.15);
        const grad = ctx.createRadialGradient(pos.x, pos.y, 0, pos.x, pos.y, currentRadius);
        grad.addColorStop(0,   `rgba(255,255,255,${alpha})`);
        grad.addColorStop(0.5, `rgba(235,245,255,${alpha * 0.5})`);
        grad.addColorStop(1,   `rgba(200,230,255,0)`);
        ctx.beginPath(); ctx.arc(pos.x, pos.y, currentRadius, 0, Math.PI * 2);
        ctx.fillStyle = grad; ctx.fill();
        if (age < 20) {
            ctx.beginPath(); ctx.arc(pos.x, pos.y, currentRadius * 0.2, 0, Math.PI * 2);
            ctx.fillStyle = `rgba(255,255,255,${(1 - age / 20) * 0.9})`;
            ctx.shadowBlur = 15; ctx.shadowColor = 'white'; ctx.fill(); ctx.shadowBlur = 0;
        }
        if (age < 45) {
            ctx.globalAlpha = baseAlpha;
            ctx.fillStyle   = 'white';
            ctx.font        = 'bold 10px ' + FmtLogic.FONT_MONO;   // damage number
            ctx.textAlign   = 'center';
            ctx.fillText('FLASH', pos.x, pos.y - currentRadius - 8);
            ctx.globalAlpha = 1; ctx.textAlign = 'left';
        }
    }
}

// ─── Sidebar ──────────────────────────────────────────────────────────────
// Buy-strength thresholds: green = full buy money, yellow = force, red = eco.
function moneyClass(m) {
    if (m >= 4000) return 'money-full';
    if (m >= 1500) return 'money-force';
    return 'money-eco';
}
const formatMoney = FmtLogic.money;

// Damage-flash timing on the health bar. Long enough to notice mid-playback,
// short enough that consecutive hits in one spray don't stack into a smear.
//
// The 700ms version was a flick rather than a fade: at 1x the ghost had come
// and gone before the eye finished moving to the row that changed, which reads
// as a flicker instead of as damage. It runs over 1.6s now, and the ghost
// starts partly transparent so the effect is a wash rather than a red bar
// briefly replacing the health bar.
const HP_FLASH_MS = 1600;
// The ghost holds its tint, then drops away at the end (ease-in), instead of
// starting to disappear the instant it appears (ease-out). The shrink keeps
// ease-out - the travel should be quick and the landing gentle.
const HP_FLASH_SHRINK_EASE = 'cubic-bezier(.22,.61,.36,1)';
const HP_FLASH_FADE_EASE = 'ease-in';
// Peak opacity of the ghost. Below 1 so the bar underneath still reads through
// it and the flash never looks like a state change.
const HP_FLASH_ALPHA = 0.62;

/**
 * Write one team's sidebar, updating rows IN PLACE.
 *
 * @param {HTMLElement} container  team list element; owns `_rosterKey`/`_rows`
 * @param {Array<Object>} players  this frame's tick records for the team
 * @param {string} color           team colour, for the dot and name
 * @param {Object} stats           name -> {kills, deaths, assists}
 * @param {HTMLElement} moneyEl    team-total money element
 *
 * Rows are rebuilt ONLY when the roster changes (new round, disconnect);
 * otherwise every field is written onto the existing nodes through the
 * write-only-if-changed helpers. Do not replace this with an innerHTML
 * rebuild - this runs up to 60×/s, and destroying rows breaks two things:
 * a click needs its mousedown and mouseup on the same element (follow-lock
 * stops working during playback), and nothing can animate across frames
 * (the damage flash below depends on it).
 */
function updateSidebar(container, players, color, stats, moneyEl) {
    const key = players.map(p => p.name).join('\u0000');
    if (container._rosterKey !== key) {
        container._rosterKey = key;
        container._rows = new Map();
        container.innerHTML = '';
        players.forEach(p => {
            const row = document.createElement('div');
            row.className = 'player-row';
            row.dataset.name = p.name;
            row.title = 'Click to lock/follow \u00b7 eye = show/hide on map';
            // Three stacked rows: (1) player name, (2) K/D/A + health bar +
            // money, (3) the full-width loadout - the equip tags need more
            // width than a shared column allows.
            row.innerHTML = `
            <div class="player-line1">
                <div class="player-dot"></div>
                <span class="player-name"></span>
                <span class="player-vis" title="Show/hide on map"></span>
            </div>
            <div class="player-line2">
                <span class="player-kda"><span class="kda-k"></span><span class="kda-sep">/</span><span class="kda-d"></span><span class="kda-sep">/</span><span class="kda-a"></span></span>
                <div class="player-hp"></div>
                <div class="hp-bar-wrap">
                    <div class="hp-bar-ghost"></div>
                    <div class="hp-bar"></div>
                    <div class="hp-mark"></div>
                </div>
                <span class="player-money"></span>
            </div>
            <div class="player-equip">
                <span class="equip-tags"></span>
            </div>`;
            container.appendChild(row);
            container._rows.set(p.name, {
                row,
                dot:   row.querySelector('.player-dot'),
                name:  row.querySelector('.player-name'),
                vis:   row.querySelector('.player-vis'),
                k:     row.querySelector('.kda-k'),
                d:     row.querySelector('.kda-d'),
                a:     row.querySelector('.kda-a'),
                hp:    row.querySelector('.player-hp'),
                bar:   row.querySelector('.hp-bar'),
                ghost: row.querySelector('.hp-bar-ghost'),
                mark:  row.querySelector('.hp-mark'),
                money: row.querySelector('.player-money'),
                equip: row.querySelector('.equip-tags'),
                lastHp: null,
                flashUntil: 0,
            });
        });
    }

    const now = performance.now();
    let teamMoney = 0, hasMoney = false;
    players.forEach(p => {
        const r = container._rows.get(p.name);
        if (!r) return;
        const hp = Math.max(0, p.health);
        const dead = hp <= 0;
        const st = stats[p.name] || { kills: 0, deaths: 0, assists: 0 };
        if (p.money != null) { teamMoney += p.money; hasMoney = true; }

        setClass(r.row, 'locked', p.name === lockedName);
        setClass(r.row, 'hidden-player', hiddenPlayers.has(p.name));
        setStyle(r.dot, 'background', dead ? '#333' : color);
        setText(r.name, p.name);
        setStyle(r.name, 'color', dead ? '#445' : 'inherit');
        setText(r.vis, hiddenPlayers.has(p.name) ? '\ud83d\udeab' : '\ud83d\udc41');
        setStyle(r.k, 'color', color);
        setText(r.k, st.kills); setText(r.d, st.deaths); setText(r.a, st.assists);
        setText(r.hp, hp);
        setClass(r.hp, 'dead', dead);
        setStyle(r.bar, 'width', hp + '%');
        setStyle(r.bar, 'background', color);

        // ── Damage flash ───────────────────────────────────────────────────
        // On a drop in health: the ghost is planted at the OLD health with no
        // transition, a marker line is drawn at the NEW health, and then the
        // ghost is released to shrink down to the marker. So the eye sees how
        // much was taken and where it landed, then both disappear.
        //
        // Health going UP (a seek backwards, a new round, a medishot) is not
        // damage and is applied silently.
        if (r.lastHp != null && hp < r.lastHp) {
            r.ghost.style.transition = 'none';
            r.ghost.style.width = r.lastHp + '%';
            r.ghost.style.opacity = String(HP_FLASH_ALPHA);
            r.mark.style.left = hp + '%';
            r.mark.style.opacity = String(HP_FLASH_ALPHA);
            // Force a reflow so the browser sees the from-value before the
            // to-value; without it both writes coalesce and nothing animates.
            void r.ghost.offsetWidth;
            r.ghost.style.transition = `width ${HP_FLASH_MS}ms ${HP_FLASH_SHRINK_EASE},`
                + ` opacity ${HP_FLASH_MS}ms ${HP_FLASH_FADE_EASE}`;
            r.ghost.style.width = hp + '%';
            r.ghost.style.opacity = '0';
            r.mark.style.transition = `opacity ${HP_FLASH_MS}ms ${HP_FLASH_FADE_EASE}`;
            r.mark.style.opacity = '0';
            r.flashUntil = now + HP_FLASH_MS;
        } else if (r.lastHp != null && hp > r.lastHp && now > r.flashUntil) {
            // Cancel a stale flash rather than letting it finish over a bar
            // that has since jumped elsewhere.
            r.ghost.style.transition = 'none';
            r.ghost.style.opacity = '0';
            r.mark.style.opacity = '0';
        }
        r.lastHp = hp;

        setText(r.money, p.money != null ? formatMoney(p.money) : '');
        // Money survives death (it matters for the next buy), so it sits on the
        // stats row - not on the loadout row that gets dimmed for dead players.
        setAttr(r.money, 'class', 'player-money' + (p.money != null ? ' ' + moneyClass(p.money) : ''));

        setClass(r.equip, 'dead', dead);
        setHtml(r.equip, buildEquipTags(p));
    });
    if (moneyEl) moneyEl.innerText = hasMoney ? formatMoney(teamMoney) : '';
}

// Write-only-if-changed helpers. At 60fps the sidebar is otherwise re-laying
// out text that has not moved; more importantly, rewriting innerHTML that is
// already correct would restart the equipment icons' decode every frame.
function setText(el, v) { const t = String(v); if (el.textContent !== t) el.textContent = t; }
function setHtml(el, v) { if (el._html !== v) { el._html = v; el.innerHTML = v; } }
function setStyle(el, prop, v) { if (el.style[prop] !== v) el.style[prop] = v; }
function setClass(el, cls, on) { if (el.classList.contains(cls) !== !!on) el.classList.toggle(cls, !!on); }
function setAttr(el, name, v) { if (el.getAttribute(name) !== v) el.setAttribute(name, v); }

// Render equipment as FIXED slots so each weapon class always sits in the same
// place on every row and never reshuffles as the demo plays. Empty slots get a
// dim placeholder. The weapon the player is actively holding at the current tick
// gets a subtle glow (the `active` class → box-shadow in CSS).
function buildEquipTags(player) {
    const activeRaw = (player.active_weapon || '').toLowerCase();
    const inv = new Set((Array.isArray(player.inventory) ? player.inventory : [])
        .map(w => (w || '').toLowerCase()));
    if (activeRaw) inv.add(activeRaw);

    // Real equipment icon (static/assets/equipment/) when present, else the text
    // label - the fixed-slot layout is identical either way (§1 assets, §2).
    const equipIcon = (key) => (key && window.Assets && Assets.available()) ? Assets.url('equipment', key) : null;
    const slot = (cls, label, active, empty, title, iconKey) => {
        let inner = label;
        if (!empty) {
            const u = equipIcon(iconKey);
            if (u) inner = `<img class="equip-ico" src="${u}" alt="${label}">`;
        }
        return `<span class="equip-tag ${cls}${active ? ' active' : ''}${empty ? ' slot-empty' : ''}"${title ? ` title="${title}"` : ''}>${inner}</span>`;
    };

    // First inventory item matching any of the given classes (fixed scan order).
    const findFirst = (...classes) => {
        for (const item of inv) {
            if (classes.includes(classifyWeapon(item))) return item;
        }
        return null;
    };

    const tags = [];

    // Slot 1 - primary (rifle / sniper / heavy / smg)
    const primary = findFirst('rifle', 'sniper', 'heavy', 'smg');
    tags.push(primary
        ? slot(`${classifyWeapon(primary)} slot-primary`, formatWeaponLabel(primary), primary === activeRaw, false, formatWeaponFeed(primary), primary)
        : slot('slot-primary slot-empty', '-', false, true));

    // Slot 2 - pistol
    const pistol = findFirst('pistol');
    tags.push(pistol
        ? slot('pistol slot-pistol', formatWeaponLabel(pistol), pistol === activeRaw, false, formatWeaponFeed(pistol), pistol)
        : slot('slot-pistol slot-empty', '-', false, true));

    // Slot 3 - armor (kevlar / kevlar+helmet). Old chunks lack the field;
    // armor stays undefined there and the slot renders as an empty placeholder.
    const hasArmor = (player.armor ?? 0) > 0;
    tags.push(hasArmor
        ? slot('armor slot-armor', player.helmet ? 'K+H' : 'KEV', false, false, player.helmet ? 'Kevlar + Helmet' : 'Kevlar', player.helmet ? 'armor_helmet' : 'kevlar')
        : slot('slot-armor slot-empty', '-', false, true));

    // Slots 4-7 - grenades (always shown; dim placeholder when not held).
    // The first nade slot carries `nade-first` to open a visual gap between
    // the gun/armor group and the utility group.
    const NADE_SLOTS = [
        { keys: ['weapon_smokegrenade'],                 label: 'SMK', title: 'Smoke' },
        { keys: ['weapon_flashbang'],                    label: 'FL',  title: 'Flashbang' },
        { keys: ['weapon_hegrenade'],                    label: 'HE',  title: 'HE Grenade' },
        { keys: ['weapon_molotov', 'weapon_incgrenade'], label: 'MOL', title: 'Molotov / Incendiary' },
    ];
    NADE_SLOTS.forEach((s, i) => {
        const held   = s.keys.find(k => inv.has(k)) || null;
        const active = held != null && held === activeRaw;
        tags.push(slot(`util slot-nade${i === 0 ? ' nade-first' : ''}`, s.label, active, held == null, s.title, held || s.keys[0]));
    });

    // One reserved trailing "carry" slot so EVERY row is the same width (the box
    // never resizes as loadouts change): defuse kit (CT) or C4 (T) or an empty
    // placeholder. A player can hold at most one of the two, so a single slot
    // covers both.
    if (player.defuse_kit)   tags.push(slot('kit slot-carry', 'KIT', false, false, 'Defuse Kit', 'defuser'));
    else if (player.has_c4)  tags.push(slot('bomb slot-carry', 'C4', activeRaw === 'weapon_c4', false, 'C4', 'weapon_c4'));
    else                     tags.push(slot('slot-carry slot-empty', '', false, true));

    return tags.join('');
}

// ─── Player Stats (K/D/A) ─────────────────────────────────────────────────
function computeStats(currentTick) {
    const stats = {}; // name -> {kills, deaths, assists}
    const ensure = name => { if (name && !stats[name]) stats[name] = {kills:0, deaths:0, assists:0}; };
    for (const k of allKills) {
        if (k.tick > currentTick) break;
        ensure(k.attacker_name);  if (k.attacker_name) stats[k.attacker_name].kills++;
        ensure(k.victim_name);    if (k.victim_name)   stats[k.victim_name].deaths++;
        ensure(k.assister_name);  if (k.assister_name) stats[k.assister_name].assists++;
    }
    return stats;
}

// The kill feed is drawn on the canvas itself (top-right) by
// static/killfeed.js - see the KillFeed.draw() call in render(), which runs in
// screen space after Viewport.clear() so the feed does not pan or zoom with
// the map.

// ─── Playback ─────────────────────────────────────────────────────────────
function animate(timestamp) {
    if (!isPlaying) return;
    // Pace by the real tick distance to the next stored snapshot so playback is
    // real-time regardless of snapshot rate (JSON chunks: 64 Hz; v2: 32 Hz).
    // Clamp the delta so gaps between rounds/chunks skip in ≤0.25 s instead of
    // stalling for the gap's real duration.
    let dtTicks = 1;
    if (currentIdx < sortedTicks.length - 1) {
        dtTicks = Number(sortedTicks[currentIdx + 1]) - Number(sortedTicks[currentIdx]);
        if (!(dtTicks >= 1)) dtTicks = 1;
        if (dtTicks > TICKRATE / 4) dtTicks = TICKRATE / 4;
    }
    const interval = (dtTicks / TICKRATE) * 1000 / speedMult;
    if (timestamp - lastFrameTime >= interval) {
        lastFrameTime = timestamp;
        if (currentIdx < sortedTicks.length - 1) {
            currentIdx++;
            if (chunkIndex.length > 0 && sortedTicks.length - 1 - currentIdx < 64) {
                const nextTick = Number(sortedTicks[currentIdx]);
                ensureChunksForTick(nextTick).then(changed => {
                    if (changed) {
                        const idx = sortedTicks.indexOf(String(nextTick));
                        if (idx !== -1) currentIdx = idx;
                    }
                });
            }
            render();
        } else {
            isPlaying = false;
            document.getElementById('btnPlay').classList.remove('active');
        }
    }
    requestAnimationFrame(animate);
}

// ─── Pan / zoom + follow lock ────────────────────────────────────────────────
// Wheel = zoom at cursor, drag = pan, double-click = reset. While paused the
// render loop is idle, so re-render on change; while playing the next frame
// picks it up anyway. Clamped so the map can't be zoomed out past fit or panned
// off-frame (viewport.logic.js).
panZoom = Viewport.bindPanZoom(canvas, view, () => {
    // The annotation overlay is a separate canvas that does NOT repaint with
    // playback, so it has to be told when the view moves under it.
    if (telestrator) telestrator.redraw();
    if (!isPlaying) render();
});

// Follow/POV lock: keep the locked player centred each frame (zoom still works).
function applyLockFollow() {
    if (!lockedName) return;
    const recs = matchData && matchData[sortedTicks[currentIdx]];
    const lp = recs && recs.find(p => p.name === lockedName);
    if (!lp) return;
    const b = worldToCanvas(lp.X, lp.Y);
    view.tx = canvas.width / 2 - b.x * view.scale;
    view.ty = canvas.height / 2 - b.y * view.scale;
    ViewportLogic.clampPan(view, canvas.width, canvas.height);
}

function setLock(name) {
    lockedName = (name && name !== lockedName) ? name : null;
    if (lockedName && view.scale < 2.5) view.scale = 3;   // zoom in on first lock
    if (!lockedName) ViewportLogic.reset(view);           // release → back to full map
    document.body.classList.toggle('locked', !!lockedName);
    buildTimelineSegments();                              // refresh per-player highlights
    render();
}

// Click (not drag) on a player dot toggles the lock on that player.
//
// The hit test runs against the frame that was on screen when the button went
// DOWN, not the one showing when the click resolves. During playback those are
// different frames and the dot has moved several pixels between them, so
// clicking a moving player mostly missed - which is why locking appeared to
// work only while paused.
let pressIdx = null;
canvas.addEventListener('pointerdown', (ev) => {
    if (ev.button === 0) pressIdx = currentIdx;
});
canvas.addEventListener('click', (ev) => {
    if (panZoom && panZoom.didDrag()) return;             // ignore the click ending a pan
    const idx = pressIdx != null ? pressIdx : currentIdx;
    pressIdx = null;
    const recs = matchData && matchData[sortedTicks[idx]];
    if (!recs) return;
    const p = Viewport.eventToCanvas(canvas, ev);
    const base = ViewportLogic.screenToBase(view, p.x, p.y);
    let best = null, bestD = 18 / view.scale;             // ~18 screen px hit radius
    for (const rec of recs) {
        if (rec.health <= 0) continue;
        const b = worldToCanvas(rec.X, rec.Y);
        const d = Math.hypot(b.x - base.x, b.y - base.y);
        if (d < bestD) { bestD = d; best = rec.name; }
    }
    if (best) setLock(best);
    else if (lockedName) setLock(null);      // click empty space → unlock
});

// Sidebar: click a row to lock/follow; click its eye to opt the player in/out.
function toggleHidden(name) {
    if (!name) return;
    if (hiddenPlayers.has(name)) hiddenPlayers.delete(name); else hiddenPlayers.add(name);
    if (!isPlaying) render();
}
function onSidebarClick(e) {
    if (e.button !== undefined && e.button !== 0) return;
    const row = e.target.closest('.player-row');
    if (!row) return;
    if (e.target.closest('.player-vis')) toggleHidden(row.dataset.name);
    else setLock(row.dataset.name);
}
// pointerdown, not click. Rows are now updated in place rather than rebuilt
// (see updateSidebar), so `click` would work again - but pointerdown is also
// simply the right event for a control whose target is a moving target, and it
// keeps this correct if the rows ever go back to being re-created.
[team1List, team2List].forEach(c => c && c.addEventListener('pointerdown', onSidebarClick));

// ── Display controls: utility/trails toggles, utility-time, telestration ──────
(function bindDisplayControls() {
    const on = (id, ev, fn) => { const el = document.getElementById(id); if (el) el.addEventListener(ev, fn); };
    on('opt-util',      'change', (e) => { display.util      = e.target.checked; if (!isPlaying) render(); });
    on('opt-trails',    'change', (e) => { display.trails    = e.target.checked; if (!isPlaying) render(); });
    on('opt-sound',     'change', (e) => { display.sound     = e.target.checked; if (!isPlaying) render(); });
    on('opt-drops',     'change', (e) => { display.drops     = e.target.checked; if (!isPlaying) render(); });
    on('opt-bomb',      'change', (e) => { display.bomb      = e.target.checked; if (!isPlaying) render(); });
    // No re-render: this one only changes where a future round click lands.
    on('opt-freeze',    'change', (e) => { display.freeze    = e.target.checked; });
    const layerBox = document.getElementById('layer-box');
    if (layerBox) layerBox.addEventListener('click', (e) => {
        const btn = e.target.closest('[data-layer]');
        if (!btn) return;
        layerMode = btn.dataset.layer;
        // Manual choices take effect immediately; 'auto' re-resolves on the
        // next frame, which render() does for us.
        shownLayer = MapLayersLogic.resolve(mapName, layerMode,
            matchData ? (matchData[sortedTicks[currentIdx]] || []) : [], shownLayer);
        updateLayerButtons();
        render();
    });
    updateLayerButtons();
    on('util-time',  'input',  (e) => {
        grenadeLookback = +e.target.value;
        const lbl = document.getElementById('util-time-val');
        if (lbl) lbl.textContent = (grenadeLookback / TICKRATE).toFixed(1) + 's';
        if (!isPlaying) render();
    });
    // HUD size. The slider carries whole percents; hudScale is the multiplier.
    const hudScaleEl  = document.getElementById('hud-scale');
    const hudScaleLbl = document.getElementById('hud-scale-val');
    const paintHudScaleLabel = () => {
        if (hudScaleLbl) hudScaleLbl.textContent = Math.round(hudScale * 100) + '%';
    };
    // Reflect the persisted value into the control before wiring it, or a
    // remembered 140% would show a slider sitting at 100.
    if (hudScaleEl) hudScaleEl.value = Math.round(hudScale * 100);
    paintHudScaleLabel();
    on('hud-scale', 'input', (e) => {
        hudScale = (+e.target.value) / 100;
        paintHudScaleLabel();
        try { localStorage.setItem(HUD_SCALE_KEY, String(hudScale)); } catch (err) { /* storage blocked - the session still works */ }
        if (!isPlaying) render();
    });

    // Telestration overlay (analytical drawing). Tool buttons toggle the active
    // tool; when none is active the overlay is click-through so pan/zoom works.
    const drawEl = document.getElementById('drawCanvas');
    if (drawEl && window.Telestration) {
        // Share the map's pan/zoom so annotations are pinned to the map
        // rather than to the screen (static/telestration.js).
        telestrator = Telestration.create(drawEl, { getView: () => view });
        document.querySelectorAll('.tele-tool').forEach(btn => btn.addEventListener('click', () => {
            const next = telestrator.getTool() === btn.dataset.tool ? null : btn.dataset.tool;
            telestrator.setTool(next);
            document.querySelectorAll('.tele-tool').forEach(b => b.classList.toggle('active', b === btn && !!next));
        }));
        on('tele-color', 'input', (e) => telestrator.setColor(e.target.value));
        on('btn-tele-clear', 'click', () => telestrator.clear());
    }
})();

// ─── Controls ─────────────────────────────────────────────────────────────
document.getElementById('btnPlay').onclick = function () {
    if (!isPlaying && matchData) {
        isPlaying = true; this.classList.add('active');
        lastFrameTime = performance.now();
        requestAnimationFrame(animate);
    }
};
document.getElementById('btnPause').onclick = () => {
    isPlaying = false;
    document.getElementById('btnPlay').classList.remove('active');
};
document.getElementById('btnStop').onclick = () => {
    isPlaying = false; currentIdx = 0;
    document.getElementById('btnPlay').classList.remove('active');
    render();
};
timeline.oninput = async e => {
    await seekToGlobalFrame(parseInt(e.target.value));
};
document.querySelectorAll('.speed-btn').forEach(btn => {
    btn.onclick = function () {
        speedMult = parseFloat(this.dataset.speed);
        document.querySelectorAll('.speed-btn').forEach(b => b.classList.remove('active'));
        this.classList.add('active');
    };
});

// ─── Display & Draw popover ────────────────────────────────────────────────
// Off the control bar rather than stacked under Team 2's roster, so it never
// competes with the roster for the map's height budget (see .dd-popover).
(function () {
    const btn = document.getElementById('btnDisplayDraw');
    const pop = document.getElementById('dd-popover');
    if (!btn || !pop) return;
    const close = () => { pop.style.display = 'none'; btn.classList.remove('active'); };
    const open  = () => { pop.style.display = ''; btn.classList.add('active'); };
    btn.onclick = (e) => {
        e.stopPropagation();
        if (pop.style.display === 'none') open(); else close();
    };
    // Click anywhere outside the popover (and its own toggle) closes it.
    document.addEventListener('click', (e) => {
        if (pop.style.display !== 'none' && !pop.contains(e.target) && e.target !== btn) close();
    });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
    // A resize can leave the popover mis-anchored relative to its wrapper's
    // new position (e.g. a responsive tier change) - closing is simpler and
    // safer than trying to re-clamp it.
    window.addEventListener('resize', close);
})();

// ─── Skip ±15s ───────────────────────────────────────────────────────────
const SKIP_TICKS = 15 * TICKRATE; // 15 seconds

async function skipByTicks(deltaTicks) {
    if (!globalTicks.length) return;
    const curFrame = parseInt(timeline.value) || 0;
    const entry    = globalTicks[curFrame];
    if (!entry) return;
    const targetTick = entry.tick + deltaTicks;
    let best = 0, bestDiff = Infinity;
    for (let i = 0; i < globalTicks.length; i++) {
        const diff = Math.abs(globalTicks[i].tick - targetTick);
        if (diff < bestDiff) { bestDiff = diff; best = i; }
        if (globalTicks[i].tick > targetTick + SKIP_TICKS * 2) break;
    }
    await seekToGlobalFrame(best);
}

document.getElementById('btnSkipBack').onclick = () => skipByTicks(-SKIP_TICKS);
document.getElementById('btnSkipFwd').onclick  = () => skipByTicks(+SKIP_TICKS);

// ─── Jump into the real demo ─────────────────────────────────────────────
// Opens the original .dem in CS2 at whatever tick is on screen, spectating
// the followed/POV-locked player when there is one (click a dot or a sidebar
// row to lock) - otherwise CS2 opens there in free-roam.
document.getElementById('btnOpenCS2').onclick = function () {
    const tick = Number(sortedTicks[currentIdx]);
    if (!Number.isFinite(tick)) return;
    DemoJump.launch({ match: matchFile, tick, focusPlayer: lockedName || '', button: this });
};

// ─── Round Navigator ──────────────────────────────────────────────────────
function buildRoundNav() {
    roundBtnScroll.innerHTML = '';
    if (!allRounds.length || !globalTicks.length) return;

    allRounds.forEach((round, i) => {
        const rn = round.round_num;
        const isHalfBoundary =
            rn === REGULATION_HALF + 1 ||   // regulation halftime (round 13)
            (rn > REGULATION_ROUNDS && (rn - REGULATION_ROUNDS - 1) % OT_HALF_LEN === 0); // OT half starts
        if (isHalfBoundary) {
            const div = document.createElement('div');
            div.className = 'round-divider';
            roundBtnScroll.appendChild(div);
        }

        const btn = document.createElement('button');
        btn.className = 'round-btn';
        btn.dataset.roundIdx = i;

        const winner     = (round.winner || '').toLowerCase();
        const site       = getRoundSite(round);
        const reasonIcon = roundReasonIcon(round.reason);
        const reasonTxt  = roundReasonLabel(round.reason);
        const sideLetter = winner === 'ct' ? 'C' : winner === 't' ? 'T' : '';

        btn.style.setProperty('--wc', winner === 'ct' ? 'var(--ct)' : winner === 't' ? 'var(--t)' : 'transparent');
        btn.innerHTML = `<span class="rnum">${round.round_num}</span>` +
            (sideLetter ? `<span class="rside ${winner}">${sideLetter}</span>` : '') +
            (reasonIcon ? `<span class="rreason">${reasonIcon}</span>` : '');
        btn.title = `Round ${round.round_num} - ${winner.toUpperCase()} win${site ? ' · site ' + site.toUpperCase() : ''}${reasonTxt ? ' · ' + reasonTxt : ''}`;

        btn.onclick = async () => {
            // With "Freeze time" on, land on the buy instead of the go-live.
            const jumpTick = display.freeze
                ? (round.start ?? round.freeze_end)
                : (round.freeze_end ?? round.start);
            let best = 0, bestDiff = Infinity;
            for (let j = 0; j < globalTicks.length; j++) {
                const diff = Math.abs(globalTicks[j].tick - jumpTick);
                if (diff < bestDiff) { bestDiff = diff; best = j; }
                if (globalTicks[j].tick > jumpTick + 256) break;
            }
            await seekToGlobalFrame(best);
        };

        roundBtnScroll.appendChild(btn);
    });
}

/** Determine the bomb site from bomb events within this round */
function getRoundSite(round) {
    const idx       = allRounds.indexOf(round);
    const nextRound = allRounds[idx + 1];
    const endTick   = round.end ?? nextRound?.start ?? Infinity;
    for (const b of allBomb) {
        if (b.tick < (round.start ?? 0)) continue;
        if (b.tick > endTick) break;
        if (b.event === 'plant' && b.bombsite) return b.bombsite;
    }
    if (round.reason) {
        const wr = round.reason.toString().toLowerCase();
        if (wr.includes('_a')) return 'A';
        if (wr.includes('_b')) return 'B';
    }
    return null;
}

// Win-condition icon/label for a round's `reason` field - see
// Match2DLogic.roundReasonIcon/Label (static/match2d.logic.js) for the mapping
// rationale, shared with multi.js and templates/match.html.
function roundReasonIcon(reason)  { return Match2DLogic.roundReasonIcon(reason, window.Assets); }
function roundReasonLabel(reason) { return Match2DLogic.roundReasonLabel(reason); }

/**
 * Highlight the active round button based on current tick.
 *
 * THIS FUNCTION MUST NOT SCROLL. It ran `btn.scrollIntoView({block:'nearest'})`
 * on the active button, and it is called from `render()` - so during playback
 * it fired ~60 times a second. `#round-btn-scroll` is `flex-wrap: wrap` and has
 * no scrollable ancestor of its own (the name is a leftover from when the
 * rounds were one horizontal strip), so the only thing that call could ever
 * scroll was the PAGE. On a full-screen window the round nav is already on
 * screen and `'nearest'` is a no-op, which is why it went unnoticed; on a
 * quarter window the nav sits below the fold, so every frame dragged the page
 * back down to it and scrolling up was impossible while a replay was running.
 * The buttons all wrap and are always visible - there is nothing to scroll to.
 */
function updateActiveRoundBtn(currentTick) {
    const roundNum = getRoundNum(currentTick);
    document.querySelectorAll('.round-btn').forEach(btn => {
        const idx = parseInt(btn.dataset.roundIdx);
        const r   = allRounds[idx];
        btn.classList.toggle('active-round', !!(r && r.round_num === roundNum));
    });
}

// ─── Segmented Timeline ───────────────────────────────────────────────────
function tickToPct(tick) {
    const total = globalTicks.length - 1;
    if (total <= 0) return 0;
    let pos = globalTicks.findIndex(g => g.tick >= tick);
    if (pos === -1) pos = total;
    return (pos / total) * 100;
}

function buildTimelineSegments() {
    timelineSegs.innerHTML = '';
    if (!allRounds.length || !globalTicks.length) return;

    allRounds.forEach(round => {
        const pct = tickToPct(round.freeze_end ?? round.start);
        if (pct <= 0.3 || pct >= 99.5) return;

        const tick = document.createElement('div');
        tick.className = 'seg-tick';
        tick.style.left = pct + '%';

        const label = document.createElement('div');
        label.className = 'seg-label';
        label.style.left = pct + '%';
        label.textContent = 'R' + round.round_num;

        timelineSegs.appendChild(tick);
        timelineSegs.appendChild(label);
    });

    // Kill markers - small neutral ticks for every kill in the match. When a
    // player is locked, that player's kills/deaths get taller coloured markers
    // (green = kill, red = death) so their impact reads along the timeline.
    const total = globalTicks.length - 1;
    for (const k of allKills) {
        let gf = globalTicks.findIndex(g => g.tick >= k.tick);
        if (gf === -1) gf = total;
        const pct = total > 0 ? (gf / total) * 100 : 0;
        if (pct < 0 || pct > 100) continue;
        const m = document.createElement('div');
        const isHl = lockedName && (k.attacker_name === lockedName || k.victim_name === lockedName);
        m.className = !isHl ? 'seg-kill'
            : (k.attacker_name === lockedName ? 'seg-hl seg-hl-kill' : 'seg-hl seg-hl-death');
        m.style.left = pct + '%';
        m.title = `${k.attacker_name || '?'} › ${k.victim_name || '?'}`;
        if (isHl) { m.style.cursor = 'pointer'; m.addEventListener('click', () => seekToGlobalFrame(gf)); }
        timelineSegs.appendChild(m);
    }
}

// ─── Canvas Scoreboard (round wins) ──────────────────────────────────────
function drawScoreboard(currentTick) {
    // ── Win counters with side-swap support (handles regulation + OT) ─────────
    let teamAWins = 0, teamBWins = 0;  // teamA = first-half CT, teamB = first-half T

    for (let i = 0; i < allRounds.length; i++) {
        const r = allRounds[i];
        const endTick = r.end ?? r.freeze_end ?? allRounds[i+1]?.start ?? null;
        if (endTick == null || endTick > currentTick) continue;
        const w = (r.winner || '').toLowerCase();
        if (!isSwappedAtRound(r.round_num)) {
            if (w === 'ct') teamAWins++;
            else if (w === 't') teamBWins++;
        } else {
            if (w === 'ct') teamBWins++;
            else if (w === 't') teamAWins++;
        }
    }

    const currentRound  = getRoundNum(currentTick) ?? 1;
    const sidesSwapped  = isSwappedAtRound(currentRound);
    const leftScore     = sidesSwapped ? teamBWins : teamAWins;
    const rightScore    = sidesSwapped ? teamAWins : teamBWins;

    // ── Scoreboard pill ──────────────────────────────────────────────────────
    const W = 220, H = 44;
    const x = Math.round((canvas.width - W) / 2);
    const y = 10;

    ctx.save();

    ctx.globalAlpha = 0.78;
    ctx.fillStyle = '#0a0c0f';
    roundRect(ctx, x, y, W, H, 6);
    ctx.fill();
    ctx.globalAlpha = 1;
    ctx.strokeStyle = '#1e2530';
    ctx.lineWidth = 1;
    roundRect(ctx, x, y, W, H, 6);
    ctx.stroke();

    const midX    = x + W / 2;
    const centerY = y + H / 2;

    // Two mirrored columns, each with its side label centred over its own
    // number. Every one of these six pieces used to be placed by its own
    // hand-picked offset off a mixed textAlign - CT ended up 24px left of the
    // number it belongs to while T was 6px right of its own, so the header did
    // not line up with the score under it and the whole pill read as crooked.
    const COL = 34;                     // column centre, either side of the dash
    const drawSide = (cx, label, score, color) => {
        ctx.textAlign = 'center';
        ctx.font = 'bold 11px ' + FmtLogic.FONT_UI;   // side label
        ctx.fillStyle = '#8a9ab0';
        ctx.fillText(label, cx, centerY - 7);
        ctx.font = 'bold 22px ' + FmtLogic.FONT_MONO;            // score digits
        ctx.fillStyle = color;
        ctx.fillText(score, cx, centerY + 12);
    };
    drawSide(midX - COL, 'CT', leftScore,  '#4a9eff');
    drawSide(midX + COL, 'T',  rightScore, '#ffaa22');

    ctx.textAlign = 'center';
    ctx.font = 'bold 16px ' + FmtLogic.FONT_MONO;
    ctx.fillStyle = '#3a4658';
    ctx.fillText('-', midX, centerY + 9);

    ctx.textAlign = 'left';

    // ── Round clock pill (below scoreboard) ──────────────────────────────────
    const CW = 160, CH = 28;
    const cx = Math.round((canvas.width - CW) / 2);
    const cy = y + H + 4;

    // Find the current round object
    let round = null;
    for (let i = allRounds.length - 1; i >= 0; i--) {
        if (allRounds[i].start != null && allRounds[i].start <= currentTick) {
            round = allRounds[i];
            break;
        }
    }

    let clockText  = '';
    let clockColor = '#c8d8e8';
    let labelText  = '';
    // Hoisted out of the `if (round)` below: the plant/defuse strip needs the
    // plant tick to answer "does this defuse beat the fuse?", and null is a
    // legitimate answer there (no plant in scope -> no verdict drawn).
    let bombPlantTickForHud = null;

    if (round) {
        const freezeEnd    = round.freeze_end ?? (round.start + FREEZE_TIME_TICKS);
        // Clock counts down from 1:55 at freeze_end - CS2 round timer
        // always starts from the full 1:55 regardless of actual round end
        const clockEnd     = freezeEnd + ROUND_TIME_TICKS;

        // Check bomb state
        const bombState = getBombState(currentTick);
        const bombPlantTick = (bombState && bombState.event === 'plant') ? bombState.tick : null;
        bombPlantTickForHud = bombPlantTick;

        if (currentTick < freezeEnd) {
            // ── Freeze time ──
            const remaining = Math.max(0, freezeEnd - currentTick);
            const secs = Math.ceil(remaining / TICKRATE);
            clockText  = formatClockTime(secs);
            clockColor = '#aaccff';
            labelText  = 'FREEZE';
        } else if (bombPlantTick != null) {
            // ── Bomb planted - counts down from 40s ──
            const elapsed   = currentTick - bombPlantTick;
            const remaining = Math.max(0, BOMB_TIME_TICKS - elapsed);
            const secs = Math.ceil(remaining / TICKRATE);
            clockText  = formatClockTime(secs);
            clockColor = secs <= 10 ? '#ff4444' : '#ff8c00';
            labelText  = 'PLANTED';
        } else if (bombState && (bombState.event === 'defuse' || bombState.event === 'detonate')) {
            // ── Round ended via bomb resolution - clock is done ──
            clockText  = '0:00';
            clockColor = '#556070';
            labelText  = '';
        } else if (currentTick <= clockEnd) {
            // ── Normal round time - counts down from 1:55 at freeze_end ──
            const remaining = Math.max(0, clockEnd - currentTick);
            const secs = Math.ceil(remaining / TICKRATE);
            clockText  = formatClockTime(secs);
            clockColor = secs <= 10 ? '#ff6644' : '#c8d8e8';
            labelText  = '';
        } else {
            // Round over
            clockText  = '0:00';
            clockColor = '#556070';
            labelText  = '';
        }
    }

    // Draw clock background pill
    ctx.globalAlpha = 0.78;
    ctx.fillStyle = '#0a0c0f';
    roundRect(ctx, cx, cy, CW, CH, 6);
    ctx.fill();
    ctx.globalAlpha = 1;
    ctx.strokeStyle = '#1e2530';
    ctx.lineWidth = 1;
    roundRect(ctx, cx, cy, CW, CH, 6);
    ctx.stroke();

    const cmidX   = cx + CW / 2;
    const cmidY   = cy + CH / 2;

    if (labelText) {
        // Label + time side by side
        ctx.textAlign = 'center';
        ctx.font = 'bold 10px ' + FmtLogic.FONT_UI;   // phase label
        ctx.fillStyle = clockColor;
        ctx.globalAlpha = 0.7;
        ctx.fillText(labelText, cmidX - 18, cmidY + 4);
        ctx.globalAlpha = 1;
        ctx.font = 'bold 14px ' + FmtLogic.FONT_MONO;            // clock
        ctx.fillStyle = clockColor;
        ctx.fillText(clockText, cmidX + 28, cmidY + 5);
    } else {
        ctx.textAlign = 'center';
        ctx.font = 'bold 15px ' + FmtLogic.FONT_MONO;            // clock
        ctx.fillStyle = clockColor;
        ctx.fillText(clockText, cmidX, cmidY + 5);
    }

    // ── Plant / defuse strip, directly under the clock pill ───────────────
    //
    // Inside the same save()/restore() and the same withHudScale() transform
    // as the pill, so it grows from the same anchor and follows the HUD-size
    // slider without threading a multiplier through any of these numbers.
    //
    // The verdict is the point of the whole thing. A defuse is only
    // interesting relative to the fuse, and the answer is knowable: the plant
    // tick gives the 40 s deadline and BombDefuseStart's has_kit gives 5 s or
    // 10 s. Showing a bare progress bar and leaving the viewer to do that
    // arithmetic would waste the one piece of information the data uniquely
    // has. When it can't be known - a plant, or a defuse with no plant tick
    // in scope - nothing is claimed rather than a guess being drawn.
    if (bombAction) {
        const BW = CW, BH = 15, BX = cx, BY = cy + CH + 3;
        const isPlant = bombAction.kind === 'plant';
        // Same source as the ring, so the strip and the circle on the map can
        // never disagree about what colour a plant or a defuse is.
        const col = BombAction.ringColor(bombAction.kind);

        ctx.globalAlpha = 0.78;
        ctx.fillStyle = '#0a0c0f';
        roundRect(ctx, BX, BY, BW, BH, 6);
        ctx.fill();
        ctx.globalAlpha = 1;

        // Fill sweeps left to right behind the text, clipped to the pill so
        // its square end can't poke out of the rounded corner.
        ctx.save();
        roundRect(ctx, BX, BY, BW, BH, 6);
        ctx.clip();
        ctx.globalAlpha = 0.3;
        ctx.fillStyle = col;
        ctx.fillRect(BX, BY, BW * bombAction.progress, BH);
        ctx.globalAlpha = 1;
        ctx.restore();

        ctx.strokeStyle = col;
        ctx.lineWidth = 1;
        roundRect(ctx, BX, BY, BW, BH, 6);
        ctx.stroke();

        const verdict = BombActionLogic.defuseVerdict(bombAction, bombPlantTickForHud);
        const remain = BombActionLogic.remainingSeconds(bombAction, currentTick);

        ctx.textAlign = 'left';
        ctx.font = 'bold 9px ' + FmtLogic.FONT_UI;               // action label
        ctx.fillStyle = col;
        ctx.fillText(isPlant ? 'PLANTING' : 'DEFUSING', BX + 6, BY + 11);

        ctx.textAlign = 'right';
        ctx.font = 'bold 10px ' + FmtLogic.FONT_MONO;            // seconds left
        ctx.fillStyle = '#c8d8e8';
        ctx.fillText(remain.toFixed(1) + 's', BX + BW - 6, BY + 11);

        if (verdict) {
            ctx.textAlign = 'center';
            ctx.font = 'bold 9px ' + FmtLogic.FONT_UI;           // in time / short
            ctx.fillStyle = verdict.inTime ? '#3ddc84' : '#ff4444';
            ctx.fillText(
                verdict.inTime
                    ? 'IN TIME +' + verdict.marginSeconds.toFixed(1) + 's'
                    : Math.abs(verdict.marginSeconds).toFixed(1) + 's SHORT',
                BX + BW / 2 + 6, BY + 11);
        }
    }

    ctx.textAlign = 'left';
    ctx.restore();
}

function formatClockTime(totalSeconds) {
    const m = Math.floor(totalSeconds / 60);
    const s = (totalSeconds % 60).toString().padStart(2, '0');
    return `${m}:${s}`;
}

/**
 * Run `draw` with the context scaled by `hudScale` about a fixed anchor point.
 *
 * The HUD is drawn from ~30 hardcoded sizes - pill widths, font sizes, text
 * offsets, icon heights, row pitch, gaps. Threading a multiplier through every
 * one of them would be that many chances to miss one and leave a 12px label in
 * a box that grew. Scaling the transform instead is exact by construction:
 * fonts, images, line widths and spacing all follow, including the ones inside
 * killfeed.js, which needs no change at all.
 *
 * The anchor is why this is a transform and not just ctx.scale(): each HUD
 * element is pinned to an edge (the scoreboard to top-centre, the feed to
 * top-right) and must GROW FROM that edge rather than drift off it. Scaling
 * about the anchor keeps the pinned point fixed - anything drawn at `ax` stays
 * at `ax`, so killfeed.js can keep right-aligning against canvas.width and land
 * in the same place at every scale.
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

function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.lineTo(x + w - r, y);
    ctx.quadraticCurveTo(x + w, y, x + w, y + r);
    ctx.lineTo(x + w, y + h - r);
    ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
    ctx.lineTo(x + r, y + h);
    ctx.quadraticCurveTo(x, y + h, x, y + h - r);
    ctx.lineTo(x, y + r);
    ctx.quadraticCurveTo(x, y, x + r, y);
    ctx.closePath();
}