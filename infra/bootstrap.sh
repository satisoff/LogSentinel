#!/usr/bin/env bash
# Same steps as cloud-init.yaml, for VMs where cloud-init user data was not applied. Run with sudo.
set -euo pipefail
apt-get update -y && apt-get install -y git curl tmux htop jq
command -v docker >/dev/null || curl -fsSL https://get.docker.com | sh
systemctl enable --now docker
USER_NAME="${SUDO_USER:-ubuntu}"
usermod -aG docker "$USER_NAME" || true
sudo -u "$USER_NAME" -H bash -lc 'command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh'
mkdir -p /var/lib/cloud/instance && touch /var/lib/cloud/instance/logsentinel-ready
