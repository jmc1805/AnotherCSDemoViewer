#!/usr/bin/env bash
# tools/linuxtest/linux_test.sh - run the app's acceptance plan on Ubuntu,
# in Docker, from any host (this is the half that knows about the host).
#
#   ./tools/linuxtest/linux_test.sh [options]
#
# The container never writes into this checkout: the repo goes in read-only at
# /src and run_tests.py stages a writable copy. The gitignored directories the
# app cannot run without (data/, static/assets/ incl. the radar PNGs) are mounted
# read-only too, because a clone alone does not carry them.
#
# Options:
#   --fast          skip Playwright/Chromium and Go (much quicker image build,
#                   loses the real-browser smoke and the parser build/test)
#   --no-browser    skip Playwright/Chromium only
#   --no-go         skip the Go toolchain only
#   --no-cs2        do not mount the CS2 install (asset extraction will SKIP)
#   --with-demo F   mount demo F and parse it end-to-end (needs Go)
#   --rebuild       force a full image rebuild (--no-cache)
#   --shell         drop into a shell in the container instead of testing
#
# Requires: docker (Linux containers). On Windows run it from Git Bash.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
IMAGE=cs2viewer-linuxtest

WITH_GO=1
WITH_BROWSER=1
USE_CS2=1
DEMO=''
REBUILD=0
SHELL_MODE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --fast)       WITH_GO=0; WITH_BROWSER=0; shift ;;
    --no-browser) WITH_BROWSER=0; shift ;;
    --no-go)      WITH_GO=0; shift ;;
    --no-cs2)     USE_CS2=0; shift ;;
    --with-demo)  DEMO="${2:-}"; shift 2 ;;
    --rebuild)    REBUILD=1; shift ;;
    --shell)      SHELL_MODE=1; shift ;;
    -h|--help)    sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

command -v docker >/dev/null || { echo "error: docker not found on PATH" >&2; exit 1; }
docker info >/dev/null 2>&1 || {
  echo "error: the Docker daemon is not responding - is Docker Desktop running?" >&2; exit 1; }

# Docker Desktop on Windows needs native paths, and Git Bash rewrites anything
# that looks like one unless MSYS_NO_PATHCONV is set. Detect the host style once
# and translate every mount source through the same function.
WINHOST=0
case "$(uname -s 2>/dev/null || echo unknown)" in
  MINGW*|MSYS*|CYGWIN*) WINHOST=1 ;;
esac

hostpath() {  # hostpath <unix-ish path> -> path docker will accept
  if [ "$WINHOST" = "1" ]; then cygpath -w "$1"; else printf '%s' "$1"; fi
}

DATA_DIR="${CS2VIEWER_DATA_DIR:-$ROOT/data}"
[ -d "$DATA_DIR" ] || { echo "error: no data dir at $DATA_DIR" >&2; exit 1; }
[ -d "$ROOT/static/assets/overheadmaps" ] || echo "warning: $ROOT/static/assets/overheadmaps is missing - the 2D viewer cannot draw without radar PNGs" >&2

# ── build ────────────────────────────────────────────────────────────────────
echo "==> building $IMAGE (WITH_GO=$WITH_GO WITH_BROWSER=$WITH_BROWSER)"
BUILD_ARGS=(--build-arg "WITH_GO=$WITH_GO" --build-arg "WITH_BROWSER=$WITH_BROWSER")
[ "$REBUILD" = "1" ] && BUILD_ARGS+=(--no-cache)
# requirements.txt is the only thing COPYed, so the repo root is the context.
MSYS_NO_PATHCONV=1 docker build "${BUILD_ARGS[@]}" \
  -f "$(hostpath "$HERE/Dockerfile")" -t "$IMAGE" "$(hostpath "$ROOT")"

# ── mounts ───────────────────────────────────────────────────────────────────
MOUNTS=(-v "$(hostpath "$ROOT"):/src:ro"
        -v "$(hostpath "$DATA_DIR"):/seed/data:ro")
[ -d "$ROOT/static/assets" ] && MOUNTS+=(-v "$(hostpath "$ROOT/static/assets"):/seed/assets:ro")

# The CS2 install, for the asset-extraction and calibration checks. Read from
# the app's own setting so there is one place that knows where CS2 lives.
if [ "$USE_CS2" = "1" ]; then
  CS2_DIR=''
  for cand in "$DATA_DIR/analysis_data/cs2_game_dir" "$ROOT/data/analysis_data/cs2_game_dir"; do
    [ -f "$cand" ] && { CS2_DIR="$(tr -d '\r\n' < "$cand")"; break; }
  done
  if [ -n "$CS2_DIR" ]; then
    CSGO_DIR=''
    for sub in "game/csgo" "csgo"; do
      # The setting is a native path; on Windows convert it to test for the file.
      probe="$CS2_DIR/$sub"
      [ "$WINHOST" = "1" ] && probe="$(cygpath -u "$CS2_DIR")/$sub"
      [ -f "$probe/pak01_dir.vpk" ] && { CSGO_DIR="$probe"; break; }
    done
    if [ -n "$CSGO_DIR" ]; then
      echo "==> mounting CS2 game files: $CSGO_DIR"
      MOUNTS+=(-v "$(hostpath "$CSGO_DIR"):/csgo:ro")
    else
      echo "warning: no pak01_dir.vpk under $CS2_DIR - asset extraction will SKIP" >&2
    fi
  else
    echo "warning: cs2_game_dir not configured - asset extraction will SKIP" >&2
  fi
fi

if [ -n "$DEMO" ]; then
  [ -f "$DEMO" ] || { echo "error: no such demo: $DEMO" >&2; exit 1; }
  # Docker rejects a relative bind source outright ("invalid characters for a
  # local volume name") - it reads anything without a leading / or drive letter
  # as a *named volume*, not a path. Resolve before handing it over.
  DEMO="$(cd "$(dirname "$DEMO")" && pwd)/$(basename "$DEMO")"
  echo "==> mounting demo: $DEMO"
  MOUNTS+=(-v "$(hostpath "$DEMO"):/seed/demo.dem:ro")
fi

# ── run ──────────────────────────────────────────────────────────────────────
if [ "$SHELL_MODE" = "1" ]; then
  exec env MSYS_NO_PATHCONV=1 docker run --rm -it "${MOUNTS[@]}" \
       --entrypoint /bin/bash "$IMAGE"
fi

echo "==> running the acceptance plan"
set +e
MSYS_NO_PATHCONV=1 docker run --rm "${MOUNTS[@]}" "$IMAGE"
rc=$?
set -e
echo "==> container exited with $rc"
exit $rc
