/**
 * bombaction.logic.js - "is someone planting or defusing right now, and is it
 * going to finish in time?"
 *
 * Pure, DOM-free, unit-tested (`node test/bombaction.logic.test.mjs`). The
 * canvas ring in viewer.js and the HUD line are both just drawings of what
 * this returns.
 *
 * WHY THIS IS DERIVED FROM EVENTS AND NOT FROM A PER-TICK FLAG. demoinfocs
 * exposes `Player.IsPlanting` / `Player.IsDefusing` as per-tick booleans, and
 * the v2 chunk flags word has spare bits, so carrying them per tick was the
 * obvious-looking route. It is the wrong one: a boolean says the action is
 * happening, not when it STARTED or how long it takes - and both are needed
 * to draw a countdown, so you would have to scan backwards for the leading
 * edge to recover what the event already tells you. `BombDefuseStart` also
 * carries `HasKit`, which is the difference between a 5 s and a 10 s defuse
 * and therefore the whole of "does this beat the fuse?". Events cost a parser
 * change; per-tick bits cost the v2 chunk format AND decoder.js in lockstep.
 *
 * THE BOMB TIMELINE IS ONE ARRAY WITH TWO KINDS OF ENTRY. `bomb[]` holds both
 * "where is the bomb" states (plant, defuse, pickup, drop, detonate) and the
 * in-progress actions this module reads (plant_begin, plant_abort,
 * defuse_start, defuse_abort). Anything asking "where is the bomb" must
 * ignore the action events - see STATE_EVENTS / viewer.js's getBombState,
 * which is a last-event-wins scan and would otherwise answer "defuse_start"
 * for a planted bomb and lose the 40 s countdown mid-defuse.
 */
(function (root, factory) {
    if (typeof module === 'object' && module.exports) module.exports = factory();
    else root.BombActionLogic = factory();
}(typeof self !== 'undefined' ? self : globalThis, function () {

    const TICKRATE = 64;

    // Events that answer "where is the bomb / what happened to it".
    const STATE_EVENTS = ['plant', 'defuse', 'pickup', 'drop', 'detonate'];
    // Events that answer "what is someone doing to it right now".
    const ACTION_START = { plant_begin: 'plant', defuse_start: 'defuse' };
    const ACTION_END = {
        plant_abort: 'plant', plant: 'plant',
        defuse_abort: 'defuse', defuse: 'defuse', detonate: 'defuse',
    };

    // CS2 durations, IN TICKS, because that is the unit the demo actually
    // carries and every one of these was MEASURED from it - not transcribed
    // from a wiki. measure() below is the tool; two re-parsed matches
    // (de_mirage 2026-06-16, de_dust 2026-06-13) gave:
    //
    //   plant           200 ticks in 20 of 21 completed plants (one 201,
    //                   one 199 - a frame either side of the boundary)
    //   defuse, kit     320 ticks, exactly, 4 of 4
    //   defuse, no kit  640 ticks, exactly, 2 of 2
    //
    // PLANT IS 3.125 s, NOT THE 3.2 s THE WIKI GIVES. This code shipped with
    // 3.2 for about an hour and the corpus contradicted it on the first
    // re-parse: 3.2 s is 204.8 ticks, and nothing in the data lands there.
    // That is the whole reason these are measured - a ring that sweeps 2.4%
    // too slowly looks perfectly fine, which is a bug you cannot see. Same
    // discipline as UtilFXLogic's smoke/fire lifetimes and MAP_LIBRARY's
    // radar calibration: re-measure after a game update, never nudge.
    //
    // The kit/no-kit split is corroborated independently: has_kit came
    // straight off BombDefuseStart and it partitioned 320 from 640 with no
    // exceptions.
    const PLANT_TICKS = 200;
    const DEFUSE_TICKS = 640;
    const DEFUSE_KIT_TICKS = 320;
    const FUSE_TICKS = 40 * TICKRATE;      // 40 s fuse, as viewer.js already had

    // Seconds are derived, never the source - see the tick constants above.
    const PLANT_SECONDS = PLANT_TICKS / TICKRATE;
    const DEFUSE_SECONDS = DEFUSE_TICKS / TICKRATE;
    const DEFUSE_KIT_SECONDS = DEFUSE_KIT_TICKS / TICKRATE;
    const FUSE_SECONDS = FUSE_TICKS / TICKRATE;

    function durationTicks(kind, hasKit) {
        if (kind === 'plant') return PLANT_TICKS;
        return hasKit ? DEFUSE_KIT_TICKS : DEFUSE_TICKS;
    }

    function durationSeconds(kind, hasKit) {
        return durationTicks(kind, hasKit) / TICKRATE;
    }

    /** True for the events that describe where the bomb is, not what someone
     *  is doing to it. The filter viewer.js's getBombState needs. */
    function isStateEvent(ev) {
        return STATE_EVENTS.indexOf(ev) !== -1;
    }

    /**
     * The plant/defuse in progress at `tick`, or null.
     *
     * @param {Array} bombEvents  the match's `bomb[]`, ascending by tick
     * @param {number} tick
     * @param {number} roundStart round-scope the scan, like getBombState
     * @returns {?Object} {kind:'plant'|'defuse', player, startTick, hasKit,
     *   durationTicks, endTick, elapsedTicks, progress}
     *
     * An action ends at its own completion/abort event. It ALSO expires at its
     * nominal duration even when no end event arrived: if a defuser is killed
     * mid-defuse and the demo carries no BombDefuseAborted, an un-expiring
     * action would leave a progress ring pinned to a corpse for the rest of
     * the round. Capping is the safe direction - the worst case is a ring that
     * vanishes a moment early, rather than one that never leaves.
     */
    function actionAt(bombEvents, tick, roundStart) {
        if (!bombEvents || !bombEvents.length) return null;
        const from = roundStart == null ? -Infinity : roundStart;
        let open = null;
        for (let i = 0; i < bombEvents.length; i++) {
            const b = bombEvents[i];
            if (b.tick < from) continue;
            if (b.tick > tick) break;
            const started = ACTION_START[b.event];
            if (started) {
                // An absent has_kit (a parse predating the field) must read
                // as "no kit": 10 s is the safe assumption, since claiming
                // the 5 s defuse would draw a ring that finishes while the
                // real one is still running.
                const hasKit = b.has_kit === true;
                const dur = durationTicks(started, hasKit);
                open = {
                    kind: started,
                    player: b.name || '',
                    startTick: b.tick,
                    hasKit: started === 'defuse' ? hasKit : null,
                    durationTicks: dur,
                    endTick: b.tick + dur,
                    x: b.X, y: b.Y,
                };
                continue;
            }
            // An end event clears only its own kind: a `plant` completing does
            // not cancel a defuse, and the two never overlap in practice, but
            // keying on kind means a stray event can't silently clear the
            // wrong action.
            if (open && ACTION_END[b.event] === open.kind) open = null;
        }
        if (!open) return null;
        if (tick >= open.endTick) return null;          // expired, no end event
        open.elapsedTicks = tick - open.startTick;
        open.progress = open.durationTicks > 0
            ? Math.max(0, Math.min(1, open.elapsedTicks / open.durationTicks))
            : 0;
        return open;
    }

    /**
     * Will a defuse in progress finish before the bomb goes off?
     *
     * @param {Object} action    from actionAt(), kind 'defuse'
     * @param {?number} plantTick tick the bomb was planted
     * @returns {?Object} {inTime, marginSeconds, defuseEndsAt, fuseEndsAt}
     *   or null when it can't be known (not a defuse, or no plant tick).
     *
     * This is the point of the whole feature. `marginSeconds` is positive when
     * the defuse lands first; the sign is what the HUD renders as IN TIME vs
     * N.Ns SHORT. It is only as honest as DEFUSE_*_SECONDS and FUSE_SECONDS,
     * which is why those are transcribed constants with a measurement path.
     */
    function defuseVerdict(action, plantTick) {
        if (!action || action.kind !== 'defuse' || plantTick == null) return null;
        const fuseEndsAt = plantTick + FUSE_TICKS;
        const defuseEndsAt = action.endTick;
        return {
            inTime: defuseEndsAt <= fuseEndsAt,
            marginSeconds: (fuseEndsAt - defuseEndsAt) / TICKRATE,
            defuseEndsAt,
            fuseEndsAt,
        };
    }

    /** Seconds still to run on an action, for the HUD readout. */
    function remainingSeconds(action, tick) {
        if (!action) return 0;
        return Math.max(0, (action.endTick - tick) / TICKRATE);
    }

    /**
     * Observed durations of every COMPLETED action in a match, as
     * {plant: [...], defuse: [...], defuseKit: [...]} in seconds.
     *
     * Not used at render time - this is the check that the constants above are
     * real. A parse carrying begin events knows both ends of every finished
     * action, so the corpus can confirm 3.2 / 5 / 10 instead of the code
     * assuming them.
     */
    function measure(bombEvents) {
        const out = { plant: [], defuse: [], defuseKit: [] };
        let open = null;
        for (const b of (bombEvents || [])) {
            const started = ACTION_START[b.event];
            if (started) { open = { kind: started, tick: b.tick, hasKit: b.has_kit === true }; continue; }
            if (!open) continue;
            if (b.event === 'plant' && open.kind === 'plant') {
                out.plant.push((b.tick - open.tick) / TICKRATE);
                open = null;
            } else if (b.event === 'defuse' && open.kind === 'defuse') {
                (open.hasKit ? out.defuseKit : out.defuse).push((b.tick - open.tick) / TICKRATE);
                open = null;
            } else if (ACTION_END[b.event] === open.kind) {
                open = null;                      // aborted: no duration to learn
            }
        }
        return out;
    }

    return {
        TICKRATE, STATE_EVENTS,
        PLANT_TICKS, DEFUSE_TICKS, DEFUSE_KIT_TICKS, FUSE_TICKS,
        PLANT_SECONDS, DEFUSE_SECONDS, DEFUSE_KIT_SECONDS, FUSE_SECONDS,
        isStateEvent, durationTicks, durationSeconds, actionAt, defuseVerdict,
        remainingSeconds, measure,
    };
}));
