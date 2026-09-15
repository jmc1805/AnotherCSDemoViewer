#!/usr/bin/env node
/**
 * tools/extract_map_calibration.mjs - read Valve's own radar projection
 * parameters out of the CS2 VPK and diff them against the app's tables.
 *
 *   csgo/pak01_dir.vpk ── Source2Viewer-CLI ──▶ resource/overviews/<map>.txt
 *                      ── parse ──▶ pos_x / pos_y / scale / verticalsections
 *
 * Two tables in this repo are meant to be transcriptions of that file and
 * nothing else:
 *
 *   static/match2d.logic.js   MAP_LIBRARY  { scale, x: pos_x, y: pos_y }
 *   static/maplayers.logic.js LAYERS       { split } = the "lower" section's
 *                                          AltitudeMax
 *
 * They are easy to get wrong by eye - a calibration that is slightly off still
 * draws a plausible-looking map, and a floor threshold guessed from a Z
 * histogram can be off by hundreds of units (see the de_train note in
 * maplayers.logic.js). So this reports a diff rather than asking anyone to
 * compare two lists by hand: every map the app supports is checked against the
 * game files, and maps the game declares but the app has no entry for are
 * listed as candidates with a paste-ready line.
 *
 * Unlike extract_ui_assets.{sh,ps1,bat} this is a single Node script with no
 * shell siblings to keep in lockstep: node is already a hard requirement of
 * the asset pipeline, and the output here is source code for a human to paste,
 * not a runtime asset directory.
 *
 * Usage:
 *   node tools/extract_map_calibration.mjs              # diff every map
 *   node tools/extract_map_calibration.mjs de_train     # just these maps
 *   node tools/extract_map_calibration.mjs --json       # machine-readable dump
 *
 * Configuration (the same analysis_data files extract_ui_assets.sh reads):
 *   analysis_data/cs2_game_dir         CS2 install (contains game/csgo/pak01_dir.vpk)
 *   analysis_data/source2viewer_cli    Source2Viewer-CLI binary (falls back to
 *                                      a sibling of source2viewer_path, then PATH)
 * $CS2_PAK_VPK overrides VPK discovery entirely.
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.dirname(HERE);
const require = createRequire(import.meta.url);

// ── the app's current tables, read from the modules themselves ───────────────
const { MAP_LIBRARY } = require(path.join(ROOT, 'static', 'match2d.logic.js'));
const MapLayers = require(path.join(ROOT, 'static', 'maplayers.logic.js'));
const MapsLogic = require(path.join(ROOT, 'static', 'maps.logic.js'));

// ── args ─────────────────────────────────────────────────────────────────────
const args = process.argv.slice(2);
const asJson = args.includes('--json');
const wanted = args.filter(a => !a.startsWith('-'));
if (args.includes('-h') || args.includes('--help')) {
    console.log(fs.readFileSync(fileURLToPath(import.meta.url), 'utf8')
        .split('\n').filter(l => /^\s*(\/\*\*|\*)/.test(l))
        .map(l => l.replace(/^\s*(\/\*\*|\*\/?)\s?/, '')).join('\n').trimEnd());
    process.exit(0);
}

// ── settings, mirroring extract_ui_assets.sh's setting() ─────────────────────
function setting(name) {
    const ddir = process.env.CS2VIEWER_DATA_DIR || path.join(ROOT, 'data');
    for (const cand of [path.join(ddir, 'analysis_data', name),
                        path.join(ROOT, 'data', 'analysis_data', name),
                        path.join(ROOT, 'analysis_data', name)]) {
        if (fs.existsSync(cand)) return fs.readFileSync(cand, 'utf8').trim();
    }
    return '';
}

function resolveCli() {
    const direct = setting('source2viewer_cli');
    if (direct && fs.existsSync(direct)) return direct;
    const gui = setting('source2viewer_path');
    if (gui) {
        for (const n of ['Source2Viewer-CLI', 'Source2Viewer-CLI.exe']) {
            const cand = path.join(path.dirname(gui), n);
            if (fs.existsSync(cand)) return cand;
        }
    }
    return 'Source2Viewer-CLI';   // let PATH resolve it (and fail loudly if not)
}

function resolveVpk() {
    if (process.env.CS2_PAK_VPK) return process.env.CS2_PAK_VPK;
    const dir = setting('cs2_game_dir');
    if (!dir) die('CS2 game dir not configured (data/analysis_data/cs2_game_dir)');
    for (const sub of ['game/csgo', 'csgo', '.']) {
        const d = path.join(dir, sub);
        if (!fs.existsSync(d)) continue;
        for (const n of ['pak01_dir.vpk', 'pak_01_dir.vpk']) {
            const c = path.join(d, n);
            if (fs.existsSync(c)) return c;
        }
        const glob = fs.readdirSync(d).filter(f => /^pak.*_dir\.vpk$/.test(f)).sort();
        if (glob.length) return path.join(d, glob[0]);
    }
    return die(`no pak*_dir.vpk found under ${dir} (looked in game/csgo, csgo)`);
}

function die(msg) { console.error(`error: ${msg}`); process.exit(1); }

// ── the overview file format ────────────────────────────────────────────────
// Valve KeyValues, but only ever a handful of flat "key" "value" pairs plus one
// optional nested verticalsections block, so a full KV parser would be more
// machinery than the format warrants here. Comments are stripped first because
// several of these files carry `// texture file` style trailers, and de_train's
// verticalsections block is a copy-paste from de_nuke's whose comment still
// says "de_nuke_lower_radar.dds".
function parseOverview(text) {
    const t = text.replace(/\/\/.*/g, '');
    const val = k => {
        const m = t.match(new RegExp(`"${k}"\\s+"([^"]+)"`, 'i'));
        return m ? m[1] : null;
    };
    const num = k => { const v = val(k); return v == null ? null : Number(v); };
    let split = null;
    const vs = t.match(/"verticalsections"\s*\{([\s\S]*)\}/i);
    if (vs) {
        const lower = vs[1].match(/"lower"\s*\{([\s\S]*?)\}/i);
        if (lower) {
            const mx = lower[1].match(/"AltitudeMax"\s+"(-?[\d.]+)"/i);
            if (mx) split = Number(mx[1]);
        }
    }
    return { pos_x: num('pos_x'), pos_y: num('pos_y'), scale: num('scale'), split };
}

// ── extract ─────────────────────────────────────────────────────────────────
const cli = resolveCli();
const vpk = resolveVpk();
if (!fs.existsSync(vpk)) die(`VPK not found: ${vpk}`);

const work = fs.mkdtempSync(path.join(os.tmpdir(), 'map_calib_'));
process.on('exit', () => { try { fs.rmSync(work, { recursive: true, force: true }); } catch {} });

if (!asJson) {
    console.error(`==> VPK: ${vpk}`);
    console.error(`==> CLI: ${cli}`);
    console.error('==> extracting resource/overviews/ …');
}
try {
    execFileSync(cli, ['--input', vpk, '--output', work,
                       '--vpk_filepath', 'resource/overviews/'], { stdio: 'ignore' });
} catch (e) {
    die(`Source2Viewer-CLI failed (${cli}): ${e.message}\n` +
        '       It is a SEPARATE download from the GUI - cli-<os>-x64.zip at\n' +
        '       https://github.com/ValveResourceFormat/ValveResourceFormat/releases\n' +
        '       then: echo /path/to/Source2Viewer-CLI > data/analysis_data/source2viewer_cli');
}

const ovDir = path.join(work, 'resource', 'overviews');
if (!fs.existsSync(ovDir)) die(`no resource/overviews/ in the extraction - is ${vpk} the main content VPK?`);

const game = new Map();
for (const f of fs.readdirSync(ovDir).filter(f => f.endsWith('.txt'))) {
    const raw = f.slice(0, -4);
    game.set(raw, parseOverview(fs.readFileSync(path.join(ovDir, f), 'utf8')));
}

// ── compare ─────────────────────────────────────────────────────────────────
// Compare on the canonical name: the app keys both tables that way.
//
// Two kinds of row must NOT be reported as a mismatch, and both look like one:
//
//   legacy   a raw name MAP_ALIASES folds onto a different map. `de_dust` is
//            the only one, and it is a real, retired map with its own real
//            parameters in the VPK - but the app deliberately reads a stored
//            `de_dust` as Dust II (the old header-scan bug, see maps.py), so
//            de_dust.txt describes a map the app never projects. Comparing its
//            numbers against Dust II's entry reports a mismatch that must not
//            be "fixed".
//   variant  a name the VPK ships alongside a covered map with byte-identical
//            parameters (de_ancient_night, de_inferno_s2, de_overpass_2v2 …).
//            Nothing is missing; adding entries for them would just be noise.
const near = (a, b) => a != null && b != null && Math.abs(a - b) < 0.01;
const sameParams = (a, b) => near(a.pos_x, b.pos_x) && near(a.pos_y, b.pos_y) &&
                             near(a.scale, b.scale) && a.split === b.split;
const rows = [];
for (const [raw, g] of [...game].sort()) {
    const canon = MapsLogic.canonical(raw);
    if (wanted.length && !wanted.includes(raw) && !wanted.includes(canon)) continue;
    const app = MAP_LIBRARY[canon] || null;
    const cfg = MapLayers.configFor(canon);
    const appSplit = cfg ? cfg.split : null;

    if (canon !== raw) { rows.push({ raw, canon, game: g, kind: 'legacy' }); continue; }
    if (!app) {
        // Identical to a map that IS covered? Then it is a variant, not a gap.
        const twin = [...game].find(([r2, g2]) =>
            r2 !== raw && MAP_LIBRARY[MapsLogic.canonical(r2)] && sameParams(g, g2));
        if (twin) { rows.push({ raw, canon, game: g, kind: 'variant', twin: twin[0] }); continue; }
        rows.push({ raw, canon, game: g, kind: 'missing' });
        continue;
    }
    const calibOk = near(app.x, g.pos_x) && near(app.y, g.pos_y) && near(app.scale, g.scale);
    const splitOk = appSplit === g.split;
    rows.push({ raw, canon, game: g, app, appSplit, calibOk, splitOk,
                kind: calibOk && splitOk ? 'ok' : 'mismatch' });
}

if (asJson) {
    console.log(JSON.stringify(rows, null, 2));
    process.exit(0);
}

const fmt = v => (v == null ? '-' : String(v));
console.log();
console.log(`${'map'.padEnd(22)}${'pos_x'.padStart(8)}${'pos_y'.padStart(8)}${'scale'.padStart(11)}${'split'.padStart(9)}   status`);
let mismatched = 0, missing = 0;
for (const r of rows) {
    let status;
    switch (r.kind) {
        case 'legacy':  status = `legacy spelling - app reads it as ${r.canon}, not compared`; break;
        case 'variant': status = `variant of ${r.twin}, already covered`; break;
        case 'missing': status = 'not in MAP_LIBRARY'; missing++; break;
        case 'ok':      status = 'ok'; break;
        default:
            status = !r.calibOk
                ? `MISMATCH: app has x ${r.app.x} y ${r.app.y} scale ${r.app.scale}`
                : `MISMATCH: app split ${fmt(r.appSplit)}`;
            mismatched++;
    }
    console.log(`${r.raw.padEnd(22)}${fmt(r.game.pos_x).padStart(8)}${fmt(r.game.pos_y).padStart(8)}` +
                `${fmt(r.game.scale).padStart(11)}${fmt(r.game.split).padStart(9)}   ${status}`);
}

const add = rows.filter(r => r.kind === 'missing');
if (add.length) {
    console.log('\n// paste into MAP_LIBRARY (static/match2d.logic.js):');
    for (const r of add) {
        console.log(`        "${r.canon}":${' '.repeat(Math.max(1, 13 - r.canon.length))}` +
                    `{ scale: ${r.game.scale}, x: ${r.game.pos_x}, y: ${r.game.pos_y} },`);
    }
    const layered = add.filter(r => r.game.split != null);
    if (layered.length) {
        console.log('\n// paste into LAYERS (static/maplayers.logic.js):');
        for (const r of layered) console.log(`        ${r.canon}: { split: ${r.game.split} },`);
    }
}

console.log(`\n${rows.length} maps checked · ${mismatched} mismatched · ${missing} not in MAP_LIBRARY`);
process.exit(mismatched ? 1 : 0);
