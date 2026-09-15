/**
 * demojump.js - the "open this moment in CS2" button behaviour.
 *
 * POSTs /demo/watch, which launches the original .dem in CS2 - plain, with no
 * HLAE and nothing injected - and then drives the game to the moment over its
 * own console socket, with a key bind as the fallback (see clip_record.py's
 * watch mode). Four callers share it: the match page's kill log, its Highlights rows, its
 * Overwatch moment rows, and the 2D replayer's control bar - so the
 * disabled-while-launching handling and the result message live here once
 * instead of four times.
 *
 * The result is a transient toast rather than inline text: the replayer's
 * control bar has nowhere to put a message, and a launch takes a few seconds
 * during which the page the user clicked from may well be scrolled away.
 *
 *   DemoJump.launch({ match, tick, focusPlayer, button })
 *
 * Pages own the button markup and styling (each of the four matches its own
 * surrounding controls); this module only disables it while in flight.
 */
const DemoJump = (() => {
    'use strict';

    let toastEl = null;
    let toastTimer = null;

    function toast(message, isError) {
        if (!toastEl) {
            toastEl = document.createElement('div');
            toastEl.className = 'demojump-toast';
            // Inline so every page gets it without importing a stylesheet -
            // there are only two states and they never vary per page.
            toastEl.style.cssText = 'position:fixed;right:18px;bottom:18px;z-index:9999;'
                + 'max-width:min(420px,80vw);padding:10px 14px;border-radius:var(--radius-md);'
                + "font-family:var(--font-mono);font-size:12px;line-height:1.5;"
                + 'background:#111820;border:1px solid #253045;color:#c8d4e0;'
                + 'box-shadow:0 6px 24px rgba(0,0,0,.5);cursor:pointer';
            toastEl.addEventListener('click', () => { toastEl.style.display = 'none'; });
            document.body.appendChild(toastEl);
        }
        toastEl.style.display = '';
        toastEl.style.borderColor = isError ? '#ff4455' : '#253045';
        toastEl.style.color = isError ? '#ff8892' : '#c8d4e0';
        toastEl.textContent = message;
        clearTimeout(toastTimer);
        // Errors are the ones worth reading (usually "not configured" with a
        // pointer to Settings), so they linger; a success message is just an
        // acknowledgement that CS2 is starting.
        toastTimer = setTimeout(() => { toastEl.style.display = 'none'; },
                                isError ? 15000 : 9000);
    }

    function launch(opts) {
        const { match, tick, focusPlayer, button } = opts || {};
        if (!match || tick == null) return;
        if (button) {
            if (button.disabled) return;
            button.disabled = true;
            button.dataset.label = button.dataset.label || button.textContent;
            button.textContent = '⏳ starting…';
        }
        const done = () => {
            if (!button) return;
            button.disabled = false;
            button.textContent = button.dataset.label;
        };
        fetch('/demo/watch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ match, tick: Math.max(0, Math.round(tick)),
                                   focus_player: focusPlayer || '' }),
        })
            .then(r => r.json())
            .then(d => {
                toast(d.ok ? d.message : d.error, !d.ok);
                done();
            })
            .catch(() => { toast('Could not reach the server.', true); done(); });
    }

    return { launch, toast };
})();
