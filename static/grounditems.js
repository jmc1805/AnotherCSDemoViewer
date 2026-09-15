/**
 * grounditems.js - canvas draw for weapons/utility dropped on the ground
 * (2D viewer + multi.js). World-space icons drawn BEFORE players in the
 * render order, so a dropped item never sits on top of a player dot or
 * other HUD elements - only a player standing exactly on it can cover it.
 * Uses Assets equipment icons (static/assets.js) when static/assets/ is
 * present, else a short text tag, same fallback idiom as static/killfeed.js.
 */
(function () {
    'use strict';

    function assetIcon(weapon) {
        if (!window.Assets || !Assets.available()) return null;
        return Assets.img('equipment', weapon);
    }

    // Explicit labels for keys the generic rule would mangle. 'item_defuser'
    // has no weapon_ prefix, so the generic path would render it as "ITEM".
    const LABELS = { item_defuser: 'KIT' };

    function shortLabel(weapon) {
        const k = (weapon || '').toLowerCase();
        if (LABELS[k]) return LABELS[k];
        return (weapon || '').replace('weapon_', '').replace(/_/g, '').toUpperCase().slice(0, 4);
    }

    // opts: { items, worldToCanvas(x,y)->{x,y}, isOnCanvas(pos)->bool,
    //         size?: px icon height (default 14), alpha?: 0..1 (default 0.9),
    //         labelFn?: (weapon) => string }
    function draw(ctx, opts) {
        const items = opts.items;
        if (!items || !items.length) return;
        const size = opts.size || 14;
        const alpha = opts.alpha != null ? opts.alpha : 0.9;
        const labelFn = opts.labelFn || shortLabel;

        ctx.save();
        ctx.globalAlpha = alpha;
        for (const it of items) {
            if (it.X == null || it.Y == null) continue;
            const pos = opts.worldToCanvas(it.X, it.Y);
            if (opts.isOnCanvas && !opts.isOnCanvas(pos)) continue;

            const im = assetIcon(it.weapon);
            if (im && im.complete && im.naturalWidth) {
                const h = size;
                const w = h * (im.naturalWidth / im.naturalHeight);
                ctx.drawImage(im, pos.x - w / 2, pos.y - h / 2, w, h);
            } else {
                const label = labelFn(it.weapon);
                ctx.font = 'bold 9px ' + FmtLogic.FONT_UI;
                ctx.textAlign = 'center';
                ctx.textBaseline = 'middle';
                const padX = 3, padY = 2;
                const tw = ctx.measureText(label).width;
                ctx.fillStyle = 'rgba(0,0,0,0.55)';
                ctx.fillRect(pos.x - tw / 2 - padX, pos.y - 6 - padY, tw + padX * 2, 12 + padY * 2);
                ctx.fillStyle = '#e8e8e8';
                ctx.fillText(label, pos.x, pos.y);
            }
        }
        ctx.restore();
    }

    window.GroundItems = { draw };
}());
