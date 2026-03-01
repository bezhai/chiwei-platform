#!/usr/bin/env bash
# Install k3s server (control plane + worker) on the infra node.
# Usage: bash install-server.sh [--tls-san <extra-ip>]
#
# Prerequisites:
#   - swap disabled (sudo swapoff -a)
#   - curl, open-iscsi, nfs-common installed
#
# After installation:
#   - kubeconfig: /etc/rancher/k3s/k3s.yaml
#   - node token: /var/lib/rancher/k3s/server/node-token
set -euo pipefail

TLS_SAN="${1:-}"

echo "==> [1/4] Pre-flight checks"
if [ "$(wc -l < /proc/swaps)" -gt 1 ]; then
  echo "WARNING: swap is enabled. Disabling for this session..."
  sudo swapoff -a 2>/dev/null || echo "  swapoff not found, but k3s can tolerate swap."
fi

echo "==> [2/4] Installing k3s server"
INSTALL_ARGS=(
  server
  --disable=traefik
  --disable=servicelb
  --write-kubeconfig-mode=644
  --node-label=node-role=infra
)

if [ -n "$TLS_SAN" ]; then
  INSTALL_ARGS+=(--tls-san="$TLS_SAN")
else
  # Default: use the machine's primary IP
  DEFAULT_IP=$(hostname -I | awk '{print $1}')
  INSTALL_ARGS+=(--tls-san="$DEFAULT_IP")
fi

# Download install script first so we get clear error on failure
INSTALL_SCRIPT=$(mktemp)
echo "  Downloading install script..."
if ! curl -fL --connect-timeout 15 --max-time 60 https://get.k3s.io -o "$INSTALL_SCRIPT"; then
  echo "ERROR: Failed to download k3s install script from https://get.k3s.io"
  echo "  Possible causes: proxy not set, network unreachable, or site blocked."
  echo "  Check: curl -v https://get.k3s.io"
  echo ""
  echo "  Alternative: download k3s binary manually and use INSTALL_K3S_SKIP_DOWNLOAD=true"
  rm -f "$INSTALL_SCRIPT"
  exit 1
fi

echo "  Running install script..."
INSTALL_K3S_MIRROR=cn bash "$INSTALL_SCRIPT" -- "${INSTALL_ARGS[@]}"
rm -f "$INSTALL_SCRIPT"

echo "==> [3/4] Verifying installation"
sudo systemctl is-active --quiet k3s || { echo "ERROR: k3s service not running"; exit 1; }
sudo k3s kubectl get nodes

echo "==> [4/4] Node token for agents"
echo "---"
sudo cat /var/lib/rancher/k3s/server/node-token
echo "---"
echo ""
echo "Done. Use the token above when running install-agent.sh on worker nodes."
echo "Kubeconfig is at /etc/rancher/k3s/k3s.yaml"
