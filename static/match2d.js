/**
 * match2d.js - canvas glue for match2d.logic.js, shared by viewer.js and
 * multi.js (classic script, loaded after match2d.logic.js).
 */
const Match2D = (function () {
    'use strict';

    // Dashed muzzle line in the facing direction, at a base-canvas-px position.
    function drawSpray(ctx, pos, color, yaw, dim = 1) {
        const r = 7, lineLength = 25;
        const angleRad = (-yaw) * (Math.PI / 180);
        ctx.save();
        ctx.beginPath();
        ctx.moveTo(pos.x + Math.cos(angleRad) * r,               pos.y + Math.sin(angleRad) * r);
        ctx.lineTo(pos.x + Math.cos(angleRad) * (r + lineLength), pos.y + Math.sin(angleRad) * (r + lineLength));
        ctx.setLineDash([4, 3]);
        ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.globalAlpha = 0.9 * dim; ctx.stroke();
        ctx.restore();
    }

    return { drawSpray };
}());
