/**
 * momentrow.logic.js - pure markup/derivation helpers for a "moment row":
 * one detected moment (a kill, a multi-kill, a clutch, a flagged Overwatch
 * moment, a query hit) rendered as a line you can preview, clip, jump to in
 * CS2, or open in the full replay.
 *
 * Five surfaces render this same row: the match page's Overwatch and
 * Highlights tabs, the Overwatch dashboard, a player's page, and the Multi
 * Match Analyser's results. They share one implementation of the escaping,
 * caption encoding and button set, so a row means the same thing and offers
 * the same actions wherever it turns up.
 *
 * One generalisation over a single-match list: the match file is read per row
 * from `data-file`, so a list can mix matches - which is what the analyser's
 * corpus-wide results need.
 *
 * No DOM, no fetch: unit-testable under node (test/momentrow.logic.test.mjs).
 * DOM glue (mount/record/poll/wire) lives in static/momentrow.js.
 *
 * A "row" is the object shape /match/highlights and /player/<name>/highlights
 * already return, which analysis/highlights.py produces:
 *
 *   { type, round, tick, start_tick, end_tick, label, detail,
 *     player, team, match, clip_url }
 *
 * Every field is optional - a row carrying only {tick} still renders, it just
 * offers fewer actions. That tolerance is deliberate: the analyser emits
 * utility and round moments that have no victim and no clip window.
 */
(function (root, factory) {
    if (typeof module === 'object' && module.exports) module.exports = factory(require('./fmt.logic.js'));
    else root.MomentRowLogic = factory(root.FmtLogic);
}(typeof self !== 'undefined' ? self : this, function (FmtLogic) {
    'use strict';

    const TYPE_LABEL = {
        multikill: 'Multi-kill', clutch: 'Clutch', impact_kill: 'Kill',
        kill: 'Kill', death: 'Death', opening: 'Opening', trade: 'Trade',
        flash: 'Flash', smoke: 'Smoke', molotov: 'Molotov', he: 'HE',
        bomb_plant: 'Plant', defuse: 'Defuse',
        support: 'Support', execute: 'Execute', crossfire: 'Crossfire',
    };

    // How the second player relates to the subject, per kind. A death is "by"
    // someone; a crossfire and a flash support are "on" someone.
    const OTHER_REL = { death: 'by', crossfire: 'on', support: 'on' };

    function otherRel(row) {
        return OTHER_REL[row && row.type] || 'vs';
    }

    // HTML-escape for text interpolated into a row. Re-exported so a caller
    // that already has MomentRowLogic needs nothing else; the implementation
    // is FmtLogic's, shared with every other list on the site.
    const esc = FmtLogic.esc;

    /** Previewable/clippable only if the row carries a tick window. */
    function hasWindow(row) {
        return !!row && row.start_tick != null && row.end_tick != null;
    }

    /** Jumpable (CS2 / viewer) if the row carries a tick. */
    function hasTick(row) {
        return !!row && row.tick != null;
    }

    /**
     * The one-line caption shown under a preview and burned into a recorded
     * clip. Round-trips through a data-attribute, so callers URI-encode it.
     */
    function captionFor(row) {
        // The other players belong here even though the row renders them as
        // their own cells: this string is burned into a recorded clip and read
        // back with no row around it, so a caption that says only "Trade" has
        // lost the half that mattered.
        const who = row.partner ? row.player + ' + ' + row.partner : row.player;
        const vs = row.other ? otherRel(row) + ' ' + row.other : '';
        return [who, row.label, vs, row.detail].filter(Boolean).join(' - ');
    }

    function viewerUrl(row, matchFile) {
        const f = row.match || matchFile || '';
        return '/viewer?match=' + encodeURIComponent(f) + '&tick=' + row.tick;
    }

    function typeLabel(row, labels) {
        const map = labels || TYPE_LABEL;
        return map[row.type] || row.type || '';
    }

    /**
     * Build one row's markup.
     *
     * opts:
     *   idPrefix    - namespace for the row's element ids (several lists can
     *                 coexist on one page; ids must not collide)
     *   index       - row index, appended to idPrefix
     *   matchFile   - page-level fallback when a row carries no `match`
     *   actions     - subset of ['preview','record','demo','viewer','overlay']
     *   showType    - render the type chip
     *   showMatch   - render a match column (cross-match lists)
     *   showPlayer  - render the player column (multi-player lists)
     *   showOther   - render the second player: the teammate a teamplay
     *                 moment was shared with, and the opponent it was against.
     *                 Both are omitted when the row has none, so a smoke or a
     *                 round does not sprout empty cells.
     *   typeLabels  - override map for the type chip text
     *   focusPlayer - override for whose POV a preview/clip/jump uses
     *
     * Every action button carries what it needs in data-attributes, including
     * `data-file` - so one list can mix rows from several matches, which is
     * the property the original copies did not all have.
     */
    function rowHtml(row, opts) {
        const o = opts || {};
        const idx = o.index != null ? o.index : 0;
        const mid = (o.idPrefix || 'mr') + '-' + idx;
        const file = row.match || o.matchFile || '';
        const focus = o.focusPlayer || row.player || '';
        const cap = encodeURIComponent(captionFor(row));
        const encFocus = encodeURIComponent(focus);
        const encFile = encodeURIComponent(file);
        const win = hasWindow(row);
        const actions = o.actions || ['preview', 'record', 'demo', 'viewer'];
        const has = a => actions.indexOf(a) >= 0;

        const cells = [];
        cells.push('<span class="m-round">' + (row.round != null ? 'R' + esc(row.round) : '·') + '</span>');
        if (o.showType)
            cells.push('<span class="hl-type ' + esc(row.type || '') + '">' + esc(typeLabel(row, o.typeLabels)) + '</span>');
        if (o.showMatch)
            cells.push('<span class="m-match" title="' + esc(file) + '">'
                + esc(row.match_stem || row.match_label || file) + '</span>');
        if (o.showPlayer)
            cells.push('<span class="m-player team' + esc(row.team || 0) + '">' + esc(row.player) + '</span>');
        // The second player. Tinted by FIXED team identity like the subject is,
        // so a teammate and an opponent are told apart by colour rather than by
        // reading the preposition. `partner_team` falls back to the subject's:
        // a partner is a teammate by definition.
        if (o.showOther && row.partner)
            cells.push('<span class="m-with">+ <b class="team'
                + esc(row.partner_team || row.team || 0) + '">' + esc(row.partner) + '</b></span>');
        if (o.showOther && row.other)
            cells.push('<span class="m-other">' + esc(otherRel(row)) + ' <b class="team'
                + esc(row.other_team || 0) + '">' + esc(row.other) + '</b></span>');
        cells.push('<span class="m-label">' + esc(row.label) + '</span>');
        cells.push('<span class="m-detail">' + esc(row.detail) + '</span>');

        const btns = [];
        if (win && has('preview'))
            btns.push('<button type="button" class="ir-toggle" data-mid="' + mid + '"'
                + ' data-file="' + encFile + '" data-start="' + row.start_tick + '"'
                + ' data-end="' + row.end_tick + '" data-focus="' + encFocus + '"'
                + ' data-caption="' + cap + '">▶ preview</button>');
        if (win && has('record')) {
            // The label reflects whether a clip is already on disk, so saved
            // footage is obvious before clicking (ClipBox.prime keeps it closed).
            // `clip_short` means the .mp4 on disk is shorter than the tick
            // window it is filed under - a truncated capture, or footage
            // re-keyed onto a window it was not recorded for. The whole point
            // of the two boxes is that they show the same moment at the same
            // length, so a clip that silently doesn't is worse than no clip:
            // it is marked on the button, before it is even opened, and the
            // box itself carries the numbers. See app.py's _with_clip_urls.
            //
            // The visible marker is drawn by CSS from the `clip-short` class,
            // NOT written into the label: ClipBox.prime/show/close each reset
            // this button's textContent ("show clip" / "re-record"), so a ⚠ in
            // the text is erased the moment the box is opened or closed.
            const shortInfo = row.clip_short || null;
            const shortAttr = shortInfo
                ? ' data-clip-short="' + shortInfo.actual + '/' + shortInfo.expected + '"'
                : '';
            btns.push('<button type="button" class="clip-record-btn'
                + (shortInfo ? ' clip-short' : '') + '" data-rid="' + mid + '"'
                + ' data-file="' + encFile + '" data-start="' + row.start_tick + '"'
                + ' data-end="' + row.end_tick + '" data-focus="' + encFocus + '"'
                + shortAttr
                + ' data-caption="' + cap + '">'
                + (row.clip_url ? '🎥 show clip' : '🎥 record') + '</button>');
            btns.push('<button type="button" class="clip-close-btn" data-rid="' + mid + '"'
                + ' title="Close the in-game clip" style="display:none">✕ clip</button>');
        }
        if (hasTick(row) && has('demo'))
            btns.push('<button type="button" class="clip-record-btn demo-jump-btn"'
                + ' data-file="' + encFile + '" data-tick="' + row.tick + '"'
                + ' data-focus="' + encFocus + '"'
                + ' title="Open the demo in CS2 - it jumps to this moment'
                + (focus ? ' from ' + esc(focus) + '’s point of view' : '')
                + ' by itself; F8 in game jumps manually">🎮 in CS2</button>');
        if (hasTick(row) && has('viewer'))
            btns.push('<a href="' + viewerUrl(row, o.matchFile) + '" target="_blank"'
                + ' title="Open this moment in the 2D replayer">▶ in 2D Player</a>');
        if (has('overlay'))
            btns.push('<button type="button" class="mr-overlay-btn" data-mid="' + mid + '"'
                + ' title="Add this moment to the map overlay">+ overlay</button>');

        return '<div class="hl-item" data-idx="' + idx + '" data-mid="' + mid + '">'
            + '<div class="ow-moment">'
            + cells.join('') + btns.join('')
            + '</div>'
            + '<div class="ow-media">'
            + '<div class="ir-mount" id="ir-' + mid + '" style="display:none"></div>'
            + '<div class="clip-record-result" id="rec-' + mid + '"></div>'
            + '</div>'
            + '</div>';
    }

    return { TYPE_LABEL, OTHER_REL, otherRel, esc, hasWindow, hasTick,
             captionFor, viewerUrl, typeLabel, rowHtml };
}));
