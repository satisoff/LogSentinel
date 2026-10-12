#!/usr/bin/env bash
# Runs ON THE VM: N parallel testbed stacks share one scenario x seed job list, then episodes are built.
#   STACKS=4 SCENARIOS="S1 S2 S3 S4 S5 S6 S7 S8 S9" SEEDS=0-15 bash infra/collect.sh
# Start it detached so it survives SSH disconnects (infra/README.md):
#   nohup bash infra/collect.sh > data/collect.log 2>&1 &
set -u
cd "$(dirname "$0")/.." || exit 1
PY=.venv/bin/python
STACKS="${STACKS:-$(( $(nproc) / 2 > 0 ? $(nproc) / 2 : 1 ))}"
SCENARIOS="${SCENARIOS:-S1 S2 S3 S4 S5 S6 S7 S8 S9}"
SEEDS="${SEEDS:-0}"
echo "== collect start $(date -u +%FT%TZ) stacks=$STACKS scenarios=[$SCENARIOS] seeds=$SEEDS"
for k in $(seq 0 $((STACKS - 1))); do
  LS_STACK=$k $PY -m orchestrator.cli up || { echo "stack $k failed to start"; exit 1; }
done
pids=()
for k in $(seq 0 $((STACKS - 1))); do
  # shellcheck disable=SC2086
  LS_STACK=$k $PY -m orchestrator.cli batch $SCENARIOS --seeds "$SEEDS" --shard "$k" --shards "$STACKS" \
    > "data/collect_stack$k.log" 2>&1 &
  pids+=($!)
done
for pid in "${pids[@]}"; do wait "$pid"; done
echo "== batches done $(date -u +%FT%TZ)"
for k in $(seq 0 $((STACKS - 1))); do LS_STACK=$k $PY -m orchestrator.cli down; done
$PY -m logsentinel.episodes.cli build
$PY -m logsentinel.episodes.cli summary
echo "== collect finished $(date -u +%FT%TZ)"
