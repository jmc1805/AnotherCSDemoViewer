/**
 * fmt.logic.js - the small formatting primitives every page needs: numbers,
 * money, ratings, and HTML escaping.
 *
 * All of it exists to pin one locale. `Number.prototype.toLocaleString()` with
 * no argument follows the *browser's* locale, so the same Premier rating read
 * "16,000" on one machine and "16.000" on another, and the same buy value
 * formatted two different ways within a single app - the 2D viewer pinned
 * en-US for money while the stats page did not. The UI is written in English
 * throughout and CS2 itself renders a Premier rating with a comma, so English
 * grouping is what the rest of the interface already implies.
 *
 * No DOM: unit-testable under node (test/fmt.logic.test.mjs).
 * Every function is total - null, undefined and NaN all format as zero rather
 * than reaching a page as "NaN".
 */
(function (root, factory) {
    if (typeof module === 'object' && module.exports) module.exports = factory();
    else root.FmtLogic = factory();
}(typeof self !== 'undefined' ? self : this, function () {
    'use strict';

    const LOCALE = 'en-US';

    /** A whole number with thousands separators: 16000 → "16,000". */
    function num(v) {
        const n = Number(v);
        return (Number.isFinite(n) ? Math.round(n) : 0).toLocaleString(LOCALE);
    }

    /** In-game money: 4750 → "$4,750". */
    function money(v) {
        return '$' + num(v);
    }

    /**
     * A CS2 Premier rating. Same grouping as num(), named separately because
     * it is a rating rather than a count - callers read better for it, and a
     * future change to rating formatting has one place to happen.
     */
    function rating(v) {
        return num(v);
    }

    /**
     * Escape a value for interpolation into HTML, attribute values included.
     *
     * Nearly every list on the site is built by joining template strings, and
     * the values going in are player names and map names that ultimately come
     * out of a demo file. `"` is escaped alongside `&<>` because most of those
     * interpolations land inside a double-quoted attribute (a title, a
     * data-value), where an unescaped quote closes the attribute early.
     */
    function esc(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    // ── Canvas font stacks ────────────────────────────────────────────────
    // The CSS side reads --font-ui / --font-narrow / --font-mono from
    // _shell.html, but ctx.font takes a plain string and cannot see a custom
    // property, so the same three stacks are spelled out once here instead of
    // being retyped at each of the ~18 ctx.font sites. Keep them in step with
    // the tokens in _shell.html; the roles are documented there.
    //
    // Usage: ctx.font = 'bold 11px ' + FmtLogic.FONT_NARROW;
    const FONT_UI     = "Archivo, system-ui, -apple-system, 'Segoe UI', sans-serif";
    const FONT_NARROW = "'Archivo Narrow', Archivo, system-ui, -apple-system, 'Segoe UI', sans-serif";
    const FONT_MONO   = "'Roboto Mono', ui-monospace, 'Cascadia Mono', Consolas, monospace";

    return { LOCALE, num, money, rating, esc, FONT_UI, FONT_NARROW, FONT_MONO };
}));
