/**
 * viewport.js - browser glue for the shared pan/zoom view transform.
 * Binds wheel-zoom-at-cursor, drag-to-pan
 * and double-click-to-reset on a canvas, and applies the transform to a 2D
 * context around world-space drawing. Pure math lives in viewport.logic.js.
 *
 * Wheel ownership: the canvas takes the wheel only while the view is zoomed in
 * or a zoom modifier (Ctrl/Cmd/Shift) is held. At the default view a bare wheel
 * is passed to the page, so a canvas that fills a small window can be scrolled
 * past instead of trapping the page behind it.
 *
 * Perf: no per-frame allocation - the transform is a single setTransform() call
 * per frame; interaction handlers only mutate the shared view object and ask the
 * caller to redraw. Reused verbatim by viewer.js and (later) multi.js.
 */
(function () {
    'use strict';
    const L = (typeof ViewportLogic !== 'undefined') ? ViewportLogic : null;

    // Map a DOM mouse/pointer event to canvas-internal (screen) coordinates,
    // accounting for CSS scaling of the fixed-resolution canvas buffer.
    function eventToCanvas(canvas, ev) {
        const r = canvas.getBoundingClientRect();
        return {
            x: (ev.clientX - r.left) * (canvas.width / r.width),
            y: (ev.clientY - r.top) * (canvas.height / r.height),
        };
    }

    // A transient "hold Ctrl to zoom" note, shown the first few times a wheel
    // event is handed back to the page. Created lazily inside the canvas's
    // positioned wrapper (#map-wrap on both canvas pages) so no page has to
    // add markup for it. Purely advisory - it never takes pointer events.
    function makeHint(canvas) {
        let el = null, timer = 0, shown = 0;
        function show() {
            if (shown >= HINT_MAX_SHOWS) return;
            const host = canvas.parentNode;
            if (!host) return;
            if (!el) {
                el = document.createElement('div');
                el.className = 'vp-zoom-hint';
                el.textContent = 'Hold Ctrl (or ⇧) and scroll to zoom';
                el.style.cssText = [
                    'position:absolute', 'left:50%', 'top:12px', 'transform:translateX(-50%)',
                    'padding:5px 11px', 'border-radius:999px', 'pointer-events:none',
                    'background:rgba(10,12,15,.86)', 'color:#c8d0dc',
                    'font-size:11px', 'letter-spacing:1px', 'white-space:nowrap',
                    'border:1px solid rgba(255,255,255,.14)', 'z-index:20',
                    'opacity:0', 'transition:opacity .18s',
                ].join(';');
                host.appendChild(el);
            }
            shown++;
            el.style.opacity = '1';
            clearTimeout(timer);
            timer = setTimeout(() => { if (el) el.style.opacity = '0'; }, 1400);
        }
        function destroy() {
            clearTimeout(timer);
            if (el && el.parentNode) el.parentNode.removeChild(el);
            el = null;
        }
        return { show, destroy };
    }

    // How many times the passthrough note is shown before it stops nagging.
    const HINT_MAX_SHOWS = 3;

    // Bind pan/zoom on `canvas` mutating `view`; calls onChange() after any change
    // (the caller redraws - needed while paused, harmless while playing). Returns
    // { unbind, isPanning() }.
    function bindPanZoom(canvas, view, onChange) {
        const ZOOM_STEP = 1.15;
        let panning = false, lastX = 0, lastY = 0, movedPx = 0;
        const hint = makeHint(canvas);

        // Ctrl/Cmd/Shift all mean "this wheel is a zoom".
        const zoomIntent = (ev) => ev.ctrlKey || ev.metaKey || ev.shiftKey;

        const onWheel = (ev) => {
            // The canvas used to preventDefault EVERY wheel event, so on a
            // window small enough for the map to fill the viewport there was
            // no way to scroll the page back up past it - the whole page was
            // trapped behind the zoom. The map claims the wheel only when it
            // is actually being used as a map: while it is already zoomed in,
            // or while a zoom modifier is held. At the default view a bare
            // wheel is what it looks like - a page scroll.
            if (!zoomIntent(ev) && L.isReset(view)) { hint.show(); return; }
            ev.preventDefault();
            const p = eventToCanvas(canvas, ev);
            L.zoomAt(view, p.x, p.y, ev.deltaY < 0 ? ZOOM_STEP : 1 / ZOOM_STEP);
            L.clampPan(view, canvas.width, canvas.height);
            onChange();
        };
        const onDown = (ev) => {
            if (ev.button !== 0) return;
            panning = true; movedPx = 0;
            lastX = ev.clientX; lastY = ev.clientY;
            canvas.style.cursor = 'grabbing';
            try { canvas.setPointerCapture(ev.pointerId); } catch (e) { /* older browsers */ }
        };
        const onMove = (ev) => {
            if (!panning) return;
            const r = canvas.getBoundingClientRect();
            const dx = (ev.clientX - lastX) * (canvas.width / r.width);
            const dy = (ev.clientY - lastY) * (canvas.height / r.height);
            movedPx += Math.abs(dx) + Math.abs(dy);
            lastX = ev.clientX; lastY = ev.clientY;
            L.panBy(view, dx, dy);
            L.clampPan(view, canvas.width, canvas.height);
            onChange();
        };
        const onUp = (ev) => {
            if (!panning) return;
            panning = false;
            canvas.style.cursor = 'grab';
            try { canvas.releasePointerCapture(ev.pointerId); } catch (e) { /* ignore */ }
        };
        const onDblClick = (ev) => { ev.preventDefault(); L.reset(view); onChange(); };

        canvas.addEventListener('wheel', onWheel, { passive: false });
        canvas.addEventListener('pointerdown', onDown);
        canvas.addEventListener('pointermove', onMove);
        canvas.addEventListener('pointerup', onUp);
        canvas.addEventListener('pointercancel', onUp);
        canvas.addEventListener('dblclick', onDblClick);
        canvas.style.cursor = 'grab';

        return {
            isPanning: () => panning,
            // Whether the last press was a drag (vs a click) - lets callers that
            // also want click-to-select ignore the click at the end of a pan.
            didDrag: () => movedPx > 4,
            unbind() {
                canvas.removeEventListener('wheel', onWheel);
                canvas.removeEventListener('pointerdown', onDown);
                canvas.removeEventListener('pointermove', onMove);
                canvas.removeEventListener('pointerup', onUp);
                canvas.removeEventListener('pointercancel', onUp);
                canvas.removeEventListener('dblclick', onDblClick);
                canvas.style.cursor = '';
                hint.destroy();
            },
        };
    }

    // Set the 2D context transform for world-space drawing (background + entities).
    function apply(ctx, view) { ctx.setTransform(view.scale, 0, 0, view.scale, view.tx, view.ty); }
    // Reset to identity for screen-space HUD (killfeed, flash overlay, scoreboard).
    function clear(ctx) { ctx.setTransform(1, 0, 0, 1, 0, 0); }

    window.Viewport = { bindPanZoom, apply, clear, eventToCanvas };
}());
