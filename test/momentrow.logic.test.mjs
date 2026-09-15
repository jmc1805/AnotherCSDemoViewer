/**
 * Unit tests for the moment-row markup helpers (static/momentrow.logic.js).
 * Run: node test/momentrow.logic.test.mjs   (no deps, no browser)
 *
 * What matters: HTML escaping (player names are user-controlled and land
 * straight in markup and in title="" attributes), graceful degradation when a
 * row lacks a clip window or a tick, per-row `data-file` (the cross-match
 * property the copy-pasted originals lacked), and id namespacing so two lists
 * on one page never collide.
 */
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const M = require('../static/momentrow.logic.js');

let passed = 0, failed = 0;
const ok = (c, m) => { if (c) passed++; else { failed++; console.error('  ✗ FAIL:', m); } };
const eq = (a, b, m) => ok(a === b, `${m} (got ${JSON.stringify(a)}, want ${JSON.stringify(b)})`);
const has = (h, needle, m) => ok(h.includes(needle), `${m} (missing ${JSON.stringify(needle)})`);
const lacks = (h, needle, m) => ok(!h.includes(needle), `${m} (unexpectedly contains ${JSON.stringify(needle)})`);

const full = {
    type: 'clutch', round: 12, tick: 91234, start_tick: 91010, end_tick: 91266,
    label: '1v3 clutch', detail: 'Won round 12 as the last player alive',
    player: 'alpha', team: 1, match: 'de_dust_20260712_1353_mm.json', clip_url: null,
};

// ── esc: the injection surface ────────────────────────────────────────────────
{
    eq(M.esc('<script>'), '&lt;script&gt;', 'escapes angle brackets');
    eq(M.esc('a & b'), 'a &amp; b', 'escapes ampersand');
    eq(M.esc('say "hi"'), 'say &quot;hi&quot;', 'escapes double quotes (attribute context)');
    eq(M.esc(null), '', 'null -> empty string');
    eq(M.esc(0), '0', 'zero survives (not treated as absent)');
    // Ampersand must be escaped FIRST or the entity itself gets re-escaped.
    eq(M.esc('&lt;'), '&amp;lt;', 'already-escaped text is escaped again, not mangled');
}

// ── hasWindow / hasTick ───────────────────────────────────────────────────────
{
    ok(M.hasWindow(full), 'full row has a clip window');
    ok(!M.hasWindow({ tick: 5 }), 'row without start/end has no window');
    ok(!M.hasWindow({ start_tick: 1 }), 'half a window is not a window');
    ok(!M.hasWindow(null), 'null row has no window');
    ok(M.hasWindow({ start_tick: 0, end_tick: 10 }), 'start_tick 0 is a real bound, not absent');
    ok(M.hasTick({ tick: 0 }), 'tick 0 is a real tick, not absent');
    ok(!M.hasTick({}), 'no tick -> not jumpable');
}

// ── captionFor: skips absent parts, no stray separators ───────────────────────
{
    eq(M.captionFor(full), 'alpha - 1v3 clutch - Won round 12 as the last player alive', 'full caption');
    eq(M.captionFor({ label: 'Kill (AK47)' }), 'Kill (AK47)', 'label only');
    eq(M.captionFor({}), '', 'empty row -> empty caption');
}

// ── viewerUrl: row match wins, page fallback otherwise ────────────────────────
{
    has(M.viewerUrl(full, 'other.json'), 'match=de_dust_20260712_1353_mm.json', "row's own match wins");
    has(M.viewerUrl({ tick: 7 }, 'fallback.json'), 'match=fallback.json', 'falls back to page match');
    has(M.viewerUrl(full, null), '&tick=91234', 'carries the tick');
}

// ── typeLabel ─────────────────────────────────────────────────────────────────
{
    eq(M.typeLabel(full), 'Clutch', 'known type -> friendly label');
    eq(M.typeLabel({ type: 'weird_thing' }), 'weird_thing', 'unknown type falls through verbatim');
    eq(M.typeLabel({ type: 'clutch' }, { clutch: 'CLUTCH!' }), 'CLUTCH!', 'caller can override labels');
}

// ── rowHtml: the full row offers every action ─────────────────────────────────
{
    const h = M.rowHtml(full, { idPrefix: 'hl', index: 3, showType: true, showPlayer: true });
    has(h, 'id="ir-hl-3"', 'preview mount id is namespaced by prefix + index');
    has(h, 'id="rec-hl-3"', 'record result id is namespaced too');
    has(h, 'data-mid="hl-3"', 'preview button carries the row id');
    has(h, 'class="ir-toggle"', 'preview button rendered');
    has(h, 'class="clip-record-btn"', 'record button rendered');
    has(h, 'class="clip-close-btn"', 'clip-close button rendered');
    has(h, 'demo-jump-btn', 'CS2 jump button rendered');
    has(h, '/viewer?match=', 'viewer link rendered');
    has(h, '🎥 record', 'no clip on disk -> record label');
    has(h, '>R12<', 'round cell');
    has(h, 'team1', 'player cell carries the fixed team identity');
}

// ── rowHtml: per-row data-file is what makes cross-match lists work ───────────
{
    const h = M.rowHtml(full, { idPrefix: 'q', index: 0 });
    const encoded = encodeURIComponent('de_dust_20260712_1353_mm.json');
    has(h, 'data-file="' + encoded + '"', 'every action button carries its own match file');
    const fallback = M.rowHtml({ tick: 5, start_tick: 1, end_tick: 9 },
        { idPrefix: 'q', index: 0, matchFile: 'page.json' });
    has(fallback, 'data-file="page.json"', 'rows without a match fall back to the list-level file');
}

// ── rowHtml: missing window / tick removes exactly those actions ──────────────
{
    const noWin = M.rowHtml({ tick: 500, label: 'Round 4', player: 'alpha' }, { idPrefix: 'q', index: 1 });
    lacks(noWin, 'class="ir-toggle"', 'no clip window -> no preview button');
    lacks(noWin, 'class="clip-record-btn"', 'no clip window -> no record button');
    has(noWin, 'demo-jump-btn', 'a tick alone is still enough to jump into CS2');
    has(noWin, '/viewer?match=', 'a tick alone is still enough to open the viewer');

    const noTick = M.rowHtml({ start_tick: 1, end_tick: 9, label: 'x' }, { idPrefix: 'q', index: 2 });
    has(noTick, 'class="ir-toggle"', 'a window alone is still previewable');
    lacks(noTick, 'demo-jump-btn', 'no tick -> no CS2 jump');
    lacks(noTick, '/viewer?match=', 'no tick -> no viewer link');
}

// ── rowHtml: the actions option is respected ──────────────────────────────────
{
    const only = M.rowHtml(full, { idPrefix: 'q', index: 0, actions: ['preview', 'overlay'] });
    has(only, 'class="ir-toggle"', 'requested action present');
    has(only, 'mr-overlay-btn', 'overlay action present when asked for');
    lacks(only, 'clip-record-btn', 'unrequested record action absent');
    lacks(only, '/viewer?match=', 'unrequested viewer action absent');
    lacks(M.rowHtml(full, { idPrefix: 'q', index: 0 }), 'mr-overlay-btn',
        'overlay is opt-in - not in the default action set');
}

// ── rowHtml: an existing clip flips the record label ──────────────────────────
{
    const saved = M.rowHtml({ ...full, clip_url: '/clips/x/y.mp4' }, { idPrefix: 'q', index: 0 });
    has(saved, '🎥 show clip', 'clip on disk -> show-clip label');
    lacks(saved, '🎥 record', 'clip on disk -> not offering a fresh record');
}

// ── rowHtml: hostile player names cannot break out ────────────────────────────
{
    const evil = { ...full, player: '"><img src=x onerror=alert(1)>', detail: '<b>bold</b>' };
    const h = M.rowHtml(evil, { idPrefix: 'q', index: 0, showPlayer: true });
    lacks(h, '<img src=x', 'a script-y player name is escaped in the cell');
    lacks(h, '<b>bold</b>', 'markup in detail is escaped');
    has(h, '&lt;b&gt;bold&lt;/b&gt;', 'detail is present, just inert');
    // The name also lands inside the jump button's title="" attribute.
    ok(!/title="[^"]*"><img/.test(h), 'escaped name cannot terminate the title attribute');
}

// ── rowHtml: optional columns are genuinely optional ──────────────────────────
{
    const bare = M.rowHtml(full, { idPrefix: 'q', index: 0 });
    lacks(bare, 'hl-type', 'type chip omitted by default');
    lacks(bare, 'm-player', 'player column omitted by default');
    lacks(bare, 'm-match', 'match column omitted by default');
    const wide = M.rowHtml({ ...full, match_stem: 'de_dust_20260712_1353_mm' },
        { idPrefix: 'q', index: 0, showMatch: true });
    has(wide, 'class="m-match"', 'match column rendered when asked for');
    has(wide, '>de_dust_20260712_1353_mm<', 'match column prefers the readable stem');
}

// ── rowHtml: a row with no round still renders ────────────────────────────────
{
    const h = M.rowHtml({ tick: 10, label: 'x' }, { idPrefix: 'q', index: 0 });
    has(h, '<span class="m-round">·</span>', 'absent round renders as a placeholder, not "Rundefined"');
    // Round 0 is a legitimate value and must not be swallowed by a falsy check.
    has(M.rowHtml({ round: 0, tick: 1 }, { idPrefix: 'q', index: 0 }), '>R0<', 'round 0 renders');
}

// ── the second player ─────────────────────────────────────────────────────────
{
    const trade = {
        type: 'trade', round: 4, tick: 100, label: 'Trade',
        detail: 'traded after 2.6s', player: 'alpha', team: 2,
        partner: 'bravo', partner_team: 2, other: 'charlie', other_team: 1,
    };
    const h = M.rowHtml(trade, { showPlayer: true, showOther: true, showType: true });
    has(h, 'class="m-with">+ <b class="team2">bravo</b>', 'partner is tinted by their team');
    has(h, 'class="m-other">vs <b class="team1">charlie</b>', 'opponent by theirs');
    has(h, '<span class="hl-type trade">Trade</span>', 'kind chip');

    // The relation word is direction-dependent - you kill someone, but you are
    // killed BY them.
    has(M.rowHtml({ type: 'death', player: 'a', other: 'b', other_team: 2 },
                  { showPlayer: true, showOther: true }),
        '>by <b class="team2">b</b>', 'a death is "by"');
    has(M.rowHtml({ type: 'crossfire', player: 'a', other: 'b', other_team: 2 },
                  { showPlayer: true, showOther: true }),
        '>on <b class="team2">b</b>', 'a crossfire is "on"');
}

{
    // A row with no second player must render nothing at all there - not an
    // empty cell, and certainly not "undefined".
    const smoke = { type: 'smoke', player: 'alpha', team: 1, label: 'Smoke', detail: '' };
    const h = M.rowHtml(smoke, { showPlayer: true, showOther: true });
    lacks(h, 'm-other', 'no opponent cell');
    lacks(h, 'm-with', 'no partner cell');
    lacks(h, 'undefined', 'and no stray undefined');
}

{
    // Same injection surface as the player name, and it lands in markup.
    const h = M.rowHtml({ type: 'kill', player: 'a', other: '<img src=x>', other_team: 1 },
                        { showPlayer: true, showOther: true });
    lacks(h, '<img src=x>', 'the opponent name is escaped');
    has(h, '&lt;img src=x&gt;', 'as entities');
}

{
    // The caption is burned into a recorded clip and read with no row around
    // it, so it has to carry what the cells carry.
    eq(M.captionFor({ player: 'alpha', partner: 'bravo', other: 'charlie',
                      type: 'trade', label: 'Trade', detail: 'traded after 2.6s' }),
       'alpha + bravo - Trade - vs charlie - traded after 2.6s',
       'caption names both other players');
    eq(M.captionFor({ player: 'alpha', label: 'Smoke' }), 'alpha - Smoke',
       'and still skips absent parts cleanly');
}

{
    // An execute has no subject at all - the same shape a round row has.
    const ex = { type: 'execute', round: 3, tick: 50, start_tick: 10, end_tick: 90,
                 label: 'B execute', detail: '3 utility from 2 players in 4.6s',
                 player: null, team: 2, match: 'm.json' };
    const h = M.rowHtml(ex, { showPlayer: true, showOther: true, showType: true,
                              actions: ['preview', 'demo', 'viewer'] });
    has(h, 'hl-type execute', 'renders');
    has(h, 'ir-toggle', 'and is still previewable without a focus player');
    lacks(h, 'undefined', 'no undefined leaks into the markup');
}

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
