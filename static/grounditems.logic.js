/**
 * grounditems.logic.js - pure state resolution for dropped weapons/utility
 * lying on the ground (parser's additive `items` array: `drop`/`pickup`
 * events per DroppedItem.ItemID, see cmd/parser/main.go). No DOM, no canvas:
 * unit-testable under node (test/grounditems.logic.test.mjs). Canvas glue
 * lives in static/grounditems.js; consumers are viewer.js and multi.js.
 *
 * Matches parsed before this feature simply have no `items` field -
 * prepare(undefined) returns [] and callers render nothing (same additive-
 * field convention as everywhere else in this repo).
 */
(function (root, factory) {
    if (typeof module === 'object' && module.exports) module.exports = factory();
    else root.GroundItemsLogic = factory();
}(typeof self !== 'undefined' ? self : this, function () {
    'use strict';

    // Defensive copy + tick-ascending sort, once per loaded match.
    function prepare(items) {
        return (items || []).slice().sort((a, b) => a.tick - b.tick);
    }

    // Which items are currently lying on the ground at `tick`, scoped to the
    // round starting at `roundStart` (drops from earlier rounds never bleed
    // through - same round-scoping as the bomb-state lookup in viewer.js).
    // Returns the most recent "drop" per item_id that hasn't since been
    // picked back up, in no particular order.
    function resolveGroundItems(sortedItems, tick, roundStart) {
        const onGround = new Map();
        for (const it of sortedItems) {
            if (it.tick < roundStart) continue;
            if (it.tick > tick) break;
            if (it.event === 'drop') onGround.set(it.item_id, it);
            else if (it.event === 'pickup') onGround.delete(it.item_id);
        }
        return Array.from(onGround.values());
    }

    return { prepare, resolveGroundItems };
}));
