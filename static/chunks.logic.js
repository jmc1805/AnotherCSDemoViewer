/**
 * chunks.logic.js - pure logic for reading tick chunks in the 2D pages
 * (viewer.js, multi.js, match.html). No DOM, no fetch: everything here is
 * unit-testable under node (test/chunks.logic.test.mjs).
 *
 * The v2 path converts a decoded binary chunk (the typed-array bundle produced
 * by static/decoder.js, whose format spec is the header comment of
 * cmd/parser/chunks_v2.go) into the exact per-tick dict shape the JSON chunks
 * used: { "<tick>": [ {name, side, X, Y, Z, health, yaw, pitch, eye_z, speed,
 * flash_duration, inventory, has_c4, active_weapon, round_num, money, armor,
 * helmet, defuse_kit}, … ] } - so the three consumers keep their render code.
 * speed is new (not part of the legacy JSON-chunk shape) - a v2-only addition,
 * so consumers should guard for it being absent on non-v2 data.
 *
 * flash_duration and round_num are not stored in v2 chunks; they are
 * reconstructed from the master JSON's `blind` events and `rounds` array.
 */
'use strict';

(function (root, factory) {
    const api = factory();
    if (typeof module !== 'undefined' && module.exports) module.exports = api; // node (CJS)
    root.ChunksLogic = api;                                                    // browser global
}(typeof self !== 'undefined' ? self : globalThis, function () {

    const TICKRATE = 64;
    // v2 flags bits (chunks_v2.go)
    const FLAG_HAS_C4  = 1 << 1;
    const FLAG_HELMET  = 1 << 2;
    const FLAG_DEFUSE  = 1 << 3;
    const FLAG_SIDE_T  = 1 << 5;   // bit 4 (blinded) unused here - see flash_duration note below
    const V2_POS_STEP  = 2;      // position snapshots every 2nd stored tick (spec constant)
    const MAX_FLASH_SECS = 6;    // longest possible full blind - scan window for events

    /** Pick the chunk index to play from: prefer v2 binary chunks. */
    function unifiedIndex(matchData) {
        const v2 = matchData.chunkIndexV2;
        if (Array.isArray(v2) && v2.length) return { entries: v2, isV2: true };
        return { entries: matchData.chunkIndex || [], isV2: false };
    }

    /** Tick spacing of stored snapshots (used for synthetic timeline entries
     *  covering not-yet-loaded chunks). */
    function effectiveStep(matchData, isV2) {
        return (matchData.tickStep || 1) * (isV2 ? V2_POS_STEP : 1);
    }

    /** blind events → {victim_name: [{tick, dur}] sorted by tick}. */
    function buildBlindIndex(blindEvents) {
        const byVictim = new Map();
        for (const e of blindEvents || []) {
            if (!e || e.victim_name == null || e.tick == null) continue;
            let arr = byVictim.get(e.victim_name);
            if (!arr) { arr = []; byVictim.set(e.victim_name, arr); }
            arr.push({ tick: e.tick, dur: +e.blind_duration || 0 });
        }
        for (const arr of byVictim.values()) arr.sort((a, b) => a.tick - b.tick);
        return byVictim;
    }

    /** Remaining flash time (seconds) for a player at a tick - the same
     *  semantics as the parser's per-tick flash_duration field. */
    function flashAt(blindIndex, name, tick) {
        const arr = blindIndex.get(name);
        if (!arr) return 0;
        let remaining = 0;
        // Events are sorted by tick; only a small trailing window can still matter.
        for (let i = arr.length - 1; i >= 0; i--) {
            const e = arr[i];
            if (e.tick > tick) continue;
            if (e.tick < tick - MAX_FLASH_SECS * TICKRATE) break;
            const r = e.dur - (tick - e.tick) / TICKRATE;
            if (r > remaining) remaining = r;
        }
        return remaining;
    }

    /** rounds[] → lookup fn tick → round_num (rounds sorted by start). */
    function buildRoundLookup(rounds) {
        const sorted = (rounds || []).slice().sort((a, b) => a.start - b.start);
        return function roundNumAt(tick) {
            for (let i = sorted.length - 1; i >= 0; i--) {
                if (sorted[i].start != null && sorted[i].start <= tick) return sorted[i].round_num;
            }
            return null;
        };
    }

    function dequant(q, min, max) {
        let span = max - min;
        if (span <= 0) span = 1;          // mirrors quant16's span guard in Go
        return min + (q / 65535) * span;
    }

    /**
     * Decoded v2 chunk → JSON-chunk-shaped tick dict.
     * @param chunk decoded bundle from decoder.js
     * @param blindIndex from buildBlindIndex (or null → flash_duration 0)
     * @param roundNumAt from buildRoundLookup (or null → round_num null)
     */
    function v2ToTickDict(chunk, blindIndex, roundNumAt) {
        const { quant, players, weapons, snapTicks, presence, recOff } = chunk;
        const dict = {};
        for (let s = 0; s < snapTicks.length; s++) {
            const tick = snapTicks[s];
            const recs = [];
            let m = presence[s] >>> 0;
            let r = recOff[s];
            while (m) {
                const slot = 31 - Math.clz32(m & -m);   // lowest set bit → ascending slot order
                m &= m - 1;
                const p = players[slot];
                const flags = chunk.flags[r];

                let yaw = (chunk.yaw[r] / 65536) * 360;
                if (yaw > 180) yaw -= 360;              // JSON chunks store -180..180

                const invBits = chunk.inventory[r] >>> 0;
                const inventory = [];
                let inv = invBits & ~1;                 // bit 0 is the "-" none sentinel
                while (inv) {
                    const wi = 31 - Math.clz32(inv & -inv);
                    inv &= inv - 1;
                    if (weapons[wi] && weapons[wi] !== '-') inventory.push(weapons[wi]);
                }
                const activeIdx = chunk.weapon[r];
                const active = activeIdx > 0 && weapons[activeIdx] !== '-' ? weapons[activeIdx] : '';

                recs.push({
                    tick,
                    name: p ? p.name : `slot${slot}`,
                    side: (flags & FLAG_SIDE_T) ? 't' : 'ct',
                    X: dequant(chunk.posX[r], quant.minX, quant.maxX),
                    Y: dequant(chunk.posY[r], quant.minY, quant.maxY),
                    Z: dequant(chunk.posZ[r], quant.minZ, quant.maxZ),
                    eye_z: dequant(chunk.eyeZ[r], quant.minZ, quant.maxZ),
                    // Horizontal ground speed (world units/sec) - velX/velY are
                    // fixed-point *8 (chunks_v2.go quantVel), derived server-side
                    // from per-frame position deltas. Used for the footstep
                    // audibility display (CS2's ~135 u/s silent-movement cutoff).
                    speed: Math.hypot(chunk.velX[r] / 8, chunk.velY[r] / 8),
                    health: chunk.health[r],
                    yaw,
                    pitch: (chunk.pitch[r] / 65535) * 180 - 90,
                    // Reconstructed from master-JSON blind events - same remaining-
                    // seconds semantics as the parser's per-tick field. Not gated on
                    // FLAG_BLINDED: the events are the ground truth for the decay curve.
                    flash_duration: blindIndex ? flashAt(blindIndex, p ? p.name : '', tick) : 0,
                    inventory,
                    has_c4: !!(flags & FLAG_HAS_C4),
                    active_weapon: active,
                    round_num: roundNumAt ? roundNumAt(tick) : null,
                    money: chunk.money[r],
                    armor: chunk.armor[r],
                    helmet: !!(flags & FLAG_HELMET),
                    defuse_kit: !!(flags & FLAG_DEFUSE),
                });
                r++;
            }
            dict[String(tick)] = recs;
        }
        return dict;
    }

    return { unifiedIndex, effectiveStep, buildBlindIndex, flashAt, buildRoundLookup, v2ToTickDict };
}));
