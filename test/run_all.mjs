/**
 * run_all.mjs - run the whole test suite, both halves of it.
 *
 *   node test/run_all.mjs              every JS and Python test
 *   node test/run_all.mjs --js         the browser-logic tests only
 *   node test/run_all.mjs --py         the server-side tests only
 *   node test/run_all.mjs scope maps   only tests whose filename contains one
 *                                      of these substrings
 *
 * The suite is deliberately dependency-free: the `*.logic.test.mjs` files run
 * under plain node and the `*_test.py` files under plain python, with no test
 * framework on either side. That keeps `git clone && node test/run_all.mjs`
 * working, but it also means nothing discovers or reports them as a whole -
 * which is what this file is for. It shells out to one process per file,
 * prints each file's own summary line, and exits non-zero if any file did.
 *
 * The Go tests are separate and need a Go toolchain: `go test ./cmd/...`.
 *
 * Set PYTHON to choose an interpreter (default: `python3`, falling back to
 * `python`, which is what a Windows install without the alias provides).
 */
import { spawnSync } from 'node:child_process';
import { readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const only = args.filter(a => !a.startsWith('--'));
const wantJs = !args.includes('--py');
const wantPy = !args.includes('--js');

/** The python command available on this machine, or null if there is none. */
function findPython() {
    for (const cmd of [process.env.PYTHON, 'python3', 'python'].filter(Boolean)) {
        const probe = spawnSync(cmd, ['--version'], { encoding: 'utf8' });
        if (!probe.error && probe.status === 0) return cmd;
    }
    return null;
}

const wanted = f => !only.length || only.some(o => f.includes(o));
const all = readdirSync(HERE).sort().filter(f => f !== 'run_all.mjs');

// Every JS file, then every Python one - grouped rather than interleaved
// alphabetically. Grouping keeps the output readable, and it keeps the
// interpreter probe below out of the way until the JS half has finished:
// spawning python before node children confuses process-group handling on some
// Windows setups, and the whole run dies with an opaque job-object error.
const files = [
    ...(wantJs ? all.filter(f => f.endsWith('.test.mjs')).filter(wanted) : []),
    ...(wantPy ? all.filter(f => f.endsWith('_test.py')).filter(wanted) : []),
];

if (!files.length) {
    console.error(only.length ? `No test files match: ${only.join(', ')}` : 'No test files found.');
    process.exit(1);
}

const failed = [];
const skipped = [];
let python;   // resolved on first use, for the reason above

for (const file of files) {
    const isPy = file.endsWith('.py');
    if (isPy && python === undefined) python = findPython();
    if (isPy && !python) { skipped.push(file); continue; }
    const [cmd, argv] = isPy ? [python, [join(HERE, file)]] : ['node', [join(HERE, file)]];
    const res = spawnSync(cmd, argv, { encoding: 'utf8' });
    // Each test file prints its own "N passed, M failed" line; show that
    // rather than its whole output, and the whole output only on a failure.
    const out = ((res.stdout || '') + (res.stderr || '')).trim();
    const summary = out.split('\n').filter(Boolean).pop() || '(no output)';
    const bad = res.status !== 0;
    if (bad) failed.push(file);
    console.log(`${bad ? 'FAIL' : 'ok  '}  ${file.padEnd(32)} ${summary}`);
    if (bad) console.log(out.split('\n').map(l => '        ' + l).join('\n'));
}

if (skipped.length) {
    console.log(`\nSkipped ${skipped.length} Python test file(s): no python interpreter found. ` +
                'Set PYTHON=<path> to run them.');
}
console.log(`\n${files.length - failed.length - skipped.length} file(s) passed` +
            (failed.length ? `, ${failed.length} failed: ${failed.join(', ')}` : ''));
process.exit(failed.length ? 1 : 0);
