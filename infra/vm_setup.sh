#!/usr/bin/env bash
# Runs ON THE VM (called by push.sh): Python 3.12 venv + dependencies, then a sanity check.
set -euo pipefail
cd ~/LogSentinel
export PATH="$HOME/.local/bin:$PATH"
until [ -f /var/lib/cloud/instance/logsentinel-ready ]; do echo "waiting for cloud-init..."; sleep 10; done
uv python install 3.12
[ -d .venv ] || uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e ".[dev,testbed]" -q
.venv/bin/python -m pytest -q -p no:warnings tests/test_labeler.py tests/test_scenarios.py tests/test_stacks.py | tail -1
docker info --format 'docker {{.ServerVersion}} on {{.Architecture}}, {{.NCPU}} CPUs, {{.MemTotal}} bytes RAM'
echo "VM ready. Suggested parallel stacks: $(( $(nproc) / 2 > 0 ? $(nproc) / 2 : 1 ))"
