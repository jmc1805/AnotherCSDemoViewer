/**
 * utilfx.js - canvas rendering for game-faithful utility visuals, shared by
 * viewer.js and multi.js (classic script, loaded after utilfx.logic.js).
 *
 * All coordinates/radii passed in are BASE canvas px (the un-zoomed 1024²
 * image space worldToCanvas() returns) - callers convert world units with
 * their own mapConfig.scale and apply the Viewport transform before calling.
 * Ticks are whatever timeline the caller uses (absolute or round-relative);
 * they only drive animation phase.
 *
 * Visuals:
 *  • drawSmokeCloud - layered drifting blobs that read as a volume, blooming
 *    in over ~1 s; HE holes (from UtilFXLogic.smokeHoles) carve blobs away
 *    and the cloud visibly refills as the holes lose strength.
 *  • drawFire - flame tongues spreading outward from the impact point; the
 *    footprint radius comes from the caller (UtilFXLogic.fireSpreadRadius),
 *    with a rim tint distinguishing CT incendiary (blue-white) from T
 *    molotov (deep orange) and separate cell rendering when the parser's
 *    additive `cells` ignition timeline is present.
 */
(function () {
    'use strict';
    const L = window.UtilFXLogic;

    // layout cache: seed -> blobs (layouts are deterministic, so cache freely)
    const layoutCache = new Map();
    function layout(seed, count) {
        const key = seed * 64 + count;
        let b = layoutCache.get(key);
        if (!b) { b = L.blobLayout(seed, count); layoutCache.set(key, b); }
        return b;
    }

    /**
     * opts: { x, y, rPx, tick, seed, bloom (0-1), fade (0-1),
     *         holes: [{x, y, r, strength}] in base px, label?: bool }
     */
    function drawSmokeCloud(ctx, opts) {
        const { x, y, rPx, tick, seed } = opts;
        const bloom = opts.bloom == null ? 1 : opts.bloom;
        const fade = opts.fade == null ? 1 : opts.fade;
        const holes = opts.holes || [];
        const alpha = bloom * fade;
        if (alpha <= 0.01 || rPx <= 0) return;

        const blobs = layout(seed, 9);
        const swirl = tick * 0.0012;                       // slow whole-cloud swirl
        const cos = Math.cos(swirl), sin = Math.sin(swirl);

        // pass 1: body blobs (overlap builds up density toward the core)
        for (const b of blobs) {
            const wob = 1 + 0.06 * Math.sin(tick / 26 + b.phase);
            const dx = (b.dx * cos - b.dy * sin) * rPx * 0.85;
            const dy = (b.dx * sin + b.dy * cos) * rPx * 0.85;
            const bx = x + dx, by = y + dy;
            const carve = L.blobCarve(bx, by, holes);
            const br = b.r * rPx * bloom * wob * carve;
            if (br < 0.5) continue;
            ctx.beginPath();
            ctx.arc(bx, by, br, 0, Math.PI * 2);
            ctx.fillStyle = `rgba(168,205,172,${(0.34 * fade * carve).toFixed(3)})`;
            ctx.fill();
        }
        // pass 2: brighter core (also carveable - an HE at the center guts it)
        const coreCarve = L.blobCarve(x, y, holes);
        if (coreCarve > 0.05) {
            ctx.beginPath();
            ctx.arc(x, y, rPx * 0.45 * bloom * coreCarve, 0, Math.PI * 2);
            ctx.fillStyle = `rgba(200,228,202,${(0.30 * alpha * coreCarve).toFixed(3)})`;
            ctx.fill();
        }
        // soft boundary ring so the true occlusion edge stays readable
        ctx.beginPath();
        ctx.arc(x, y, rPx * bloom, 0, Math.PI * 2);
        ctx.strokeStyle = `rgba(190,230,195,${(0.35 * alpha).toFixed(3)})`;
        ctx.lineWidth = 1.2;
        ctx.stroke();

        if (opts.label && alpha > 0.35) {
            ctx.fillStyle = `rgba(215,245,220,${(0.8 * alpha).toFixed(3)})`;
            ctx.font = '9px sans-serif';
            ctx.textAlign = 'center';
            ctx.fillText('SMOKE', x, y + 3);
            ctx.textAlign = 'left';   // restore - shared ctx, other callers assume left-aligned
        }
    }

    const FIRE_RIM = {
        Molotov:    'rgba(255,110,30,',    // T - deep orange rim
        Incendiary: 'rgba(150,200,255,',   // CT - blue-white rim
    };

    /**
     * opts: { x, y, rPx (current spread radius in px), tick, seed,
     *         weapon: 'Molotov'|'Incendiary', alpha (0-1),
     *         cells?: [{x, y}] base-px ignited fire cells (new parses) }
     */
    function drawFire(ctx, opts) {
        const { x, y, rPx, tick, seed } = opts;
        const alpha = opts.alpha == null ? 1 : opts.alpha;
        if (alpha <= 0.01 || rPx <= 0) return;
        const rim = FIRE_RIM[opts.weapon] || FIRE_RIM.Molotov;

        // ground scorch under the flames
        ctx.beginPath();
        ctx.arc(x, y, rPx, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(60,25,5,${(0.30 * alpha).toFixed(3)})`;
        ctx.fill();

        if (opts.cells && opts.cells.length) {
            // real per-cell geometry from the parser's ignition timeline
            for (let i = 0; i < opts.cells.length; i++) {
                const c = opts.cells[i];
                flame(ctx, c.x, c.y, Math.max(3.5, rPx * 0.16), tick, i, alpha);
            }
        } else {
            // approximation: flame tongues inside the current spread radius
            const blobs = layout(seed, 7);
            for (let i = 0; i < blobs.length; i++) {
                const b = blobs[i];
                flame(ctx, x + b.dx * rPx * 0.8, y + b.dy * rPx * 0.8,
                      b.r * rPx * 0.55, tick, i + b.phase, alpha);
            }
            flame(ctx, x, y, rPx * 0.4, tick, 2.7, alpha);   // core flame
        }

        // weapon-tinted rim marks the true damage boundary (and CT vs T)
        ctx.beginPath();
        ctx.arc(x, y, rPx, 0, Math.PI * 2);
        ctx.strokeStyle = rim + (0.75 * alpha).toFixed(3) + ')';
        ctx.lineWidth = 1.6;
        ctx.stroke();
    }

    // one flickering flame: outer orange + inner yellow
    function flame(ctx, fx, fy, fr, tick, phase, alpha) {
        const flick = 1 + 0.16 * Math.sin(tick / 3.1 + phase * 5.13);
        const r = fr * flick;
        if (r < 0.5) return;
        ctx.beginPath();
        ctx.arc(fx, fy, r, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(255,95,20,${(0.5 * alpha).toFixed(3)})`;
        ctx.fill();
        ctx.beginPath();
        ctx.arc(fx, fy, r * 0.55, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(255,210,80,${(0.75 * alpha).toFixed(3)})`;
        ctx.fill();
    }

    window.UtilFX = { drawSmokeCloud, drawFire };
})();
