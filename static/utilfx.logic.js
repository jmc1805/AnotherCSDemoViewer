/**
 * utilfx.logic.js - pure math behind the game-faithful utility rendering
 * (volumetric smokes that HE grenades carve, fire that spreads over time,
 * molotov-vs-incendiary sizing). No DOM, no canvas: unit-testable under node
 * (test/utilfx.logic.test.mjs). The canvas glue lives in static/utilfx.js;
 * consumers are viewer.js and multi.js.
 *
 * All radii here are WORLD UNITS - callers convert to base px with
 * worldUnits / mapConfig.scale (the per-map calibration in MAP_LIBRARY).
 * Old util rendering used hardcoded canvas-px radii; this module is the
 * single place the real-game sizes live.
 *
 * Data reality (see cmd/parser/main.go): the `infernos` array is a bare
 * centroid + start/end ticks - no weapon field on matches parsed before the
 * additive `weapon`/`cells` fields landed. classifyInfernos() therefore
 * falls back to correlating each inferno with the Molotov/Incendiary
 * projectile trajectories in `grenades` (thrower + time + landing point),
 * which exist in every processed match.
 */
(function (root, factory) {
    if (typeof module === 'object' && module.exports) module.exports = factory();
    else root.UtilFXLogic = factory();
}(typeof self !== 'undefined' ? self : this, function () {
    'use strict';

    // ── Real-game sizes (world units) ────────────────────────────────────────
    const SMOKE_RADIUS      = 144;  // CS2 volumetric smoke sphere
    const MOLOTOV_RADIUS    = 140;  // T molotov fire footprint (larger)
    const INCENDIARY_RADIUS = 110;  // CT incendiary burns a smaller area
    const HE_HOLE_RADIUS    = 100;  // hole an HE carves through a smoke
    const FIRE_SPREAD_TICKS = 128;  // ~2 s: fire grows from impact to full size
    const SMOKE_BLOOM_TICKS = 64;   // ~1 s: smoke blooms to full volume
    const HOLE_REGROW_TICKS = 160;  // ~2.5 s: carved smoke refills
    const HOLE_PUNCH_TICKS  = 6;    // carve opens near-instantly

    // Authoritative real-game durations (@ 64 tick/s, per the CS wiki: fire
    // burns 7.03125s for both molotov and incendiary, smoke lasts 18s once
    // deployed). The demo's InfernoExpired/SmokeExpired events fire well
    // after this - they track entity/ember persistence, not the actual
    // burn/screen duration (observed on real matches: recorded durations
    // cluster around 20-22s) - so displayed lifetime is always clamped to
    // these rather than trusting the recorded end_tick.
    const FIRE_LIFETIME_TICKS  = 450;   // 7.03125 s
    const SMOKE_LIFETIME_TICKS = 1152;  // 18 s

    // Clamp a recorded end_tick (possibly null/missing, possibly inflated by
    // demo entity-lingering) to the authoritative max lifetime from start.
    // Never extends a genuinely-early end (e.g. round ending) past `startTick`.
    function clampEnd(startTick, endTick, maxTicks) {
        const recorded = (endTick == null) ? Infinity : endTick;
        return Math.min(recorded, startTick + maxTicks);
    }

    // ── Deterministic PRNG (mulberry32) - stable per-entity visuals ─────────
    function rng(seed) {
        let a = seed >>> 0;
        return function () {
            a |= 0; a = (a + 0x6D2B79F5) | 0;
            let t = Math.imul(a ^ (a >>> 15), 1 | a);
            t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
            return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
        };
    }

    // Blob layout for the volumetric smoke look / fire tongues: `count`
    // pseudo-random circles inside the unit disc, deterministic for a seed so
    // a given smoke keeps its shape between frames. Returns [{dx,dy,r}] with
    // dx/dy in [-1,1] (fraction of parent radius) and r a fraction of it.
    function blobLayout(seed, count) {
        const r = rng(seed);
        const blobs = [];
        for (let i = 0; i < count; i++) {
            const ang = r() * Math.PI * 2;
            const dist = Math.sqrt(r()) * 0.62;       // bias toward the core
            blobs.push({
                dx: Math.cos(ang) * dist,
                dy: Math.sin(ang) * dist,
                r: 0.38 + r() * 0.34,
                phase: r() * Math.PI * 2,             // per-blob drift phase
            });
        }
        return blobs;
    }

    const easeOut = t => 1 - Math.pow(1 - Math.min(1, Math.max(0, t)), 3);

    // Fire footprint radius at `age` ticks: eases out from ~15% to the
    // weapon's full radius over FIRE_SPREAD_TICKS (in-game fire spreads
    // outward from the impact point rather than appearing full-size).
    function fireSpreadRadius(age, maxRadius, spreadTicks) {
        const st = spreadTicks || FIRE_SPREAD_TICKS;
        return maxRadius * (0.15 + 0.85 * easeOut(age / st));
    }

    function infernoMaxRadius(weapon) {
        return weapon === 'Incendiary' ? INCENDIARY_RADIUS : MOLOTOV_RADIUS;
    }

    // Smoke bloom: fraction of full volume at `age` ticks.
    function smokeBloom(age, bloomTicks) {
        return easeOut(age / (bloomTicks || SMOKE_BLOOM_TICKS));
    }

    // ── Molotov vs incendiary classification ────────────────────────────────
    // Prefer the parser's additive `weapon` field (new parses). Otherwise
    // correlate: reduce the huge per-frame `grenades` trajectory stream to
    // one landing record per fire-grenade entity, then match each inferno to
    // the record with the same thrower whose last-seen tick is closest to
    // (and at most `window` ticks before / slightly after) the fire start,
    // breaking ties by landing-point distance when positions are usable.
    // Returns a new array of 'Molotov' | 'Incendiary' aligned with infernos.
    function classifyInfernos(infernos, grenades, opts) {
        const o = opts || {};
        const windowTicks = o.windowTicks || 256;
        const fireEnts = new Map();   // entity_id -> {type, thrower, tick, x, y}
        for (const g of grenades || []) {
            if (g.grenade_type !== 'Molotov' && g.grenade_type !== 'Incendiary') continue;
            const prev = fireEnts.get(g.entity_id);
            if (!prev || g.tick >= prev.tick) {
                fireEnts.set(g.entity_id, {
                    type: g.grenade_type, thrower: g.thrower,
                    tick: g.tick, x: g.X, y: g.Y,
                });
            }
        }
        const landings = [...fireEnts.values()];
        return (infernos || []).map(inf => {
            if (inf.weapon === 'Molotov' || inf.weapon === 'Incendiary') return inf.weapon;
            let best = null, bestScore = Infinity;
            for (const l of landings) {
                if (l.thrower !== inf.thrower_name) continue;
                const dt = inf.start_tick - l.tick;   // landing precedes the fire
                if (dt < -32 || dt > windowTicks) continue;
                // Time distance dominates; positional distance breaks ties
                // (inferno centroid can be (0,0) on old parses, so only use
                // position when both look sane).
                let score = Math.abs(dt);
                if ((inf.X || inf.Y) && (l.x || l.y)) {
                    score += Math.hypot(inf.X - l.x, inf.Y - l.y) / 100;
                }
                if (score < bestScore) { bestScore = score; best = l; }
            }
            return best ? best.type : 'Molotov';
        });
    }

    // ── HE-carved smoke holes ────────────────────────────────────────────────
    // CS2 smokes are volumetric: an HE detonating inside one blasts a
    // temporary hole that then refills. Given one smoke, the HE detonation
    // list and the current tick, return the active holes as
    // [{x, y, r, strength}] where strength 1 = fully carved, 0 = refilled.
    // `heList` entries need {tick, X, Y}; `tickOf` lets multi.js pass
    // round-relative tick accessors.
    function smokeHoles(smoke, heList, tick, opts) {
        const o = opts || {};
        const smokeR = o.smokeRadius || SMOKE_RADIUS;
        const holeR = o.holeRadius || HE_HOLE_RADIUS;
        const regrow = o.regrowTicks || HOLE_REGROW_TICKS;
        const punch = o.punchTicks || HOLE_PUNCH_TICKS;
        const tickOf = o.tickOf || (h => h.tick);
        const startOf = o.startOf || (s => s.start_tick);
        const endOf = o.endOf || (s => s.end_tick);
        const holes = [];
        for (const he of heList || []) {
            const ht = tickOf(he);
            const age = tick - ht;
            if (age < 0 || age > regrow) continue;
            // only detonations while the smoke is up, inside its volume
            if (ht < startOf(smoke) || ht > endOf(smoke)) continue;
            const d = Math.hypot(he.X - smoke.X, he.Y - smoke.Y);
            if (d > smokeR + holeR * 0.5) continue;
            const open = Math.min(1, age / punch);            // fast punch-in
            const refill = 1 - easeOut((age - punch) / (regrow - punch));
            const strength = open * Math.max(0, Math.min(1, refill));
            if (strength > 0.02) holes.push({ x: he.X, y: he.Y, r: holeR, strength });
        }
        return holes;
    }

    // Per-blob carve factor: how much a smoke blob at world (bx,by) shrinks
    // given the active holes. 1 = untouched, 0 = fully carved away.
    function blobCarve(bx, by, holes) {
        let f = 1;
        for (const h of holes || []) {
            const d = Math.hypot(bx - h.x, by - h.y);
            if (d < h.r) {
                const local = 1 - (d / h.r);                  // 1 at hole center
                f = Math.min(f, 1 - h.strength * easeOut(local * 1.6));
            }
        }
        return Math.max(0, f);
    }

    return {
        SMOKE_RADIUS, MOLOTOV_RADIUS, INCENDIARY_RADIUS, HE_HOLE_RADIUS,
        FIRE_SPREAD_TICKS, SMOKE_BLOOM_TICKS, HOLE_REGROW_TICKS,
        FIRE_LIFETIME_TICKS, SMOKE_LIFETIME_TICKS, clampEnd,
        rng, blobLayout, easeOut,
        fireSpreadRadius, infernoMaxRadius, smokeBloom,
        classifyInfernos, smokeHoles, blobCarve,
    };
}));
