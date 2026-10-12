#!/usr/bin/env bash
# M4 pilot batch: one full episode per scenario (~10 h), then build episodes and print the summary.
# Run from Git Bash in the repo root, in your own terminal (not inside a Claude Code session):
#   bash scripts/run_pilot.sh 2>&1 | tee data/pilot.log
# Before starting: start Docker Desktop, stop other Docker stacks, close memory-heavy apps,
# keep the laptop plugged in with the lid open.
set -u
cd "$(dirname "$0")/.." || exit 1
PY=.venv/Scripts/python.exe; [ -x "$PY" ] || PY=.venv/bin/python
SEEDS="${SEEDS:-0}"
SCENARIOS="${SCENARIOS:-S1 S2 S3 S4 S5 S6 S7 S8 S9}"
MARKER="data/.pilot.running"
touch "$MARKER"
trap 'rm -f "$MARKER"' EXIT
if command -v powershell >/dev/null 2>&1; then  # Windows laptop: prevent sleep
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts/keepawake.ps1 -Marker "$MARKER" &
fi
echo "== pilot start $(date -u +%FT%TZ)  scenarios: $SCENARIOS  seeds: $SEEDS"
$PY -m orchestrator.cli up || exit 1
# shellcheck disable=SC2086
$PY -m orchestrator.cli batch $SCENARIOS --seeds "$SEEDS"
echo "== batch done $(date -u +%FT%TZ)"
$PY -m orchestrator.cli down
$PY -m logsentinel.episodes.cli build
$PY -m logsentinel.episodes.cli summary
echo "== pilot finished $(date -u +%FT%TZ)"
