#!/usr/bin/env bash
# Copy kubeconfig from k3s server to the local dev machine.
# Usage: bash setup-dev-kubeconfig.sh <server-ip>
#
# This script:
#   1. Copies /etc/rancher/k3s/k3s.yaml from the server
#   2. Replaces 127.0.0.1 with the server's actual IP
#   3. Saves to ~/.kube/config
set -euo pipefail

SERVER_IP="${1:?Usage: setup-dev-kubeconfig.sh <server-ip>}"
KUBE_DIR="$HOME/.kube"
KUBE_CONFIG="$KUBE_DIR/config"

echo "==> [1/4] Checking kubectl"
if ! command -v kubectl &>/dev/null; then
  echo "kubectl not found. Installing..."
  KUBECTL_VERSION=$(curl -L -s https://dl.k8s.io/release/stable.txt)
  curl -LO "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl"
  chmod +x kubectl
  sudo mv kubectl /usr/local/bin/
fi

echo "==> [2/4] Copying kubeconfig from ${SERVER_IP}"
mkdir -p "$KUBE_DIR"

if [ -f "$KUBE_CONFIG" ]; then
  BACKUP="$KUBE_CONFIG.backup.$(date +%Y%m%d%H%M%S)"
  echo "  Backing up existing config to $BACKUP"
  cp "$KUBE_CONFIG" "$BACKUP"
fi

scp "${SERVER_IP}:/etc/rancher/k3s/k3s.yaml" "$KUBE_CONFIG"

echo "==> [3/4] Updating server address to ${SERVER_IP}"
sed -i "s/127.0.0.1/${SERVER_IP}/g" "$KUBE_CONFIG"
chmod 600 "$KUBE_CONFIG"

echo "==> [4/4] Verifying connection"
kubectl cluster-info
kubectl get nodes

echo ""
echo "Done. kubeconfig saved to $KUBE_CONFIG"
