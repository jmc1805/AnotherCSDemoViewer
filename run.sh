#!/usr/bin/env bash
# run.sh - launcher (Linux/macOS). Run it from a terminal: double-clicking a
# bare .sh doesn't reliably open one.
# Ctrl+C or closing the terminal window stops the server (plain foreground
# process, no daemonizing). Keep in lockstep with run.bat.
set -e
cd "$(dirname "$0")"
python3 serve.py
