/**
 * killfeed.js - shared in-canvas kill feed.
 * Draws the most recent kills at the top-right of a canvas, each fading out over
 * a few seconds. Screen space - call it AFTER the pan/zoom transform is cleared.
 * Reused by viewer.js and multi.js.
 *
 * Perf: no per-frame allocation beyond a tiny fixed-size row list; weapon glyphs
 * and kill-condition glyphs (headshot/wallbang/noscope/smoke/blind/air/suicide)
 * come from the decode-once Assets cache when static/assets/ is present, else a
 * bracketed text tag. Deterministic in demo tick `t` (rewind-safe, frozen on pause).
 * Condition fields (`headshot`, `wallbang`, `through_smoke`, `attacker_blind`,
 * `noscope`, `attacker_air`, `suicide`, `assister_name`, `assist_flash`) are
 * additive parser fields - absent on kills from data parsed before this feature,
 * so every lookup below guards with `k.field` and just omits the chip.
 */
(function () {
    'use strict';
    const DEFAULTS = {
        maxRows: 6,
        holdTicks: 192,     // fully opaque window (~3 s @ 64 Hz)
        fadeTicks: 384,     // gone by here (~6 s)
        margin: 10,
        rowH: 20,
        font: 'bold 12px ' + FmtLogic.FONT_NARROW,
        ctColor: '#4a9eff',
        tColor: '#ffaa22',
        sideOf: (k, who) => (who === 'a' ? k.attacker_side : k.victim_side),
    };

    // Chainable kill-condition chips, in display order, each a [flag field, deathnotice
    // key, text-fallback tag] triple. All independent - any subset can be active.
    const CONDITIONS = [
        ['headshot',      'headshot', 'HS'],
        ['wallbang',      'wallbang', 'WB'],
        ['noscope',       'noscope',  'NS'],
        ['through_smoke', 'smoke',    'SMOKE'],
        ['attacker_blind', 'blind',   'BLIND'],
        ['attacker_air',  'air',      'AIR'],
    ];

    function assetIcon(kind, key) {
        return (window.Assets && Assets.available()) ? Assets.img(kind, key) : null;
    }

    // A ready-to-draw icon segment for an already-fetched <img>, or null if it
    // hasn't finished decoding yet (falls back to the caller's text tag).
    function iconSeg(im, h) {
        if (!im || !im.complete || !im.naturalWidth) return null;
        return { img: im, w: h * (im.naturalWidth / im.naturalHeight), h };
    }

    // opts: { kills (asc by tick), tick, canvasW, weaponLabel(fn), o? overrides }
    function draw(ctx, opts) {
        const o = Object.assign({}, DEFAULTS, opts.o || {});
        const { kills, tick, canvasW } = opts;
        if (!kills || !kills.length) return;
        const label = opts.weaponLabel || ((w) => (w || '').replace('weapon_', ''));

        // Collect the last maxRows kills still within the fade window (newest last).
        const rows = [];
        for (let i = kills.length - 1; i >= 0 && rows.length < o.maxRows; i--) {
            const age = tick - kills[i].tick;
            if (age < 0) continue;
            if (age > o.fadeTicks) break;
            rows.push(kills[i]);
        }
        if (!rows.length) return;
        rows.reverse();

        ctx.save();
        ctx.textBaseline = 'middle';
        ctx.font = o.font;
        const gap = 4, iconH = 14, tagCol = 'rgba(210,220,230,0.9)', assistCol = 'rgba(180,190,200,0.85)';
        const sideCol = (s) => s === 'ct' ? o.ctColor : s === 't' ? o.tColor : '#c8d0da';

        // Vertical start of the feed. Defaults to the top margin; callers can
        // push it down (e.g. below a top-right minimap) via o.top.
        const top = o.top != null ? o.top : o.margin;
        rows.forEach((k, idx) => {
            const age = tick - k.tick;
            const alpha = age <= o.holdTicks ? 1
                : Math.max(0, 1 - (age - o.holdTicks) / (o.fadeTicks - o.holdTicks));
            if (alpha <= 0) return;
            const y = top + o.rowH * idx + o.rowH / 2;

            // Build the row as an ordered list of segments, measure each, then
            // right-align the whole thing against the canvas edge.
            const segs = [];
            const pushText = (text, color) => segs.push({ text, color, w: ctx.measureText(text).width });
            const pushIcon = (im, color) => {
                const seg = iconSeg(im, iconH);
                if (seg) segs.push(Object.assign({ color }, seg));
                return !!seg;
            };

            if (k.suicide) {
                const vName = k.victim_name || '?';
                const susIcon = assetIcon('deathnotice', 'suicide');
                if (!pushIcon(susIcon, tagCol)) pushText('(suicide) ', tagCol);
                pushText(vName, sideCol((o.sideOf(k, 'v') || '').toLowerCase()));
            } else {
                const aName = k.attacker_name || '?';
                const vName = k.victim_name || '?';
                pushText(aName, sideCol((o.sideOf(k, 'a') || '').toLowerCase()));

                // Assist (single assister; flash-assist gets the flashbang icon
                // instead of a generic "+" so it reads as *how* they helped).
                if (k.assister_name) {
                    pushText(' + ', assistCol);
                    if (k.assist_flash) pushIcon(assetIcon('equipment', 'flashbang_assist'), assistCol);
                    pushText(k.assister_name, assistCol);
                }

                // Weapon icon (equipment) when available, else the text label.
                const wIcon = assetIcon('equipment', k.weapon);
                const wSeg = iconSeg(wIcon, iconH);
                if (wSeg) segs.push(Object.assign({ color: null, alphaMul: 0.95 }, wSeg));
                else pushText('  ›  ' + label(k.weapon) + '  ›  ', tagCol);

                // Chainable kill-condition chips - every flag that's set gets its
                // own chip, in a fixed order; all can appear on the same kill.
                CONDITIONS.forEach(([field, dnKey, tag]) => {
                    if (!k[field]) return;
                    if (!pushIcon(assetIcon('deathnotice', dnKey), tagCol)) pushText('[' + tag + ']', tagCol);
                });

                pushText(vName, sideCol((o.sideOf(k, 'v') || '').toLowerCase()));
            }

            const total = segs.reduce((sum, s) => sum + s.w, 0) + gap * (segs.length - 1);
            let x = canvasW - o.margin - total;
            segs.forEach((s) => {
                ctx.globalAlpha = alpha * (s.alphaMul || 1);
                if (s.img) ctx.drawImage(s.img, x, y - s.h / 2, s.w, s.h);
                else { ctx.fillStyle = s.color; ctx.fillText(s.text, x, y); }
                x += s.w + gap;
            });
        });
        ctx.restore();
        ctx.globalAlpha = 1;
    }

    window.KillFeed = { draw, DEFAULTS, CONDITIONS };
}());
