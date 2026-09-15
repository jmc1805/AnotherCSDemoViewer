/**
 * Unit tests for shared number formatting (static/fmt.logic.js).
 * Run: node test/fmt.logic.test.mjs   (no deps, no browser)
 *
 * The point of the module is that its output does not depend on the machine
 * running it, so these assertions are written as exact strings on purpose:
 * they fail on any host whose locale leaks through.
 */
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const F = require('../static/fmt.logic.js');

let passed = 0, failed = 0;
const ok = (cond, msg) => { if (cond) passed++; else { failed++; console.error('  ✗ FAIL:', msg); } };
const eq = (a, b, msg) => ok(a === b, `${msg} (got ${JSON.stringify(a)}, want ${JSON.stringify(b)})`);

// ── num: English grouping regardless of the host locale ───────────────────────
eq(F.num(16000), '16,000', 'thousands separated by a comma, not a period');
eq(F.num(1234567), '1,234,567', 'grouped every three digits');
eq(F.num(999), '999', 'no separator below a thousand');
eq(F.num(0), '0', 'zero');
eq(F.num(-4200), '-4,200', 'negatives keep their sign');

// ── num: rounds rather than emitting a fraction ───────────────────────────────
eq(F.num(1234.4), '1,234', 'rounds down');
eq(F.num(1234.6), '1,235', 'rounds up');

// ── num: totality - nothing reaches a page as "NaN" ───────────────────────────
eq(F.num(null), '0', 'null → 0');
eq(F.num(undefined), '0', 'undefined → 0');
eq(F.num(NaN), '0', 'NaN → 0');
eq(F.num(''), '0', 'empty string → 0');
eq(F.num(Infinity), '0', 'Infinity → 0');
eq(F.num('2500'), '2,500', 'numeric strings are accepted');

// ── money / rating ────────────────────────────────────────────────────────────
eq(F.money(4750), '$4,750', 'money is prefixed and grouped');
eq(F.money(0), '$0', 'zero money still renders');
eq(F.money(null), '$0', 'money is total too');
eq(F.rating(16000), '16,000', 'a Premier rating groups like any other number');
eq(F.rating(0), '0', 'unrated');

// ── the pinned locale is the contract, not an implementation detail ───────────
eq(F.LOCALE, 'en-US', 'the locale is pinned and exported so callers can see it');

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
