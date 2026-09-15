/**
 * sound.logic.js - what can be HEARD on the map right now, and from how far.
 *
 * The 2D replayer's audibility layer used to draw footsteps and nothing else,
 * which is misleading in the direction that matters: a footstep carries ~1100
 * units, an unsuppressed gunshot carries ~4000, and a grenade bounce carries
 * about as far as the gunshot. Showing only the quietest of the three makes
 * the map look far safer than it is.
 *
 * RADII below are the community-measured CS2/CS:GO values (see the table in
 * the repo notes; measurements are from in-game tests, so treat them as good
 * estimates rather than engine constants). Everything is in WORLD UNITS -
 * callers divide by mapConfig.scale exactly like the utility radii do.
 *
 * Only sounds this repo's parsed data can actually witness are modelled:
 * movement, gunfire, grenade detonations, and the bomb. Reloads, weapon
 * switches and scope clicks are real and loud (~1000u each) but nothing in the
 * demo export records them, and inventing them from inventory changes would be
 * a guess drawn as a measurement. Landing/ladder/water are likewise omitted:
 * a Z delta can suggest a landing but cannot distinguish it from a ramp.
 *
 * Pure: no DOM, no canvas. Unit-tested by test/sound.logic.test.mjs.
 */
(function (root, factory) {
    if (typeof module === 'object' && module.exports) module.exports = factory();
    else root.SoundLogic = factory();
}(typeof self !== 'undefined' ? self : this, function () {
    'use strict';

    const TICKRATE = 64;

    // World units. Sources are the community measurements quoted in the CS2
    // sound-radius table; the CS:GO numbers they derive from were measured at
    // ~1086u (running), ~1062u (landing), ~1100u (reload), ~3937u (gunshot).
    const RADII = {
        footstep:   1100,   // running
        gunshot:    4000,   // unsuppressed - no audible layer beyond ~4000u
        gunshot_sil: 900,   // suppressed - no distant layer at all
        explosion:  3500,   // HE / decoy end / flashbang detonation
        fire:        600,   // molotov burn loop is local; the BOUNCE is loud, the fire is not
        bomb:       1100,   // plant and defuse are explicitly non-silenceable
    };

    // How long a one-shot sound stays drawn, in ticks. These are display
    // decays, not physical durations: the point is to make an instantaneous
    // event visible while scrubbing, not to claim the sound lasted this long.
    const DECAY = {
        gunshot:   Math.round(0.35 * TICKRATE),
        explosion: Math.round(0.9 * TICKRATE),
        bomb:      Math.round(1.2 * TICKRATE),
    };

    // Suppressed weapons. The USP-S and M4A1-S are the only silenced primaries
    // in CS2; the parser emits both the `weapon_` form and the bare name
    // depending on the surface, so both are matched.
    const SILENCED = /(usps|usp_silencer|m4a1_silencer)/;

    /** Audible radius of one shot, in world units. */
    function shotRadius(weapon) {
        const w = String(weapon || '').toLowerCase();
        return SILENCED.test(w) ? RADII.gunshot_sil : RADII.gunshot;
    }

    /**
     * Movement audibility for one player.
     *
     * CS2 has a global silent-movement cutoff (~135 u/s): below it, movement
     * makes no sound at all. Above it the sound does not grow - a footstep is
     * a footstep - so the radius is flat and only the fade-in near the
     * threshold varies, which keeps the ring from popping on and off as a
     * walking player crosses the cutoff.
     *
     * Returns null when the player is inaudible.
     */
    function movement(speed, opts) {
        opts = opts || {};
        const cutoff = opts.cutoff === undefined ? 135 : opts.cutoff;
        const margin = opts.margin === undefined ? 20 : opts.margin;
        if (speed == null || !isFinite(speed)) return null;
        if (speed < cutoff - margin) return null;
        const fade = Math.max(0, Math.min(1, (speed - (cutoff - margin)) / margin));
        return { radius: RADII.footstep, alpha: fade, kind: 'footstep' };
    }

    /** Linear fade from 1 at the event tick to 0 at tick+decay. */
    function decayAlpha(tick, eventTick, decayTicks) {
        if (eventTick == null || tick < eventTick) return 0;
        const age = tick - eventTick;
        if (age >= decayTicks) return 0;
        return 1 - age / decayTicks;
    }

    /**
     * Every non-movement sound audible at `tick`.
     *
     * Inputs are the viewer's own event arrays, all optional - a match parsed
     * before a given array existed simply contributes nothing rather than
     * throwing. Movement is handled per player by movement() above, since it
     * needs the caller's smoothed speed.
     *
     * `roundStart` scopes the scan the same way the ground-item and bomb
     * layers are scoped: a gunshot from the previous round is not audible now.
     */
    function events(opts) {
        opts = opts || {};
        const tick = opts.tick || 0;
        const from = opts.roundStart == null ? -Infinity : opts.roundStart;
        const out = [];

        const push = (src, radius, kind, eventTick, decay) => {
            if (eventTick == null || eventTick < from) return;
            const a = decayAlpha(tick, eventTick, decay);
            if (a <= 0) return;
            const x = src.X != null ? src.X : src.x;
            const y = src.Y != null ? src.Y : src.y;
            if (typeof x !== 'number' || typeof y !== 'number') return;
            out.push({ x: x, y: y, radius: radius, kind: kind, alpha: a });
        };

        (opts.shots || []).forEach(s => {
            // Shots carry the shooter's eye position; fall back to X/Y for
            // older exports that only had the feet origin.
            const src = (s.eye_x != null && s.eye_y != null)
                ? { x: s.eye_x, y: s.eye_y } : s;
            push(src, shotRadius(s.weapon), 'gunshot', s.tick, DECAY.gunshot);
        });
        (opts.he || []).forEach(g => push(g, RADII.explosion, 'explosion', g.tick, DECAY.explosion));
        (opts.flashes || []).forEach(g => push(g, RADII.explosion, 'explosion', g.tick, DECAY.explosion));
        (opts.bomb || []).forEach(b => {
            if (b.event !== 'plant' && b.event !== 'defuse') return;
            push(b, RADII.bomb, 'bomb', b.tick, DECAY.bomb);
        });

        return out;
    }

    return { RADII, DECAY, TICKRATE, SILENCED, shotRadius, movement, decayAlpha, events };
}));
