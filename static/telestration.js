/**
 * telestration.js - analytical drawing overlay. A separate <canvas> stacked
 * over the map so user strokes never repaint with playback. Tools: pen / line / arrow / rect, a colour, and clear. When no tool is
 * active the overlay is click-through (pointer-events:none) so pan/zoom works.
 *
 * Strokes are stored in BASE (un-zoomed 1024² map) coordinates and redrawn
 * under the shared viewport transform, so an arrow drawn on B site stays on B
 * site when you zoom or pan. They used to be stored in overlay pixel space,
 * which meant every annotation slid off whatever it was pointing at the moment
 * the view moved - the one thing a telestration must not do.
 *
 * Pass `opts.getView()` returning the viewport state shared with the map canvas
 * (static/viewport.logic.js). Without it the overlay falls back to the old
 * fixed-to-the-canvas behaviour, so a host with no pan/zoom needs no changes.
 *
 * Line widths are divided by the view scale before stroking, so a zoomed-in
 * annotation stays the same thickness on screen rather than growing into a
 * blob.
 *
 * Reused by viewer.js and (later) multi.js. No dependency beyond the DOM.
 */
(function () {
    'use strict';

    function toCanvas(canvas, ev) {
        const r = canvas.getBoundingClientRect();
        return {
            x: (ev.clientX - r.left) * (canvas.width / r.width),
            y: (ev.clientY - r.top) * (canvas.height / r.height),
        };
    }

    function create(canvas, opts) {
        opts = opts || {};
        const ctx = canvas.getContext('2d');
        const strokes = [];          // {tool, color, pts:[{x,y}]}  in BASE coords
        let tool = null;             // null → inactive (click-through)
        let color = '#ff3b3b';
        let cur = null;

        const IDENTITY = { scale: 1, tx: 0, ty: 0 };
        const getView = () => (opts.getView && opts.getView()) || IDENTITY;

        // Screen (canvas-internal) → base. ViewportLogic when the host shares a
        // view; otherwise the identity, i.e. the old behaviour.
        function toBase(ev) {
            const p = toCanvas(canvas, ev);
            const v = getView();
            if (typeof ViewportLogic !== 'undefined' && opts.getView)
                return ViewportLogic.screenToBase(v, p.x, p.y);
            return p;
        }

        function setActive(on) {
            canvas.style.pointerEvents = on ? 'auto' : 'none';
            canvas.style.cursor = on ? 'crosshair' : '';
        }
        setActive(false);

        function redraw() {
            ctx.setTransform(1, 0, 0, 1, 0, 0);
            ctx.clearRect(0, 0, canvas.width, canvas.height);
            const v = getView();
            ctx.setTransform(v.scale, 0, 0, v.scale, v.tx, v.ty);
            for (const s of strokes) drawStroke(s, v.scale);
            if (cur) drawStroke(cur, v.scale);
            ctx.setTransform(1, 0, 0, 1, 0, 0);
        }

        function drawStroke(s, scale) {
            const p = s.pts;
            if (p.length < 1) return;
            const k = 1 / (scale || 1);      // keep screen-constant thickness
            ctx.save();
            ctx.strokeStyle = s.color; ctx.fillStyle = s.color;
            ctx.lineWidth = 3 * k; ctx.lineJoin = 'round'; ctx.lineCap = 'round';
            ctx.beginPath();
            if (s.tool === 'pen') {
                ctx.moveTo(p[0].x, p[0].y);
                for (let i = 1; i < p.length; i++) ctx.lineTo(p[i].x, p[i].y);
                ctx.stroke();
            } else {
                const a = p[0], b = p[p.length - 1];
                if (s.tool === 'rect') {
                    ctx.strokeRect(a.x, a.y, b.x - a.x, b.y - a.y);
                } else { // line or arrow
                    ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
                    if (s.tool === 'arrow') {
                        const ang = Math.atan2(b.y - a.y, b.x - a.x), h = 14 * k;
                        ctx.beginPath();
                        ctx.moveTo(b.x, b.y);
                        ctx.lineTo(b.x - h * Math.cos(ang - 0.4), b.y - h * Math.sin(ang - 0.4));
                        ctx.lineTo(b.x - h * Math.cos(ang + 0.4), b.y - h * Math.sin(ang + 0.4));
                        ctx.closePath(); ctx.fill();
                    }
                }
            }
            ctx.restore();
        }

        const onDown = (ev) => {
            if (!tool || ev.button !== 0) return;
            ev.preventDefault();
            cur = { tool, color, pts: [toBase(ev)] };
            try { canvas.setPointerCapture(ev.pointerId); } catch (e) { /* ignore */ }
        };
        const onMove = (ev) => {
            if (!cur) return;
            const pt = toBase(ev);
            if (cur.tool === 'pen') cur.pts.push(pt);
            else cur.pts[1] = pt;                       // line/arrow/rect: just endpoint
            redraw();
        };
        const onUp = (ev) => {
            if (!cur) return;
            if (cur.pts.length >= (cur.tool === 'pen' ? 2 : 2)) strokes.push(cur);
            cur = null; redraw();
            try { canvas.releasePointerCapture(ev.pointerId); } catch (e) { /* ignore */ }
        };
        canvas.addEventListener('pointerdown', onDown);
        canvas.addEventListener('pointermove', onMove);
        canvas.addEventListener('pointerup', onUp);
        canvas.addEventListener('pointercancel', onUp);

        return {
            setTool(t) { tool = t; setActive(!!t); },
            getTool() { return tool; },
            setColor(c) { color = c; },
            undo() { strokes.pop(); redraw(); },
            clear() { strokes.length = 0; cur = null; redraw(); },
            isEmpty() { return strokes.length === 0; },
            redraw,
        };
    }

    window.Telestration = { create };
}());
