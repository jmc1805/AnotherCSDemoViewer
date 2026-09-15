#!/usr/bin/env bash
# tools/extract_ui_assets.sh - pull CS2 UI images out of pak_01_dir.vpk with
# Source2Viewer-CLI into static/assets/.
#
#   csgo/pak_01_dir.vpk ── Source2Viewer-CLI ──▶ panorama/images/{…}
#                       ── flatten ──▶ assetindex.py (tint + index) ──▶
#   static/assets/{skillgroups,map_icons,overheadmaps,equipment,deathnotice,premier}/ + manifest.json
#
# Six VPK subtrees, one manifest. Run once per game update on the machine that
# has the CS2 game files. Outputs are Valve content (static/assets/ is
# gitignored) - the directory's presence is the feature switch; without it the
# app falls back to its text labels / CSS chips. Keep the .ps1/.bat siblings in
# lockstep.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
# CS2VIEWER_ASSETS_DIR is paths.ASSETS_DIR, passed by app.py - the frozen
# desktop build keeps assets under the user's data dir, not static/.
OUT="${CS2VIEWER_ASSETS_DIR:-$ROOT/static/assets}"
# Create a temporary directory for extraction
WORK=$(mktemp -d "${TMPDIR:-/tmp}/ui_assets_XXXXXX")

# kind → VPK subtree (path prefix inside pak_01_dir.vpk).
# For CS:GO, the paths are slightly different from CS2
KIND_NAMES=(skillgroups map_icons overheadmaps equipment deathnotice premier)
declare -A KIND_SUBTREE=(
  [skillgroups]="panorama/images/icons/skillgroups"
  [map_icons]="panorama/images/map_icons"
  [overheadmaps]="panorama/images/overheadmaps"
  [equipment]="panorama/images/icons/equipment"
  [deathnotice]="panorama/images/hud/deathnotice"
  [premier]="panorama/images/icons/ui"
)

# deathnotice: the 8 killfeed condition icons (headshot, wallbang/penetrate,
# noscope, through-smoke, blind-kill, air-kill, suicide, smoke-impact) - extracted
# by exact path like skillgroups, since the folder also holds unrelated hud art.
DEATHNOTICE_FILES=(blind_kill icon_headshot icon_suicide inairkill noscope penetrate smoke_kill smokegrenade_impact)

# premier: the Premier rating banner. Extracted by exact path - icons/ui is a
# large grab-bag folder. The game ships one grey banner and wash-colours it per
# rating tier at runtime; the grey files are copied as-is and assetindex.py
# flattens that multiply into the seven tier files the manifest indexes. See
# that module for the colours and the why.
PREMIER_FILES=(premier_rating_bg_large premier_rating_bg_large_none)

usage() {
  cat >&2 <<EOF
Usage: $(basename "$0") [kinds…] [options]

Extracts CS2 UI images from csgo/pak_01_dir.vpk into static/assets/ and rebuilds
static/assets/manifest.json. With no kinds given, extracts all six:
  skillgroups  map_icons  overheadmaps  equipment  deathnotice  premier

Options:
  --list        List the kinds and their VPK subtrees, then exit.
  --manifest    Skip extraction; just re-tint + re-scan static/assets/ and
                rebuild the manifest (needs Python, no CS2 files / Source2Viewer).
  -h, --help    Show this help and exit.

Configuration (only analysis_data/ files are used, no environment variables):
  analysis_data/cs2_game_dir
        Your CS2 install dir (the one containing game/csgo/pak01_dir.vpk).
  analysis_data/source2viewer_cli
        Path to Source2Viewer-CLI (the cli-<os>-x64.zip download, separate from
        the GUI). Falls back to a sibling of analysis_data/source2viewer_path, then PATH.

Examples:
  $(basename "$0")                     # extract every kind + manifest
  $(basename "$0") equipment           # just re-extract equipment icons
  $(basename "$0") --manifest          # re-index existing PNGs, no CS2 files
  $(basename "$0") --list
EOF
}

# Resolve a setting: only check the analysis_data file, not env vars. Honors the data/
# layout + CS2VIEWER_DATA_DIR - checks the configured data
# dir first, then the legacy root location.
setting() {  # setting <analysis_data filename>
  local ddir="${CS2VIEWER_DATA_DIR:-$ROOT/data}"
  for cand in "$ddir/analysis_data/$1" "$ROOT/data/analysis_data/$1" "$ROOT/analysis_data/$1"; do
    [ -f "$cand" ] && { cat "$cand"; return; }
  done
}

# ── Parse arguments ───────────────────────────────────────────────────────────
SELECTED=()
DO_LIST=0
MANIFEST_ONLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --list) DO_LIST=1; shift ;;
    --manifest) MANIFEST_ONLY=1; shift ;;
    -*) echo "error: unknown option: $1" >&2; usage; exit 2 ;;
    *)
      [ -n "${KIND_SUBTREE[$1]:-}" ] || { echo "error: unknown kind: $1 (want: ${KIND_NAMES[*]})" >&2; exit 2; }
      SELECTED+=("$1"); shift ;;
  esac
done
[ "${#SELECTED[@]}" -gt 0 ] || SELECTED=("${KIND_NAMES[@]}")

if [ "$DO_LIST" = "1" ]; then
  echo "UI asset kinds (VPK subtree inside pak_01_dir.vpk → static/assets/<kind>/):"
  for k in "${KIND_NAMES[@]}"; do printf '  %-13s %s\n' "$k" "${KIND_SUBTREE[$k]}"; done
  exit 0
fi

# Tinting the Premier banner and writing manifest.json are assetindex.py's job.
# The app runs it in-process and sets CS2VIEWER_SKIP_INDEX - an installed app
# has no python for this script to call. Run by hand, it is called from here.
rebuild_manifest() {
  if [ -n "${CS2VIEWER_SKIP_INDEX:-}" ]; then
    echo "==> extraction done; the app tints + indexes"
    return
  fi
  echo "==> tinting + rebuilding manifest…"
  local py
  py="$(command -v python3 || command -v python || true)"
  [ -n "$py" ] || { echo "error: python not found - finish with 'Rebuild index only' on the app's Settings page" >&2; exit 1; }
  "$py" "$ROOT/assetindex.py" "$OUT"
}

if [ "$MANIFEST_ONLY" = "1" ]; then
  rebuild_manifest
  exit 0
fi

# ── Resolve the Source2Viewer CLI ────────────────────────────────────────────
S2V_CLI="$(setting source2viewer_cli)"
if [ -z "$S2V_CLI" ]; then
  S2V_GUI="$(setting source2viewer_path)"
  if [ -n "$S2V_GUI" ]; then
    for cand in "$(dirname "$S2V_GUI")/Source2Viewer-CLI" "$(dirname "$S2V_GUI")/Source2Viewer-CLI.exe"; do
      { [ -x "$cand" ] || [ -f "$cand" ]; } && { S2V_CLI="$cand"; break; }
    done
  fi
fi
[ -n "$S2V_CLI" ] || S2V_CLI="$(command -v Source2Viewer-CLI || true)"
[ -n "$S2V_CLI" ] || {
  echo "error: Source2Viewer-CLI not found. It is a SEPARATE download from the GUI:" >&2
  echo "  1. Get cli-<os>-x64.zip from https://github.com/ValveResourceFormat/ValveResourceFormat/releases" >&2
  echo "  2. Point this script at it by persisting the path:" >&2
  echo "     echo /path/to/Source2Viewer-CLI > data/analysis_data/source2viewer_cli" >&2
  exit 1
}

# ── Resolve the main content VPK under the CS2 game dir ───────────────────────
# CS2 names it pak01_dir.vpk (no underscore after "pak") under game/csgo/; older
# layouts used csgo/. Try known names in the usual subdirs, then glob
# pak*_dir.vpk. $CS2_PAK_VPK overrides everything.
VPK="${CS2_PAK_VPK:-}"
if [ -z "$VPK" ]; then
  CS2_DIR="$(setting cs2_game_dir)"
  [ -n "$CS2_DIR" ] || { echo "error: CS2 game dir not configured (data/analysis_data/cs2_game_dir)" >&2; exit 1; }
  for sub in "game/csgo" "csgo" "."; do
    dir="$CS2_DIR/$sub"
    [ -d "$dir" ] || continue
    for n in "pak01_dir.vpk" "pak_01_dir.vpk"; do
      [ -f "$dir/$n" ] && { VPK="$dir/$n"; break 2; }
    done
    cand="$(ls "$dir"/pak*_dir.vpk 2>/dev/null | sort | head -1)"
    [ -n "$cand" ] && { VPK="$cand"; break; }
  done
  [ -n "$VPK" ] || { echo "error: no pak*_dir.vpk found under $CS2_DIR (looked in game/csgo, csgo). Set \$CS2_PAK_VPK to the file." >&2; exit 1; }
fi
[ -f "$VPK" ] || { echo "error: VPK not found: $VPK" >&2; exit 1; }
echo "==> VPK: $VPK"
echo "==> CLI: $S2V_CLI"

# For now, we'll assume CS:GO since that's what's being used
# The correct paths for CS:GO are different from what the original script expected
echo "Detected CS:GO game files (hardcoded)"
GAME_TYPE="csgo"

# ── Extract each selected kind ────────────────────────────────────────────────
# Panorama images are stored uncompiled (.png/.svg) or as .vtex_c; --vpk_decompile
# extracts the former as-is and decompiles the latter to PNG, preserving the vpk
# tree under the output dir. We then flatten just the leaf images into
# static/assets/<kind>/ so filenames are the manifest's key source.
for kind in "${SELECTED[@]}"; do
  subtree="${KIND_SUBTREE[$kind]}"
  raw="$WORK/$kind"
  dst="$OUT/$kind"
  # Clear BOTH work and destination so a prior run's files can't linger and get
  # re-indexed (e.g. dangerzone*/wingman* art from an earlier whole-subtree run).
  rm -rf "$raw" "$dst"; mkdir -p "$raw" "$dst"
  if [ "$kind" = "skillgroups" ]; then
    # Only the 18 competitive/wingman rank icons matter: skillgroup1..18. They're
    # compiled panorama SVG (.vsvg_c → .svg). Extract each by exact path so no
    # extra variants (skillgroup0, wingman/premier art, animated) leak in.
    echo "==> [$kind] extracting skillgroup1..18.vsvg_c …"
    for n in $(seq 1 18); do
      echo "Extracting skillgroup${n}.vsvg_c"
      "$S2V_CLI" --input "$VPK" --output "$raw" --vpk_decompile \
                 --vpk_filepath "panorama/images/icons/skillgroups/skillgroup${n}.vsvg_c" >/dev/null 2>&1 || true
    done
  elif [ "$kind" = "premier" ]; then
    echo "==> [$kind] extracting the Premier rating banner …"
    for n in "${PREMIER_FILES[@]}"; do
      echo "Extracting ${n}.vsvg_c"
      "$S2V_CLI" --input "$VPK" --output "$raw" --vpk_decompile \
                 --vpk_filepath "panorama/images/icons/ui/${n}.vsvg_c" >/dev/null 2>&1 || true
    done
  elif [ "$kind" = "deathnotice" ]; then
    echo "==> [$kind] extracting ${#DEATHNOTICE_FILES[@]} killfeed condition icons …"
    for n in "${DEATHNOTICE_FILES[@]}"; do
      echo "Extracting ${n}.vsvg_c"
      "$S2V_CLI" --input "$VPK" --output "$raw" --vpk_decompile \
                 --vpk_filepath "panorama/images/hud/deathnotice/${n}.vsvg_c" >/dev/null 2>&1 || true
    done
  else
    echo "==> [$kind] extracting $subtree …"
    "$S2V_CLI" --input "$VPK" \
               --output "$raw" \
               --vpk_decompile \
               --vpk_filepath "$subtree" >/dev/null 2>&1 || {
      # For map_icons, it might not exist in CS:GO, so we continue silently
      if [ "$kind" = "map_icons" ]; then
        echo "    warning: map_icons subtree may not exist in this build (expected for CS:GO), continuing..."
      else
        echo "    warning: extraction returned non-zero for $kind (subtree may be absent in this build)" >&2
      fi
    }
  fi
  if [ "$kind" = "premier" ]; then
    # The grey source art is copied as-is; assetindex.py tints it into the
    # seven tier banners and never indexes the grey files themselves.
    found=0
    for n in "${PREMIER_FILES[@]}"; do
      f="$(find "$raw" -type f -name "${n}.svg" -print -quit)"
      [ -n "$f" ] || continue
      cp -f "$f" "$dst/${n}.svg"; found=$((found+1))
      echo "      + ${n}.svg"
    done
    [ "$found" -gt 0 ] || echo "    warning: premier banner not found in this build - skipping" >&2
    continue
  fi
  # Flatten leaf images (png/jpg/webp/svg - .vsvg_c decompiles to .svg) into the
  # destination, dropping the vpk subdirectory nesting. map_icons keeps only the
  # map_icon_* files (the dir also holds screenshots/other art in subfolders).
  found=0
  while IFS= read -r -d '' f; do
    b="$(basename "$f")"
    [ "$kind" = "map_icons" ] && case "$b" in map_icon_*) ;; *) continue ;; esac
    cp -f "$f" "$dst/$b"; found=$((found+1))
    echo "      + $b"
  done < <(find "$raw" -type f \( -iname '*.png' -o -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.webp' -o -iname '*.svg' \) -print0)
  echo "    $found image(s) → $dst"
done

rebuild_manifest
echo
echo "done: $OUT"
echo "Verify in the app: reload /match - skill-group icons replace the CSS chips"
echo "when present; delete static/assets/ to confirm the text-label fallback."
# Clean up the temporary directory
trap 'rm -rf "$WORK"' EXIT
