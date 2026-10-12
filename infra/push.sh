#!/usr/bin/env bash
# Runs ON THE LAPTOP (Git Bash): copy the committed code to the VM and prepare it.
#   bash infra/push.sh ubuntu@<vm-ip>        (uses ~/.ssh/logsentinel if present)
# Only committed files are sent (git archive): no data, no venv, no secrets.
set -euo pipefail
VM="${1:?usage: push.sh user@host}"
KEY="$HOME/.ssh/logsentinel"; SSH=(ssh -o StrictHostKeyChecking=accept-new); [ -f "$KEY" ] && SSH+=(-i "$KEY")
cd "$(dirname "$0")/.."
git archive --format=tar HEAD | "${SSH[@]}" "$VM" 'mkdir -p ~/LogSentinel && tar -x -C ~/LogSentinel'
"${SSH[@]}" "$VM" 'bash ~/LogSentinel/infra/vm_setup.sh'
