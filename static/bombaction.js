/**
 * bombaction.js - the canvas half of the plant/defuse cue.
 *
 * Pairs with static/bombaction.logic.js, the same way utilfx.js pairs with
 * utilfx.logic.js and grounditems.js with grounditems.logic.js: the .logic
 * file answers "what is happening and how far along is it", this one draws
 * it, and neither page owns the picture.
 *
 * IT EXISTS BECAUSE THERE WERE TWO COPIES. The replayer drew the ring at
 * radius 19 / 3px around the acting player; the analyser drew the same idea
 * at radius 11 / 2.5px around the bomb. Same arc, same sweep, same two
 * colours, drawn at two sizes on two pages - which is the drift that produced
 * three disagreeing copies of the moment row before momentrow.css was pulled
 * out. The analyser's proportions won (they read better against a 7px player
 * dot and clear the replayer's 16px lock ring), and now there is one of them.
 *
 * The ANCHOR still differs by page, deliberately, and that is not a drift:
 * the replayer centres the ring on the acting player, because it follows one
 * round and "who is doing this" is the question; the analyser centres it on
 * the bomb, because it can be showing ten rounds at once and the bomb is what
 * each of them has one of. The parser records an action's position as the
 * bomb's own and a defuser stands on the bomb, so the two anchors land within
 * a few units of each other anyway.
 */
const BombAction = (() => {
    'use strict';

    // Geometry, shared so the two pages cannot drift apart again.
    const RING_RADIUS = 11;
    const RING_WIDTH = 2.5;
    // A full dark circle under the arc, so the ring reads as a gauge with a
    // remainder rather than as an arc that happens to be short.
    const RING_BACKING = 'rgba(0,0,0,0.55)';
    // Sweep from 12 o'clock, clockwise - the direction every progress dial
    // in the app turns.
    const RING_START = -Math.PI / 2;

    const COLORS = { plant: '#ff8c00', defuse: '#3ddc84' };

    /** The cue colour for an action kind. Also used by the replayer's HUD
     *  strip, so the strip and the ring can never disagree. */
    function ringColor(kind) {
        return COLORS[kind] || COLORS.defuse;
    }

    /**
     * Draw one progress ring.
     *
     * @param {CanvasRenderingContext2D} ctx
     * @param {Object} o
     * @param {number} o.x  canvas x of the anchor
     * @param {number} o.y  canvas y
     * @param {string} o.kind      'plant' | 'defuse'
     * @param {number} o.progress  0..1, from BombActionLogic.actionAt()
     * @param {number} [o.radius]  override for a surface that needs it
     *
     * Wrapped in save()/restore() and sets no globalAlpha of its own: the
     * replayer calls this from drawPlayerMarker(), which owns globalAlpha for
     * the lock pulse and the death cross, and a value set here would either
     * be overwritten or leak into those.
     */
    function drawRing(ctx, o) {
        const R = o.radius || RING_RADIUS;
        const p = Math.max(0, Math.min(1, o.progress || 0));
        ctx.save();
        ctx.lineWidth = RING_WIDTH;
        ctx.lineCap = 'butt';
        ctx.strokeStyle = RING_BACKING;
        ctx.beginPath();
        ctx.arc(o.x, o.y, R, 0, Math.PI * 2);
        ctx.stroke();
        ctx.strokeStyle = ringColor(o.kind);
        ctx.beginPath();
        ctx.arc(o.x, o.y, R, RING_START, RING_START + p * Math.PI * 2);
        ctx.stroke();
        ctx.restore();
    }

    return { drawRing, ringColor, COLORS, RING_RADIUS, RING_WIDTH };
})();
