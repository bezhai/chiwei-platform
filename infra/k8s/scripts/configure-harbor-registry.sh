#!/usr/bin/env bash
# Configure a k3s node to trust a Harbor registry over HTTPS.
#
# What it does:
#   1. Write /etc/rancher/k3s/registries.yaml (auth credentials + TLS settings)
#   2. Restart k3s / k3s-agent
#   3. Ensure containerd picks up the HTTPS registry (no manual hosts.toml edits)
#
# Usage:
#   sudo bash configure-harbor-registry.sh [username] [password]
#   Optional env:
#     HARBOR_ADDR (default: harbor.local:30002)
#     HARBOR_USER (default: admin)
#     HARBOR_PASS (default: read from ../harbor/values.local.yaml if present)
#   Example: sudo bash configure-harbor-registry.sh admin mypassword
#
# Run this on EVERY k3s node (server + agents).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VALUES_FILE="${SCRIPT_DIR}/../harbor/values.local.yaml"

HARBOR_ADDR="${HARBOR_ADDR:-harbor.local:30002}"
HARBOR_USER="${HARBOR_USER:-${1:-admin}}"
HARBOR_PASS="${HARBOR_PASS:-${2:-}}"

if [[ -z "${HARBOR_PASS}" && -f "${VALUES_FILE}" ]]; then
  HARBOR_PASS="$(awk -F': ' '/^harborAdminPassword:/ {gsub(/"/,"",$2); print $2}' "${VALUES_FILE}")"
fi

if [[ -z "${HARBOR_PASS}" ]]; then
  echo "ERROR: Harbor password not provided."
  echo "Provide it as the second argument or via HARBOR_PASS env."
  exit 1
fi

REGISTRIES_FILE="/etc/rancher/k3s/registries.yaml"
CERTS_DIR="/var/lib/rancher/k3s/agent/etc/containerd/certs.d/${HARBOR_ADDR}"
HOSTS_FILE="${CERTS_DIR}/hosts.toml"

# --- Step 1: Write registries.yaml (provides auth + TLS settings) ---
echo "==> [1/3] Writing ${REGISTRIES_FILE}"
mkdir -p /etc/rancher/k3s

cat > "$REGISTRIES_FILE" <<YAML
mirrors:
  "${HARBOR_ADDR}":
    endpoint:
      - "https://${HARBOR_ADDR}"
configs:
  "${HARBOR_ADDR}":
    auth:
      username: ${HARBOR_USER}
      password: ${HARBOR_PASS}
    tls:
      insecure_skip_verify: true
YAML

echo "  Done."

# --- Step 2: Restart k3s service ---
echo "==> [2/3] Restarting k3s service..."
if systemctl is-active --quiet k3s; then
  systemctl restart k3s
  SVC="k3s"
elif systemctl is-active --quiet k3s-agent; then
  systemctl restart k3s-agent
  SVC="k3s-agent"
else
  echo "ERROR: Neither k3s nor k3s-agent is running."
  exit 1
fi
echo "  Restarted ${SVC}. Waiting for containerd to generate config..."
sleep 5

# --- Step 3: Verify hosts.toml exists ---
echo "==> [3/3] Verifying ${HOSTS_FILE} exists"
if [[ ! -f "$HOSTS_FILE" ]]; then
  echo "  WARNING: ${HOSTS_FILE} not found yet. It should be generated after restart."
else
  echo "  Found ${HOSTS_FILE}."
fi
echo ""
echo "=== Configuration complete ==="
echo "Verify with: sudo crictl pull ${HARBOR_ADDR}/${HARBOR_PROJECT:-inner-bot}/lark-server:<tag>"
