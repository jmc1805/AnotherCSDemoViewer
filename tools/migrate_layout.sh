#!/usr/bin/env bash
# tools/migrate_layout.sh - one-time move to the data/ + bin/ layout.
# Relocates existing generated data and compiled
# binaries from their old scattered locations into data/ and bin/.
#
# Safe to run repeatedly: each move is skipped if the source is already gone
# (already migrated) and refuses to overwrite a non-empty destination. All moves
# are `mv` on the same filesystem - instant even for the multi-GB chunks dir, no
# copying. Nothing here is destructive: to undo, mv the entries back.
#
#   tools/migrate_layout.sh            # migrate
#   tools/migrate_layout.sh --dry-run  # show what would move, change nothing
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

moved=0 skipped=0
move() {  # move <src> <dst>
  local src="$1" dst="$2"
  if [ ! -e "$src" ]; then echo "  skip (already migrated / absent): $src"; skipped=$((skipped+1)); return; fi
  if [ -e "$dst" ]; then
    echo "  CONFLICT: $dst already exists - leaving $src in place; merge manually" >&2
    skipped=$((skipped+1)); return
  fi
  if [ "$DRY" = "1" ]; then echo "  would move: $src -> $dst"; return; fi
  mkdir -p "$(dirname "$dst")"
  mv "$src" "$dst"
  echo "  moved: $src -> $dst"; moved=$((moved+1))
}

echo "== generated data -> data/ =="
move demos             data/demos
move processed_matches data/processed_matches
move static/chunks     data/chunks
move overwatch_raw     data/overwatch_raw
move analysis_data     data/analysis_data
move demo_map.json     data/demo_map.json

echo "== compiled binaries -> bin/ =="
move parser        bin/parser
move overwatch     bin/overwatch
move parser.exe    bin/parser.exe
move overwatch.exe bin/overwatch.exe

echo
if [ "$DRY" = "1" ]; then
  echo "dry run - nothing changed."
else
  echo "done: $moved moved, $skipped skipped."
  echo "The app now reads data/ and bin/ (paths.py). Old locations are empty."
fi
