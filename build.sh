#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

# Builds the Go binaries: the demo parser and the Overwatch 2nd-pass extractor.
usage() {
  cat >&2 <<EOF
Usage: $(basename "$0") [options]

Builds the Go binaries (parser, overwatch) into bin/ - that's the whole app
(2D viewer, match stats, Players, and the Overwatch layer).

Options:
  -h, --help   Show this help and exit.
EOF
}

for arg in "$@"; do
  case "$arg" in
    -h|--help) usage; exit 0 ;;
    *) echo "unknown flag: $arg" >&2; usage; exit 2 ;;
  esac
done

echo "Downloading dependencies..."
go mod tidy
mkdir -p bin   # compiled binaries live under bin/
echo "Building parser..."
go build -o bin/parser ./cmd/parser
echo "Building overwatch extractor..."
go build -o bin/overwatch ./cmd/overwatch

echo "Done. Binaries: $(pwd)/bin/parser, $(pwd)/bin/overwatch"
