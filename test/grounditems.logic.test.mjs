/**
 * Unit tests for the ground-item state resolution (static/grounditems.logic.js).
 * Run: node test/grounditems.logic.test.mjs   (no deps, no browser)
 *
 * What matters: drop/pickup pairing by item_id, round scoping (drops from an
 * earlier round never bleed into the next), and graceful handling of an
 * absent/empty `items` array (matches parsed before this feature).
 */
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const G = require('../static/grounditems.logic.js');

let passed = 0, failed = 0;
const ok = (c, m) => { if (c) passed++; else { failed++; console.error('  ✗ FAIL:', m); } };
const eq = (a, b, m) => ok(a === b, `${m} (got ${JSON.stringify(a)}, want ${JSON.stringify(b)})`);

// ── prepare: absent/empty data degrades to [] ─────────────────────────────────
{
    eq(G.prepare(undefined).length, 0, 'prepare(undefined) -> []');
    eq(G.prepare([]).length, 0, 'prepare([]) -> []');
}

// ── prepare: sorts by tick ascending, defensive copy ───────────────────────────
{
    const raw = [{ tick: 30 }, { tick: 10 }, { tick: 20 }];
    const sorted = G.prepare(raw);
    ok(sorted !== raw, 'prepare returns a new array');
    eq(sorted.map(i => i.tick).join(','), '10,20,30', 'sorted ascending by tick');
    eq(raw.map(i => i.tick).join(','), '30,10,20', 'original array left untouched');
}

// ── resolveGroundItems: a dropped item stays on the ground until picked up ─────
{
    const items = G.prepare([
        { tick: 100, event: 'drop', item_id: 'a', X: 1, Y: 1, weapon: 'weapon_ak47' },
    ]);
    eq(G.resolveGroundItems(items, 100, 0).length, 1, 'visible at the drop tick');
    eq(G.resolveGroundItems(items, 500, 0).length, 1, 'still visible long after, no pickup yet');
    eq(G.resolveGroundItems(items, 99, 0).length, 0, 'not visible before it was dropped');
}

// ── resolveGroundItems: pickup removes the item ────────────────────────────────
{
    const items = G.prepare([
        { tick: 100, event: 'drop', item_id: 'a' },
        { tick: 200, event: 'pickup', item_id: 'a' },
    ]);
    eq(G.resolveGroundItems(items, 150, 0).length, 1, 'on ground between drop and pickup');
    eq(G.resolveGroundItems(items, 200, 0).length, 0, 'gone exactly at pickup tick');
    eq(G.resolveGroundItems(items, 300, 0).length, 0, 'stays gone after pickup');
}

// ── resolveGroundItems: re-drop after pickup reappears ─────────────────────────
{
    const items = G.prepare([
        { tick: 100, event: 'drop', item_id: 'a' },
        { tick: 200, event: 'pickup', item_id: 'a' },
        { tick: 300, event: 'drop', item_id: 'a' },
    ]);
    eq(G.resolveGroundItems(items, 250, 0).length, 0, 'gone between pickup and re-drop');
    eq(G.resolveGroundItems(items, 300, 0).length, 1, 'back on the ground after re-drop');
}

// ── resolveGroundItems: independent items tracked separately by item_id ────────
{
    const items = G.prepare([
        { tick: 100, event: 'drop', item_id: 'a' },
        { tick: 110, event: 'drop', item_id: 'b' },
        { tick: 200, event: 'pickup', item_id: 'a' },
    ]);
    const at150 = G.resolveGroundItems(items, 150, 0);
    eq(at150.length, 2, 'both items on ground before either pickup');
    const at250 = G.resolveGroundItems(items, 250, 0);
    eq(at250.length, 1, 'only the un-picked-up item remains');
    eq(at250[0].item_id, 'b', 'the remaining item is the right one');
}

// ── resolveGroundItems: round scoping - earlier-round drops don't bleed through ─
{
    const items = G.prepare([
        { tick: 100, event: 'drop', item_id: 'a' },   // round 1 drop, never picked up
        { tick: 5000, event: 'drop', item_id: 'b' },  // round 2 drop
    ]);
    eq(G.resolveGroundItems(items, 5010, 4000).length, 1, 'round-2 lookup only sees round-2 drop');
    eq(G.resolveGroundItems(items, 5010, 4000)[0].item_id, 'b', 'the visible item is the round-2 one');
    eq(G.resolveGroundItems(items, 200, 0).length, 1, 'round-1 lookup still sees the round-1 drop');
}

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
