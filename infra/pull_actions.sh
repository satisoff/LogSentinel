#!/usr/bin/env bash
# Runs ON THE LAPTOP: download a collect-episodes workflow run's artifacts, merge into data/raw, build.
#   bash infra/pull_actions.sh            # latest run of collect.yml
#   bash infra/pull_actions.sh <run-id>
set -euo pipefail
cd "$(dirname "$0")/.."
RUN_ID="${1:-$(gh run list --workflow collect.yml --limit 1 --json databaseId -q '.[0].databaseId')}"
gh run view "$RUN_ID" --json status,conclusion,displayTitle -q '"run \(.displayTitle): \(.status) \(.conclusion)"'
DEST="data/actions/$RUN_ID"
rm -rf "$DEST" && mkdir -p "$DEST" data/raw/_runners
gh run download "$RUN_ID" -D "$DEST"
for art in "$DEST"/raw-*shard-*; do
  [ -d "$art" ] || continue
  for run in "$art"/run-*; do [ -d "$run" ] && cp -r "$run" data/raw/; done
  [ -d "$art/_runners" ] && cp "$art"/_runners/* data/raw/_runners/ 2>/dev/null || true
done
echo "runs now in data/raw: $(ls -d data/raw/run-* 2>/dev/null | wc -l)"
PY=.venv/Scripts/python.exe; [ -x "$PY" ] || PY=.venv/bin/python
$PY -m logsentinel.episodes.cli build
$PY -m logsentinel.episodes.cli summary
