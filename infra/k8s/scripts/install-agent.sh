#!/usr/bin/env bash
# Install k3s agent (worker node) and join an existing cluster.
# Usage: bash install-agent.sh <server-ip> <token> [--gpu]
#
# Arguments:
#   server-ip   IP of the k3s server node
#   token       Node token from /var/lib/rancher/k3s/server/node-token
#   --gpu       (optional) Add gpu=true label to this node
#
# Prerequisites:
#   - swap disabled (sudo swapoff -a)
#   - curl, open-iscsi, nfs-common installed
#   - For GPU nodes: NVIDIA driver + nvidia-container-toolkit installed
set -euo pipefail

SERVER_IP="${1:?Usage: install-agent.sh <server-ip> <token> [--gpu]}"
TOKEN="${2:?Usage: install-agent.sh <server-ip> <token> [--gpu]}"
GPU_FLAG="${3:-}"

echo "==> [1/3] Pre-flight checks"
if [ "$(wc -l < /proc/swaps)" -gt 1 ]; then
  echo "WARNING: swap is enabled. Disabling for this session..."
  sudo swapoff -a 2>/dev/null || echo "  swapoff not found, but k3s can tolerate swap."
fi

echo "==> [2/3] Installing k3s agent (server: ${SERVER_IP})"
INSTALL_ARGS=(
  agent
  --node-label=node-role=app
)

if [ "$GPU_FLAG" = "--gpu" ]; then
  INSTALL_ARGS+=(--node-label=gpu=true)
  echo "  GPU label will be applied."
fi

# Download install script first so we get clear error on failure
INSTALL_SCRIPT=$(mktemp)
echo "  Downloading install script..."
if ! curl -fL --connect-timeout 15 --max-time 60 https://get.k3s.io -o "$INSTALL_SCRIPT"; then
  echo "ERROR: Failed to download k3s install script from https://get.k3s.io"
  echo "  Possible causes: proxy not set, network unreachable, or site blocked."
  echo "  Check: curl -v https://get.k3s.io"
  rm -f "$INSTALL_SCRIPT"
  exit 1
fi

echo "  Running install script..."
INSTALL_K3S_MIRROR=cn \
  K3S_URL="https://${SERVER_IP}:6443" \
  K3S_TOKEN="$TOKEN" \
  bash "$INSTALL_SCRIPT" -- "${INSTALL_ARGS[@]}"
rm -f "$INSTALL_SCRIPT"

echo "==> [3/3] Verifying agent"
sudo systemctl is-active --quiet k3s-agent || { echo "ERROR: k3s-agent service not running"; exit 1; }

echo "Done. This node should now appear in 'kubectl get nodes' on the server."

# Configure NVIDIA runtime for k3s if GPU flag is set and nvidia-ctk exists.
# NOTE: nvidia-ctk overwrites config.toml.tmpl, removing k3s defaults (CNI bin path, etc).
# We must let k3s generate its default config first, then merge the nvidia runtime in.
if [ "$GPU_FLAG" = "--gpu" ] && command -v nvidia-ctk &>/dev/null; then
  echo ""
  echo "==> Configuring NVIDIA container runtime for k3s..."
  TMPL="/var/lib/rancher/k3s/agent/etc/containerd/config.toml.tmpl"

  # Wait for k3s to generate the default containerd config
  sleep 3
  # Get the running config as our base template
  sudo cp /var/lib/rancher/k3s/agent/etc/containerd/config.toml "$TMPL"

  # Append nvidia runtime to the existing config (merge, not overwrite)
  sudo nvidia-ctk runtime configure \
    --runtime=containerd \
    --config="$TMPL"

  sudo systemctl restart k3s-agent &
  echo "NVIDIA runtime configured. Agent restarting in background."
  echo "Deploy the NVIDIA device plugin from the server node to enable GPU scheduling."
fi
