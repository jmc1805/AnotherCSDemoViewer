/**
 * clipbox.js - the framed "In-game clip" box and its open/close state.
 *
 * The recorded CS2 clip (clip_record.py) is shown next to the 2D Instant
 * Replay preview as a matched pair, sized from the --clip-w / --clip-h vars
 * on the media row that holds both. Player Highlights and the match page's
 * Overwatch tab drive the exact same behaviour, so it lives here once instead
 * of being reimplemented per page:
 *
 *   - a clip already on disk starts closed - the row's record button reads
 *     "show clip" and displays it on demand, so opening a page never opens a
 *     video by itself,
 *   - closing a clip only hides it; the .mp4 stays on disk, so the button
 *     goes back to "show clip" and re-displays it for free rather than
 *     spending another recording,
 *   - the close control sits in the row's button strip next to
 *     preview/record, not inside the box.
 *
 * Pages own the CSS (.clip-box, .clip-video, …) - same convention as the
 * other shared UI modules.
 */
const ClipBox = (() => {
    'use strict';

    const esc = FmtLogic.esc;

    // `saved` marks a clip restored from disk rather than just recorded.
    function html(url, caption, warning, saved) {
        const head = saved
            ? '<span class="dot"></span>In-game clip <span class="clip-saved">· saved</span>'
            : '<span class="dot"></span>In-game clip';
        return `<div class="clip-box">
            <div class="clip-box-head">${head}</div>
            <video class="clip-video" controls preload="metadata" src="${esc(url)}"></video>
            ${caption ? `<div class="clip-caption">${esc(caption)}</div>` : ''}
            ${warning ? `<div class="clip-warn">⚠ ${esc(warning)}</div>` : ''}
        </div>`;
    }

    // Show a clip in `mount`, remembering its URL on the record button so a
    // later "show clip" can bring it back without re-recording.
    function show(mount, opts) {
        const { url, caption, warning, saved, recordBtn, closeBtn } = opts;
        if (!mount) return;
        mount.innerHTML = html(url, caption, warning, saved);
        if (recordBtn) {
            recordBtn.dataset.clipUrl = url;
            recordBtn.dataset.closed = '';
            recordBtn.textContent = '🎥 re-record';
        }
        if (closeBtn) closeBtn.style.display = '';
    }

    // A clip that already exists on disk: remember it on the record button
    // and leave the box closed. Pages call this on load - opening a page
    // shouldn't start a video playing on its own; the row says "show clip"
    // and the viewer decides.
    function prime(opts) {
        const { url, recordBtn, closeBtn } = opts;
        if (recordBtn) {
            recordBtn.dataset.clipUrl = url;
            recordBtn.dataset.closed = '1';
            recordBtn.textContent = '🎥 show clip';
        }
        if (closeBtn) closeBtn.style.display = 'none';
    }

    function close(mount, opts) {
        const { recordBtn, closeBtn } = opts || {};
        if (mount) mount.innerHTML = '';
        if (recordBtn && recordBtn.dataset.clipUrl) {
            recordBtn.dataset.closed = '1';
            recordBtn.textContent = '🎥 show clip';
        }
        if (closeBtn) closeBtn.style.display = 'none';
    }

    // Canvas size for the 2D preview mounted beside a clip: the same
    // --clip-w / --clip-h the video uses, so the two boxes match exactly.
    function previewSize(el) {
        const cs = getComputedStyle(el.closest('[style*="--clip-w"], .hl-media, .ow-media') || el);
        return {
            width:  Math.round(parseFloat(cs.getPropertyValue('--clip-w')) || 360),
            height: Math.round(parseFloat(cs.getPropertyValue('--clip-h')) || 203),
        };
    }

    return { html, show, prime, close, previewSize };
})();
