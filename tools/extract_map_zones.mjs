#!/usr/bin/env node
/**
 * tools/extract_map_zones.mjs - extract Valve's own named callout regions
 * (env_cs_place entities) out of each map's compiled VPK, so the Multi Match
 * Analyser can filter/breakdown by "where on the map" beyond bomb_site A/B.
 *
 *   game/csgo/maps/<map>.vpk ── Source2Viewer-CLI ──▶ maps/<map>/entities/default_ents.vents_c
 *                                                   (plain decompile: flat, ====N====-delimited
 *                                                    KeyValues blocks - env_cs_place entities
 *                                                    carry place_name/origin/angles/scales and a
 *                                                    reference to a small block-shaped model)
 *                            ── Source2Viewer-CLI ──▶ maps/<map>/entities/*_physics.gltf
 *                                                   (same folder, --gltf_export_format gltf:
 *                                                    each referenced model's glTF carries its
 *                                                    local-space POSITION accessor's min/max -
 *                                                    a ready-made axis-aligned bounding box in
 *                                                    the model's own Source hammer-unit space,
 *                                                    BEFORE the node's inches-to-meters/axis-swap
 *                                                    matrix - i.e. the same units and axes as the
 *                                                    entity's own `origin`)
 *
 * These volumes are authored as literal box brushes, so combining an entity's
 * `origin` (+ `angles`/`scales` when not identity) with its model's local AABB
 * gives an EXACT world-space box per named region - not an approximation, and
 * not something to hand-trace by eye. This is the same "measured, never
 * eyeballed" convention that already governs radar calibration
 * (tools/extract_map_calibration.mjs) and the Nuke/Vertigo floor splits.
 *
 * No new runtime dependency: everything here goes through the
 * Source2Viewer-CLI binary this repo already drives for UI assets and radar
 * calibration. A model's block-shaped mesh needs no C#/ValveResourceFormat
 * code of our own - the CLI's own glTF export already hands back the local
 * AABB as plain JSON.
 *
 * Multiple entities commonly share one place_name (e.g. "TSpawn" split into
 * several disjoint volumes) - each is written as its own box under that name
 * rather than unioned into one, so a point between two same-named pieces
 * correctly falls to whatever box (if any) actually contains it instead of a
 * hull that swallows unrelated ground.
 *
 * Output: one JSON file per canonical map name,
 *   data/analysis_data/map_zones/<canonical_map>.json
 *   [{"name": "LongA", "mins": [x,y,z], "maxs": [x,y,z]}, ...]
 * This is derived-from-Valve-content data, like static/assets/ - gitignored,
 * regenerated locally, never committed or redistributed.
 *
 * Usage:
 *   node tools/extract_map_zones.mjs                 # every calibrated map
 *   node tools/extract_map_zones.mjs de_dust2         # just these maps
 *   node tools/extract_map_zones.mjs de_nuke de_vertigo
 *
 * Configuration (the same analysis_data files extract_map_calibration.mjs reads):
 *   analysis_data/cs2_game_dir         CS2 install (contains game/csgo/maps/<map>.vpk)
 *   analysis_data/source2viewer_cli    Source2Viewer-CLI binary (falls back to
 *                                      a sibling of source2viewer_path, then PATH)
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

const { MAP_LIBRARY } = require(path.join(ROOT, 'static', 'match2d.logic.js'));

// ── args ─────────────────────────────────────────────────────────────────────
const args = process.argv.slice(2);
if (args.includes('-h') || args.includes('--help')) {
    console.log(fs.readFileSync(fileURLToPath(import.meta.url), 'utf8')
        .split('\n').filter(l => /^\s*(\/\*\*|\*)/.test(l))
        .map(l => l.replace(/^\s*(\/\*\*|\*\/?)\s?/, '')).join('\n').trimEnd());
    process.exit(0);
}
const wanted = args.length ? args : Object.keys(MAP_LIBRARY);

// ── settings, mirroring extract_map_calibration.mjs's setting() ──────────────
function dataDir() { return process.env.CS2VIEWER_DATA_DIR || path.join(ROOT, 'data'); }

function setting(name) {
    for (const cand of [path.join(dataDir(), 'analysis_data', name),
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

function resolveMapVpk(map) {
    const dir = setting('cs2_game_dir');
    if (!dir) die('CS2 game dir not configured (data/analysis_data/cs2_game_dir)');
    for (const sub of ['game/csgo/maps', 'csgo/maps', 'maps']) {
        const c = path.join(dir, sub, `${map}.vpk`);
        if (fs.existsSync(c)) return c;
    }
    return null;
}

function die(msg) { console.error(`error: ${msg}`); process.exit(1); }

// ── entity lump parsing ──────────────────────────────────────────────────────
// The plain decompile of default_ents.vents_c is flat text: ====N====-delimited
// blocks of "key value" pairs, one block per entity. Only env_cs_place blocks
// matter here; everything else (info_map_parameters, lights, props, …) is
// skipped by the classname check.
function parseEnvCsPlaces(text) {
    const blocks = text.split(/^====\d+====\s*$/m).slice(1);
    const out = [];
    for (const b of blocks) {
        if (!/classname\s+"env_cs_place"/.test(b)) continue;
        const str = k => { const m = b.match(new RegExp(`${k}\\s+"([^"]*)"`)); return m ? m[1] : null; };
        const vec = k => {
            const m = b.match(new RegExp(`${k}\\s+\\[\\s*([^\\]]+)\\]`));
            return m ? m[1].split(',').map(s => Number(s.trim())) : null;
        };
        const name = str('place_name');
        const origin = vec('origin');
        const modelMatch = b.match(/model\s+resource_name:"([^"]+)"/);
        if (!name || !origin || !modelMatch) continue;
        out.push({
            name, origin,
            angles: vec('angles') || [0, 0, 0],
            scales: vec('scales') || [1, 1, 1],
            model: modelMatch[1],
        });
    }
    return out;
}

// ── local AABB from a model's glTF physics export ──────────────────────────
function localAabbFor(gltfRoot, map, modelResource) {
    const base = path.basename(modelResource, path.extname(modelResource));
    const gltfPath = path.join(gltfRoot, 'maps', map, 'entities', `${base}_physics.gltf`);
    if (!fs.existsSync(gltfPath)) return null;
    let doc;
    try { doc = JSON.parse(fs.readFileSync(gltfPath, 'utf8')); } catch { return null; }
    const pos = (doc.accessors || []).find(a => a.type === 'VEC3' && a.min && a.max);
    if (!pos) return null;
    return { min: pos.min, max: pos.max };
}

// ── local → world AABB ───────────────────────────────────────────────────────
// Valve's AngleMatrix (mathlib), angles as QAngle order [pitch, yaw, roll]:
// world = M * (local * scale), then + origin. Every env_cs_place entity
// checked so far ships identity angles (box brushes placed axis-aligned in
// the editor), so the common path is a plain translate; the rotated path
// exists for correctness rather than because it has been exercised, and logs
// a warning so a map that actually needs it gets a by-eye sanity check.
const DEG = Math.PI / 180;
function angleMatrix([pitch, yaw, roll]) {
    const sy = Math.sin(yaw * DEG), cy = Math.cos(yaw * DEG);
    const sp = Math.sin(pitch * DEG), cp = Math.cos(pitch * DEG);
    const sr = Math.sin(roll * DEG), cr = Math.cos(roll * DEG);
    return [
        [cp * cy, sr * sp * cy - cr * sy, cr * sp * cy + sr * sy],
        [cp * sy, sr * sp * sy + cr * cy, cr * sp * sy - sr * cy],
        [-sp, sr * cp, cr * cp],
    ];
}

function worldAabb(localMin, localMax, origin, angles, scales) {
    const isIdentity = angles.every(a => Math.abs(a) < 1e-6);
    const corners = [];
    for (const x of [localMin[0], localMax[0]])
        for (const y of [localMin[1], localMax[1]])
            for (const z of [localMin[2], localMax[2]])
                corners.push([x * scales[0], y * scales[1], z * scales[2]]);
    let world = corners;
    if (!isIdentity) {
        const M = angleMatrix(angles);
        world = corners.map(([x, y, z]) => [
            M[0][0] * x + M[0][1] * y + M[0][2] * z,
            M[1][0] * x + M[1][1] * y + M[1][2] * z,
            M[2][0] * x + M[2][1] * y + M[2][2] * z,
        ]);
    }
    const mins = [0, 1, 2].map(i => Math.min(...world.map(c => c[i])) + origin[i]);
    const maxs = [0, 1, 2].map(i => Math.max(...world.map(c => c[i])) + origin[i]);
    return { mins, maxs, rotated: !isIdentity };
}

// ── per-map extraction ───────────────────────────────────────────────────────
const cli = resolveCli();
const cleanupDirs = [];
process.on('exit', () => {
    for (const d of cleanupDirs) { try { fs.rmSync(d, { recursive: true, force: true }); } catch {} }
});

function extractMap(map) {
    const vpk = resolveMapVpk(map);
    if (!vpk) { console.log(`skip  ${map}  no per-map VPK found (game/csgo/maps/${map}.vpk)`); return null; }

    const work = fs.mkdtempSync(path.join(os.tmpdir(), `map_zones_${map}_`));
    cleanupDirs.push(work);

    const entsFile = path.join(work, 'ents.txt');
    const gltfRoot = path.join(work, 'gltf');
    try {
        execFileSync(cli, ['-i', vpk, '-f', `maps/${map}/entities/default_ents.vents_c`,
                           '-o', entsFile, '-d'], { stdio: 'ignore' });
        execFileSync(cli, ['-i', vpk, '-f', `maps/${map}/entities/`,
                           '-o', gltfRoot, '-d', '--gltf_export_format', 'gltf'], { stdio: 'ignore' });
    } catch (e) {
        console.log(`FAIL  ${map}  Source2Viewer-CLI failed: ${e.message}`);
        return null;
    }
    if (!fs.existsSync(entsFile)) { console.log(`FAIL  ${map}  no entity lump in the extraction`); return null; }

    const places = parseEnvCsPlaces(fs.readFileSync(entsFile, 'utf8'));
    const zones = [];
    let missingModel = 0, rotated = 0;
    for (const p of places) {
        const local = localAabbFor(gltfRoot, map, p.model);
        if (!local) { missingModel++; continue; }
        const box = worldAabb(local.min, local.max, p.origin, p.angles, p.scales);
        if (box.rotated) rotated++;
        zones.push({ name: p.name, mins: box.mins, maxs: box.maxs });
    }

    const names = new Set(zones.map(z => z.name));
    console.log(`ok    ${map}  ${zones.length} zone box(es), ${names.size} named region(s)` +
                (missingModel ? `, ${missingModel} skipped (no model export)` : '') +
                (rotated ? `, ${rotated} rotated (verify by eye)` : ''));
    return zones;
}

console.error(`==> CLI: ${cli}`);
const outDir = path.join(dataDir(), 'analysis_data', 'map_zones');
fs.mkdirSync(outDir, { recursive: true });

let ok = 0, failed = 0;
for (const map of wanted) {
    const zones = extractMap(map);
    if (!zones) { failed++; continue; }
    fs.writeFileSync(path.join(outDir, `${map}.json`), JSON.stringify(zones, null, 2));
    ok++;
}

console.log(`\n${ok} map(s) written to ${outDir}` + (failed ? `, ${failed} failed` : ''));
process.exit(failed ? 1 : 0);
