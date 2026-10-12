#!/usr/bin/env bash
# Runs ON THE LAPTOP: fetch raw runs (and collection logs) from the VM, then build episodes locally.
#   bash infra/pull.sh ubuntu@<vm-ip>
set -euo pipefail
VM="${1:?usage: pull.sh user@host}"
KEY="$HOME/.ssh/logsentinel"; SSH=(ssh -o StrictHostKeyChecking=accept-new); [ -f "$KEY" ] && SSH+=(-i "$KEY")
cd "$(dirname "$0")/.."
mkdir -p data
"${SSH[@]}" "$VM" 'cd ~/LogSentinel && tar -czf - --exclude="data/raw/_live_logs*" data/raw $(ls data/*.log 2>/dev/null)' | tar -xzf - -C .
PY=.venv/Scripts/python.exe; [ -x "$PY" ] || PY=.venv/bin/python
$PY -m logsentinel.episodes.cli build
$PY -m logsentinel.episodes.cli summary
