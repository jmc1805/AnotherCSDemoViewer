/**
 * momentrow.js - DOM glue for moment rows: the inline Instant Replay preview,
 * real in-game clip recording (fire + poll), the "open in CS2" jump, and the
 * teardown a row needs when it leaves the visible set.
 *
 * Pairs with static/momentrow.logic.js (pure markup, node-tested) the same way
 * utilfx.js pairs with utilfx.logic.js. Requires, loaded first:
 *   momentrow.logic.js, clipbox.js, instantreplay.js, demojump.js
 * (`demojump.js` is feature-detected - a page without it simply has no
 * working 🎮 button rather than a thrown error.)
 *
 * Every entry point is **scope-rooted**: it takes the element containing the
 * rows, so several independent lists can live on one page (the match page's
 * Overwatch cards are one list per player). This is the contract the three
 * pre-existing copies already relied on informally; it is now explicit.
 *
 * The one behavioural generalisation over those copies: the match file is
 * read per row from `data-file`, falling back to the list-level `matchFile`
 * option. Cross-match lists (the player page's reel, the Multi Match
 * Analyser's results) need that; single-match lists are unaffected.
 */
const MomentRow = (() => {
    'use strict';

    const POLL_MS = 1500;

    const dec = s => { try { return decodeURIComponent(s || ''); } catch (e) { return s || ''; } };
    const closeBtnFor = (scope, rid) => scope.querySelector('.clip-close-btn[data-rid="' + rid + '"]');
    const recordBtnFor = (scope, rid) => scope.querySelector('.clip-record-btn[data-rid="' + rid + '"]');

    /** The match file this button acts on: per-row first, list-level fallback. */
    function fileOf(btn, opts) {
        return dec(btn.dataset.file) || (opts && opts.matchFile) || '';
    }

    /**
     * Expand/collapse one row's inline Instant Replay. Lazy - a mini viewer
     * exists only while its row is open, so a list of many moments never runs
     * many render loops at once.
     */
    function togglePreview(scope, btn, opts) {
        const o = opts || {};
        const mount = scope.querySelector('#ir-' + btn.dataset.mid);
        if (!mount) return;
        if (mount.dataset.mounted === '1') {
            if (mount._irCtrl) mount._irCtrl.destroy();
            mount._irCtrl = null;
            mount.dataset.mounted = '';
            mount.style.display = 'none';
            btn.textContent = '▶ preview';
            return;
        }
        mount.style.display = '';
        mount.dataset.mounted = '1';
        btn.textContent = '✕ close';
        // Match the in-game clip box exactly, so an open pair reads as two
        // equal panes rather than a square beside a wide rectangle. Pages that
        // never record clips don't load clipbox.js - there, fall through to
        // InstantReplay's own default size rather than requiring the dep.
        const size = (typeof ClipBox !== 'undefined') ? ClipBox.previewSize(mount) : {};
        const file = fileOf(btn, o);
        mount._irCtrl = InstantReplay.mount(mount, {
            matchFile: file,
            // Only hand over already-fetched match JSON when it IS this match -
            // a cross-match list would otherwise render one match's ticks
            // against another's radar.
            matchData: (o.matchData && (!o.matchFile || o.matchFile === file)) ? o.matchData : null,
            startTick: +btn.dataset.start,
            endTick: +btn.dataset.end,
            focusPlayer: o.focusPlayer || dec(btn.dataset.focus),
            caption: dec(btn.dataset.caption),
            width: size.width, height: size.height,
        });
    }

    /**
     * Kick off a real in-game CS2 clip recording (clip_record.py - HLAE) and
     * poll it to completion. Ends in a clear "not configured" message unless
     * this machine has CS2 + HLAE + ffmpeg installed and set in Settings.
     */
    function startRecording(scope, btn, opts) {
        const rid = btn.dataset.rid;
        const resultEl = scope.querySelector('#rec-' + rid);
        if (!resultEl || btn.disabled) return;
        const caption = dec(btn.dataset.caption);

        // A clip already on disk that the user closed: put it back rather than
        // spending a whole re-record on footage we still have.
        if (btn.dataset.closed === '1' && btn.dataset.clipUrl) {
            ClipBox.show(resultEl, {
                url: btn.dataset.clipUrl, caption, saved: true,
                warning: shortWarning(btn),
                recordBtn: btn, closeBtn: closeBtnFor(scope, rid),
            });
            return;
        }

        btn.disabled = true;
        resultEl.innerHTML = '<span class="ir-status">Starting…</span>';
        fetch('/clip/record', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                match: fileOf(btn, opts),
                start_tick: +btn.dataset.start,
                end_tick: +btn.dataset.end,
                focus_player: (opts && opts.focusPlayer) || dec(btn.dataset.focus),
            }),
        })
        .then(r => r.json())
        .then(d => {
            if (!d.ok) { fail(resultEl, btn, d.error); return; }
            pollStatus(scope, d.job_id, resultEl, btn, caption);
        })
        .catch(() => fail(resultEl, btn, 'Request failed.'));
    }

    function fail(resultEl, btn, msg) {
        resultEl.innerHTML = '<span class="ir-status clip-err">'
            + MomentRowLogic.esc(msg || 'Failed.') + '</span>';
        btn.disabled = false;
    }

    function pollStatus(scope, jobId, resultEl, btn, caption) {
        fetch('/clip/status/' + jobId).then(r => r.json()).then(d => {
            if (!d.ok) { fail(resultEl, btn, 'Lost track of the job.'); return; }
            if (d.state === 'error') { fail(resultEl, btn, d.error); return; }
            if (d.state === 'done') {
                delete btn.dataset.clipShort;
                btn.classList.remove('clip-short');
                // Cache-bust: a re-record writes the same deterministic
                // filename, so without this the browser replays old footage.
                ClipBox.show(resultEl, {
                    url: d.clip_url + '?t=' + Date.now(), caption, warning: d.warning,
                    recordBtn: btn, closeBtn: closeBtnFor(scope, btn.dataset.rid),
                });
                btn.disabled = false;
                return;
            }
            resultEl.innerHTML = '<span class="ir-status">'
                + MomentRowLogic.esc(d.message || 'Recording…') + '</span>';
            setTimeout(() => pollStatus(scope, jobId, resultEl, btn, caption), POLL_MS);
        }).catch(() => fail(resultEl, btn, 'Lost track of the job.'));
    }

    /**
     * Attach every handler for one list, by delegation on the scope root - so
     * rows added later (a "load more", a re-query) need no re-wiring, and
     * calling wire() twice on the same root is harmless.
     */
    function wire(scope, opts) {
        const o = opts || {};
        if (scope._mrWired) { scope._mrOpts = o; return; }
        scope._mrWired = true;
        scope._mrOpts = o;

        scope.addEventListener('click', e => {
            const cur = scope._mrOpts || {};

            const prev = e.target.closest('.ir-toggle[data-mid]');
            if (prev && scope.contains(prev)) { togglePreview(scope, prev, cur); return; }

            const close = e.target.closest('.clip-close-btn[data-rid]');
            if (close && scope.contains(close)) {
                ClipBox.close(scope.querySelector('#rec-' + close.dataset.rid), {
                    recordBtn: recordBtnFor(scope, close.dataset.rid), closeBtn: close,
                });
                return;
            }

            const jump = e.target.closest('.demo-jump-btn');
            if (jump && scope.contains(jump)) {
                if (typeof DemoJump === 'undefined') return;
                DemoJump.launch({
                    match: fileOf(jump, cur), tick: +jump.dataset.tick,
                    focusPlayer: cur.focusPlayer || dec(jump.dataset.focus), button: jump,
                });
                return;
            }

            const overlay = e.target.closest('.mr-overlay-btn[data-mid]');
            if (overlay && scope.contains(overlay)) {
                if (cur.onOverlay) cur.onOverlay(overlay.dataset.mid, overlay);
                return;
            }

            // Checked last: .demo-jump-btn also carries .clip-record-btn for
            // its styling, so the jump must win the match before this does.
            const rec = e.target.closest('.clip-record-btn[data-rid]');
            if (rec && scope.contains(rec)) { startRecording(scope, rec, cur); return; }
        });
    }

    /**
     * Show "🎥 show clip" (closed) for rows whose clip already exists on disk.
     * app.py attaches `clip_url` per request when the .mp4 is there.
     */
    function primeClips(scope, rows, idPrefix) {
        (rows || []).forEach((row, i) => {
            if (!row.clip_url) return;
            const rid = (idPrefix || 'mr') + '-' + i;
            ClipBox.prime({
                url: row.clip_url,
                recordBtn: recordBtnFor(scope, rid),
                closeBtn: closeBtnFor(scope, rid),
            });
        });
    }

    /**
     * The "this clip is shorter than its moment" notice, or '' when it isn't.
     *
     * `data-clip-short` is "<actual>/<expected>" in seconds, written by
     * MomentRowLogic.rowHtml from the server's measurement of the .mp4 on
     * disk. It exists because the recorded clip and the 2D preview are sold
     * as one pair showing one moment - so when the footage doesn't cover the
     * window, the box has to say so rather than let the viewer discover a
     * missing frag by watching the play get cut off.
     *
     * ClipBox.prime() deliberately doesn't take it: nothing is rendered until
     * the box is opened, and the button already carries the ⚠.
     */
    function shortWarning(btn) {
        const raw = btn && btn.dataset ? btn.dataset.clipShort : '';
        if (!raw) return '';
        const [actual, expected] = raw.split('/').map(Number);
        if (!(expected > actual)) return '';
        return 'This recording is ' + actual.toFixed(1) + 's of a '
            + expected.toFixed(1) + 's moment, so it stops '
            + (expected - actual).toFixed(1) + 's before the 2D replay does. '
            + 'Re-record to capture the whole play.';
    }

    /**
     * Tear one row down before it leaves the visible set: a hidden
     * InstantReplay would keep its rAF loop running, and a hidden <video>
     * would keep playing audio behind a filtered-out row.
     */
    function teardownRow(scope, item) {
        if (!item) return;
        const mount = item.querySelector('.ir-mount');
        if (mount && mount.dataset.mounted === '1') {
            const btn = item.querySelector('.ir-toggle');
            if (btn) togglePreview(scope, btn, scope._mrOpts || {});
        }
        item.querySelectorAll('video').forEach(v => v.pause());
    }

    /** Tear down every row matching `sel` (default: all rows) under `scope`. */
    function teardown(scope, sel) {
        scope.querySelectorAll(sel || '.hl-item').forEach(item => teardownRow(scope, item));
    }

    return { wire, togglePreview, startRecording, primeClips, teardownRow, teardown };
})();
