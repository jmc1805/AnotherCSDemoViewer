"""mapzone_extract.py - Valve's own named callout regions (env_cs_place
entities), extracted straight out of each map's compiled VPK, in-process.

Replaces tools/extract_map_zones.mjs for anyone who doesn't have Node - which
is everyone running the installed desktop app, since it ships none (see
assetindex.py, which solved the identical problem for UI icons). Stdlib plus
one subprocess call to Source2Viewer-CLI, the same binary
tools/extract_ui_assets.* already drives for icons - no new dependency.
app.py runs it in the background from the Settings page; `python
mapzone_extract.py [<map> ...]` runs it from a terminal too.

    game/csgo/maps/<map>.vpk ── Source2Viewer-CLI ──▶ maps/<map>/entities/default_ents.vents_c
                                                     (plain decompile: flat, ====N====-delimited
                                                      KeyValues blocks - env_cs_place entities
                                                      carry place_name/origin/angles/scales and a
                                                      reference to a small block-shaped model)
                              ── Source2Viewer-CLI ──▶ maps/<map>/entities/*_physics.gltf
                                                     (same folder, --gltf_export_format gltf:
                                                      each referenced model's glTF carries its
                                                      local-space POSITION accessor's min/max -
                                                      a ready-made axis-aligned bounding box in
                                                      the model's own Source hammer-unit space,
                                                      BEFORE the node's inches-to-meters/axis-swap
                                                      matrix - i.e. the same units and axes as the
                                                      entity's own `origin`)

These volumes are authored as literal box brushes, so combining an entity's
`origin` (+ `angles`/`scales` when not identity) with its model's local AABB
gives an EXACT world-space box per named region - not an approximation, and
not something to hand-trace by eye. Same "measured, never eyeballed"
convention as radar calibration (tools/extract_map_calibration.mjs, still
Node - a one-off run against a diff, not a runtime feature) and the
Nuke/Vertigo floor splits.

Output: one JSON file per canonical map name,
  data/analysis_data/map_zones/<canonical_map>.json
  [{"name": "LongA", "mins": [x,y,z], "maxs": [x,y,z]}, ...]
This is derived-from-Valve-content data, like static/assets/ - gitignored,
regenerated locally, never committed or redistributed. analysis/mapzones.py
loads and queries it; see that module for how a query result gets a "zone".
"""
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile

import maps
import paths
import procutil

# Same settings file app.py's Settings page already writes for the UI-asset
# extractor (data/analysis_data/source2viewer_cli) - one Source2Viewer-CLI
# path configured once, shared by every extractor that needs it.
_SOURCE2VIEWER_CLI_PATH = os.path.join(paths.ANALYSIS_DATA_DIR, 'source2viewer_cli')
_SOURCE2VIEWER_GUI_PATH = os.path.join(paths.ANALYSIS_DATA_DIR, 'source2viewer_path')

_MAP_LIBRARY_KEY_RE = re.compile(r'"([a-z0-9_]+)"\s*:\s*\{')
_BLOCK_SPLIT_RE = re.compile(r'^====\d+====\s*$', re.MULTILINE)
_CLASSNAME_RE = re.compile(r'classname\s+"env_cs_place"')
_MODEL_RE = re.compile(r'model\s+resource_name:"([^"]+)"')


def _load_text_setting(path):
    if not os.path.isfile(path):
        return None
    with open(path, encoding='utf-8') as f:
        return f.read().strip() or None


def calibrated_maps():
    """Every map static/match2d.logic.js has radar calibration for - the same
    default set tools/extract_map_zones.mjs offers when called with no
    arguments. Read as text rather than imported: this module has no JS
    runtime, and match2d.logic.js already ships with the app (static/ is
    always in the payload, checkout or frozen) so there is nothing extra to
    bundle."""
    path = os.path.join(paths.STATIC_DIR, 'match2d.logic.js')
    try:
        with open(path, encoding='utf-8') as f:
            text = f.read()
    except OSError:
        return []
    m = re.search(r'MAP_LIBRARY\s*=\s*\{(.*?)\n\s*\};', text, re.DOTALL)
    if not m:
        return []
    return _MAP_LIBRARY_KEY_RE.findall(m.group(1))


def zones_dir():
    return os.path.join(paths.ANALYSIS_DATA_DIR, 'map_zones')


def extracted_maps():
    """Canonical map names that already have zone data available - either
    this install's own extraction (zones_dir()) or the seed committed with
    the app (paths.MAP_ZONES_SEED_DIR); either is enough for
    analysis/mapzones.py to answer a query, so both count here. What the
    Settings page shows before/after a run, same idea as
    app.py:_assets_inventory()."""
    names = set()
    for d in (zones_dir(), paths.MAP_ZONES_SEED_DIR):
        try:
            names.update(os.path.splitext(f)[0] for f in os.listdir(d) if f.endswith('.json'))
        except OSError:
            pass
    return sorted(names)


def resolve_cli():
    """Source2Viewer-CLI's path: the dedicated setting first (same file
    tools/extract_ui_assets.* read), then derived from the GUI app's own
    setting (a sibling binary), then PATH. Mirrors extract_ui_assets.ps1's
    Get-Setting chain minus the env-var override, which only that
    developer-run script needs."""
    direct = _load_text_setting(_SOURCE2VIEWER_CLI_PATH)
    if direct and os.path.isfile(direct):
        return direct
    gui = _load_text_setting(_SOURCE2VIEWER_GUI_PATH)
    if gui:
        for name in ('Source2Viewer-CLI.exe', 'Source2Viewer-CLI'):
            cand = os.path.join(os.path.dirname(gui), name)
            if os.path.isfile(cand):
                return cand
    return shutil.which('Source2Viewer-CLI')


def _resolve_map_vpk(game_dir, map_name):
    for sub in ('game/csgo/maps', 'csgo/maps', 'maps'):
        cand = os.path.join(game_dir, *sub.split('/'), f'{map_name}.vpk')
        if os.path.isfile(cand):
            return cand
    return None


# ── entity lump parsing ──────────────────────────────────────────────────────
# The plain decompile of default_ents.vents_c is flat text: ====N====-delimited
# blocks of "key value" pairs, one block per entity. Only env_cs_place blocks
# matter here; everything else (info_map_parameters, lights, props, …) is
# skipped by the classname check.

def _str_field(block, key):
    m = re.search(r'%s\s+"([^"]*)"' % re.escape(key), block)
    return m.group(1) if m else None


def _vec_field(block, key):
    m = re.search(r'%s\s+\[\s*([^\]]+)\]' % re.escape(key), block)
    if not m:
        return None
    return [float(x.strip()) for x in m.group(1).split(',')]


def parse_env_cs_places(text):
    blocks = _BLOCK_SPLIT_RE.split(text)[1:]
    out = []
    for b in blocks:
        if not _CLASSNAME_RE.search(b):
            continue
        name = _str_field(b, 'place_name')
        origin = _vec_field(b, 'origin')
        model_m = _MODEL_RE.search(b)
        if not (name and origin and model_m):
            continue
        out.append({
            'name': name,
            'origin': origin,
            'angles': _vec_field(b, 'angles') or [0.0, 0.0, 0.0],
            'scales': _vec_field(b, 'scales') or [1.0, 1.0, 1.0],
            'model': model_m.group(1),
        })
    return out


# ── local AABB from a model's glTF physics export ───────────────────────────

def local_aabb_for(gltf_root, map_name, model_resource):
    base = os.path.splitext(os.path.basename(model_resource))[0]
    gltf_path = os.path.join(gltf_root, 'maps', map_name, 'entities', f'{base}_physics.gltf')
    if not os.path.isfile(gltf_path):
        return None
    try:
        with open(gltf_path, encoding='utf-8') as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    for a in doc.get('accessors') or []:
        if a.get('type') == 'VEC3' and a.get('min') and a.get('max'):
            return {'min': a['min'], 'max': a['max']}
    return None


# ── local → world AABB ───────────────────────────────────────────────────────
# Valve's AngleMatrix (mathlib), angles as QAngle order [pitch, yaw, roll]:
# world = M * (local * scale), then + origin. Every env_cs_place entity
# checked so far ships identity angles (box brushes placed axis-aligned in the
# editor), so the common path is a plain translate; the rotated path exists
# for correctness rather than because it has been exercised.

def _angle_matrix(angles):
    pitch, yaw, roll = angles
    d = math.pi / 180
    sy, cy = math.sin(yaw * d), math.cos(yaw * d)
    sp, cp = math.sin(pitch * d), math.cos(pitch * d)
    sr, cr = math.sin(roll * d), math.cos(roll * d)
    return (
        (cp * cy, sr * sp * cy - cr * sy, cr * sp * cy + sr * sy),
        (cp * sy, sr * sp * sy + cr * cy, cr * sp * sy - sr * cy),
        (-sp, sr * cp, cr * cp),
    )


def world_aabb(local_min, local_max, origin, angles, scales):
    is_identity = all(abs(a) < 1e-6 for a in angles)
    corners = [
        (x * scales[0], y * scales[1], z * scales[2])
        for x in (local_min[0], local_max[0])
        for y in (local_min[1], local_max[1])
        for z in (local_min[2], local_max[2])
    ]
    world = corners
    if not is_identity:
        m = _angle_matrix(angles)
        world = [
            (m[0][0] * x + m[0][1] * y + m[0][2] * z,
             m[1][0] * x + m[1][1] * y + m[1][2] * z,
             m[2][0] * x + m[2][1] * y + m[2][2] * z)
            for x, y, z in corners
        ]
    mins = [min(c[i] for c in world) + origin[i] for i in range(3)]
    maxs = [max(c[i] for c in world) + origin[i] for i in range(3)]
    return mins, maxs, not is_identity


# ── per-map extraction ───────────────────────────────────────────────────────

def extract_map(map_name, cli, game_dir, on_log=None):
    """-> list[{"name","mins","maxs"}] | None. None means "skip this map, not
    a hard failure" - a per-map VPK the user doesn't own, or a CLI hiccup,
    should not stop the other maps in the batch."""
    log = on_log or (lambda s: None)
    vpk = _resolve_map_vpk(game_dir, map_name)
    if not vpk:
        log(f'skip  {map_name}  no per-map VPK found (game/csgo/maps/{map_name}.vpk)')
        return None

    work = tempfile.mkdtemp(prefix=f'cs2viewer_zones_{map_name}_')
    try:
        ents_file = os.path.join(work, 'ents.txt')
        gltf_root = os.path.join(work, 'gltf')
        try:
            subprocess.run([cli, '-i', vpk, '-f', f'maps/{map_name}/entities/default_ents.vents_c',
                            '-o', ents_file, '-d'],
                           check=True, capture_output=True, **procutil.NO_WINDOW)
            subprocess.run([cli, '-i', vpk, '-f', f'maps/{map_name}/entities/',
                            '-o', gltf_root, '-d', '--gltf_export_format', 'gltf'],
                           check=True, capture_output=True, **procutil.NO_WINDOW)
        except (OSError, subprocess.CalledProcessError) as e:
            log(f'FAIL  {map_name}  Source2Viewer-CLI failed: {e}')
            return None
        if not os.path.isfile(ents_file):
            log(f'FAIL  {map_name}  no entity lump in the extraction')
            return None

        with open(ents_file, encoding='utf-8') as f:
            places = parse_env_cs_places(f.read())

        zones, missing_model, rotated = [], 0, 0
        for p in places:
            local = local_aabb_for(gltf_root, map_name, p['model'])
            if not local:
                missing_model += 1
                continue
            mins, maxs, is_rotated = world_aabb(local['min'], local['max'],
                                                p['origin'], p['angles'], p['scales'])
            if is_rotated:
                rotated += 1
            zones.append({'name': p['name'], 'mins': mins, 'maxs': maxs})

        names = {z['name'] for z in zones}
        extra = ''
        if missing_model:
            extra += f', {missing_model} skipped (no model export)'
        if rotated:
            extra += f', {rotated} rotated (verify by eye)'
        log(f'ok    {map_name}  {len(zones)} zone box(es), {len(names)} named region(s){extra}')
        return zones
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ── entry point ──────────────────────────────────────────────────────────────

def run(map_names=None, on_log=None):
    """Extract zones for `map_names` (default: every calibrated map) and
    write data/analysis_data/map_zones/<canonical>.json for each that
    succeeded. -> (ok, one-line message, log lines). Never raises for an
    expected failure, so a route can report it instead of 500ing - mirrors
    assetindex.finish()'s contract."""
    log = on_log or (lambda s: None)
    lines = []

    def emit(s):
        lines.append(s)
        log(s)

    cli = resolve_cli()
    if not cli:
        msg = ('Source2Viewer-CLI not found (a SEPARATE download from the GUI) - '
               'set its path on the Settings page (CS2 Installation card).')
        return False, msg, [msg]

    from clip_record import load_cs2_game_dir  # local import: avoids a hard
    # dependency on clip_record's own imports for callers that only want the
    # pure parsing/math helpers above (e.g. a future test).
    game_dir = load_cs2_game_dir()
    if not game_dir:
        msg = 'CS2 game directory not configured - set it on the Settings page first.'
        return False, msg, [msg]

    wanted = [maps.canonical(m) for m in (map_names or calibrated_maps())]
    if not wanted:
        msg = 'no calibrated maps to extract zones for'
        return False, msg, [msg]

    emit(f'==> CLI: {cli}')
    out_dir = zones_dir()
    os.makedirs(out_dir, exist_ok=True)

    ok_n, failed_n = 0, 0
    for map_name in wanted:
        zones = extract_map(map_name, cli, game_dir, on_log=emit)
        if not zones:
            failed_n += 1
            continue
        out_path = os.path.join(out_dir, f'{map_name}.json')
        tmp = out_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(zones, f, indent=2)
        os.replace(tmp, out_path)
        ok_n += 1

    summary = f'{ok_n} map(s) written to {out_dir}' + (f', {failed_n} failed' if failed_n else '')
    emit(summary)
    return ok_n > 0, summary, lines


def main(argv):
    ok, msg, lines = run(argv[1:] or None, on_log=print)
    if not ok:
        print('error: ' + msg, file=sys.stderr)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
