#!/usr/bin/env bash
# Install a GitHub Actions self-hosted runner on the infra node (Node 1).
#
# What it does:
#   1. Create a dedicated `github-runner` user (added to `docker` group)
#   2. Install dependencies: Node.js 22, Python 3.11 + uv, kustomize, git
#   3. Configure Docker to trust Harbor self-signed certificate
#   4. Copy k3s kubeconfig for the runner
#   5. Download & configure the GitHub Actions runner
#   6. Configure proxy settings in runner .env
#   7. Register as a systemd service
#
# Usage:
#   sudo bash install-gh-runner.sh <github-repo-url> <runner-token> [https-proxy-url]
#
# Examples:
#   sudo bash install-gh-runner.sh https://github.com/user/repo AABCDEF1234 http://proxy:7890
#   sudo bash install-gh-runner.sh https://github.com/user/repo AABCDEF1234
#
# The runner token can be obtained from:
#   GitHub → Settings → Actions → Runners → New self-hosted runner
set -euo pipefail

# --- Argument parsing ---
if [[ $# -lt 2 ]]; then
  echo "Usage: sudo bash $0 <github-repo-url> <runner-token> [https-proxy-url]"
  echo "  github-repo-url: e.g. https://github.com/user/repo"
  echo "  runner-token:    from GitHub Settings → Actions → Runners"
  echo "  https-proxy-url: optional, e.g. http://proxy:7890"
  exit 1
fi

REPO_URL="$1"
RUNNER_TOKEN="$2"
PROXY_URL="${3:-}"

RUNNER_USER="github-runner"
RUNNER_HOME="/home/${RUNNER_USER}"
RUNNER_DIR="${RUNNER_HOME}/actions-runner"
RUNNER_VERSION="2.321.0"
RUNNER_ARCH="linux-x64"
RUNNER_TAR="actions-runner-${RUNNER_ARCH}-${RUNNER_VERSION}.tar.gz"
RUNNER_DOWNLOAD_URL="https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/${RUNNER_TAR}"

HARBOR_ADDR="harbor.local:30002"

echo "=== GitHub Actions Self-Hosted Runner Installer ==="
echo "  Repo:   ${REPO_URL}"
echo "  Proxy:  ${PROXY_URL:-none}"
echo ""

# --- Step 1: Create runner user ---
echo "==> [1/7] Creating user '${RUNNER_USER}'..."
if id "${RUNNER_USER}" &>/dev/null; then
  echo "  User already exists, skipping."
else
  useradd -m -s /bin/bash "${RUNNER_USER}"
  echo "  Created."
fi

# Add to docker group (for docker build/push)
if getent group docker &>/dev/null; then
  usermod -aG docker "${RUNNER_USER}"
  echo "  Added to docker group."
fi

# --- Step 2: Check & install dependencies ---
echo "==> [2/7] Checking dependencies..."

# Proxy for curl downloads in this step
CURL_PROXY=()
if [[ -n "${PROXY_URL}" ]]; then
  CURL_PROXY=(--proxy "${PROXY_URL}")
fi

# Helper: find a binary in PATH or common locations (nvm, .local/bin, etc.)
find_bin() {
  local name="$1"
  # Check current PATH first
  if command -v "${name}" &>/dev/null; then
    command -v "${name}"
    return 0
  fi
  # Search common user-installed locations (nvm, .local/bin)
  local candidate sudo_user_home=""
  if [[ -n "${SUDO_USER:-}" ]]; then
    sudo_user_home="$(getent passwd "${SUDO_USER}" | cut -d: -f6 || true)"
  fi
  for candidate in \
    "${sudo_user_home}"/.nvm/versions/node/v*/bin/"${name}" \
    /home/*/".nvm/versions/node"/v*/bin/"${name}" \
    /root/.nvm/versions/node/v*/bin/"${name}" \
    /data00/home/*/.nvm/versions/node/v*/bin/"${name}" \
    "${sudo_user_home}"/.local/bin/"${name}" \
    /home/*/.local/bin/"${name}" \
    /root/.local/bin/"${name}"; do
    if [[ -x "${candidate}" ]]; then
      echo "${candidate}"
      return 0
    fi
  done
  return 1
}

# Node.js 22
node_major() {
  "$1" -v | cut -d. -f1 | tr -d v
}

find_node_22() {
  local candidate sudo_user_home="" major
  local -a candidates=()
  local found_info=""
  local -A seen=()

  if [[ -n "${SUDO_USER:-}" ]]; then
    sudo_user_home="$(getent passwd "${SUDO_USER}" | cut -d: -f6 || true)"
  fi

  if command -v node &>/dev/null; then
    candidates+=("$(command -v node)")
  fi

  for candidate in \
    "${sudo_user_home}"/.nvm/versions/node/v*/bin/node \
    /home/*/.nvm/versions/node/v*/bin/node \
    /root/.nvm/versions/node/v*/bin/node \
    /data00/home/*/.nvm/versions/node/v*/bin/node; do
    [[ -x "${candidate}" ]] || continue
    candidates+=("${candidate}")
  done

  for candidate in "${candidates[@]}"; do
    [[ -n "${candidate}" ]] || continue
    if [[ -n "${seen["${candidate}"]+x}" ]]; then
      continue
    fi
    seen["${candidate}"]=1
    major="$(node_major "${candidate}" 2>/dev/null || echo 0)"
    if [[ "${major}" -ge 22 ]]; then
      echo "${candidate}"
      return 0
    fi
    found_info+="${candidate} (v${major}), "
  done

  if [[ -n "${found_info}" ]]; then
    NODE_FOUND_INFO="${found_info%, }"
  fi
  return 1
}

NODE_FOUND_INFO=""
NODE_BIN="$(find_node_22 || true)"
if [[ -n "${NODE_BIN}" ]]; then
  echo "  Node.js $("${NODE_BIN}" -v) ✓  (${NODE_BIN})"
else
  echo "  ERROR: Node.js 22+ not found in PATH or nvm."
  if [[ -n "${NODE_FOUND_INFO}" ]]; then
    echo "    Found: ${NODE_FOUND_INFO}"
  fi
  echo "    Install via nvm as the runner user or system-wide."
  exit 1
fi

# Python 3.11
PYTHON_BIN="$(find_bin python3.11 || true)"
if [[ -n "${PYTHON_BIN}" ]]; then
  echo "  Python 3.11 ✓  (${PYTHON_BIN})"
else
  echo "  ERROR: Python 3.11 not found. Please install it manually."
  exit 1
fi

# uv (Python package manager)
UV_BIN="$(find_bin uv || true)"
if [[ -n "${UV_BIN}" ]]; then
  echo "  uv ✓  (${UV_BIN})"
else
  echo "  Installing uv..."
  curl -LsSf "${CURL_PROXY[@]}" https://astral.sh/uv/install.sh | sh
  UV_BIN="$(find_bin uv || echo /usr/local/bin/uv)"
  echo "  uv installed (${UV_BIN})."
fi

# kustomize
KUSTOMIZE_BIN="$(find_bin kustomize || true)"
if [[ -n "${KUSTOMIZE_BIN}" ]]; then
  echo "  kustomize ✓  (${KUSTOMIZE_BIN})"
else
  echo "  Installing kustomize..."
  if [[ -n "${PROXY_URL}" ]]; then
    export https_proxy="${PROXY_URL}" http_proxy="${PROXY_URL}"
    export HTTPS_PROXY="${PROXY_URL}" HTTP_PROXY="${PROXY_URL}"
  fi
  curl -s "${CURL_PROXY[@]}" "https://raw.githubusercontent.com/kubernetes-sigs/kustomize/master/hack/install_kustomize.sh" | bash
  mv kustomize /usr/local/bin/
  KUSTOMIZE_BIN="/usr/local/bin/kustomize"
  echo "  kustomize installed."
fi

# git
if command -v git &>/dev/null; then
  echo "  git ✓"
else
  echo "  ERROR: git not found. Please install it manually."
  exit 1
fi

# docker
if command -v docker &>/dev/null; then
  echo "  docker ✓"
else
  echo "  ERROR: docker not found. Please install it manually."
  exit 1
fi

# --- Step 3: Configure Docker to trust Harbor ---
echo "==> [3/7] Configuring Docker to trust Harbor (${HARBOR_ADDR})..."
DOCKER_CERTS_DIR="/etc/docker/certs.d/${HARBOR_ADDR}"
mkdir -p "${DOCKER_CERTS_DIR}"

# Configure Docker daemon for insecure registry (self-signed cert)
DOCKER_DAEMON_JSON="/etc/docker/daemon.json"
if [[ -f "${DOCKER_DAEMON_JSON}" ]]; then
  # Add harbor.local to insecure-registries if not already present
  if ! grep -q "${HARBOR_ADDR}" "${DOCKER_DAEMON_JSON}"; then
    python3 -c "
import json
with open('${DOCKER_DAEMON_JSON}') as f:
    conf = json.load(f)
regs = conf.get('insecure-registries', [])
if '${HARBOR_ADDR}' not in regs:
    regs.append('${HARBOR_ADDR}')
    conf['insecure-registries'] = regs
with open('${DOCKER_DAEMON_JSON}', 'w') as f:
    json.dump(conf, f, indent=2)
"
    systemctl restart docker 2>/dev/null || true
  fi
else
  cat > "${DOCKER_DAEMON_JSON}" <<JSON
{
  "insecure-registries": ["${HARBOR_ADDR}"]
}
JSON
  systemctl restart docker 2>/dev/null || true
fi
echo "  Docker configured for ${HARBOR_ADDR}."

# --- Step 4: Copy k3s kubeconfig ---
echo "==> [4/7] Setting up kubeconfig for runner..."
KUBE_DIR="${RUNNER_HOME}/.kube"
mkdir -p "${KUBE_DIR}"

if [[ -f /etc/rancher/k3s/k3s.yaml ]]; then
  cp /etc/rancher/k3s/k3s.yaml "${KUBE_DIR}/config"
  chown -R "${RUNNER_USER}:${RUNNER_USER}" "${KUBE_DIR}"
  chmod 600 "${KUBE_DIR}/config"
  echo "  Kubeconfig copied."
else
  echo "  WARNING: /etc/rancher/k3s/k3s.yaml not found. Kubeconfig not set up."
fi

# --- Step 5: Download & extract runner ---
echo "==> [5/7] Downloading GitHub Actions runner v${RUNNER_VERSION}..."
mkdir -p "${RUNNER_DIR}"

CURL_OPTS=()
if [[ -n "${PROXY_URL}" ]]; then
  CURL_OPTS=(--proxy "${PROXY_URL}")
  # Export for sub-processes (e.g. kustomize install script)
  export https_proxy="${PROXY_URL}" http_proxy="${PROXY_URL}"
  export no_proxy="harbor.local,.cluster.local,127.0.0.1,localhost,10.0.0.0/8"
fi

if [[ ! -f "${RUNNER_DIR}/config.sh" ]]; then
  curl -fsSL "${CURL_OPTS[@]}" -o "/tmp/${RUNNER_TAR}" "${RUNNER_DOWNLOAD_URL}"
  tar xzf "/tmp/${RUNNER_TAR}" -C "${RUNNER_DIR}"
  rm -f "/tmp/${RUNNER_TAR}"
  chown -R "${RUNNER_USER}:${RUNNER_USER}" "${RUNNER_DIR}"
  echo "  Extracted to ${RUNNER_DIR}."
else
  echo "  Runner already exists at ${RUNNER_DIR}, skipping download."
fi

# --- Step 6: Configure proxy in runner .env ---
echo "==> [6/7] Configuring runner environment..."
RUNNER_ENV_FILE="${RUNNER_DIR}/.env"

# Build PATH from detected tool locations
NODE_DIR="$(dirname "${NODE_BIN}")"
UV_DIR="$(dirname "${UV_BIN}")"
KUSTOMIZE_DIR="$(dirname "${KUSTOMIZE_BIN}")"

cat > "${RUNNER_ENV_FILE}" <<ENV
# Runner environment — managed by install-gh-runner.sh
KUBECONFIG=${KUBE_DIR}/config
PATH=${NODE_DIR}:${UV_DIR}:${KUSTOMIZE_DIR}:/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin
ENV

if [[ -n "${PROXY_URL}" ]]; then
  cat >> "${RUNNER_ENV_FILE}" <<ENV
https_proxy=${PROXY_URL}
http_proxy=${PROXY_URL}
HTTPS_PROXY=${PROXY_URL}
HTTP_PROXY=${PROXY_URL}
no_proxy=harbor.local,.cluster.local,127.0.0.1,localhost,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16
NO_PROXY=harbor.local,.cluster.local,127.0.0.1,localhost,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16
ENV
  echo "  Proxy configured: ${PROXY_URL}"
else
  echo "  No proxy configured."
fi

chown "${RUNNER_USER}:${RUNNER_USER}" "${RUNNER_ENV_FILE}"

# --- Step 7: Configure & install runner as service ---
echo "==> [7/7] Configuring runner..."

# Configure (must run as the runner user)
su - "${RUNNER_USER}" -c "
  cd '${RUNNER_DIR}'
  ./config.sh \
    --url '${REPO_URL}' \
    --token '${RUNNER_TOKEN}' \
    --name '$(hostname)' \
    --labels 'self-hosted,linux,x64,infra' \
    --work '_work' \
    --unattended \
    --replace
"

# Install as systemd service
cd "${RUNNER_DIR}"
./svc.sh install "${RUNNER_USER}"
./svc.sh start

echo ""
echo "=== Installation complete ==="
echo "  Runner:  ${RUNNER_DIR}"
echo "  User:    ${RUNNER_USER}"
echo "  Service: actions.runner.*.service"
echo ""
echo "Verify:"
echo "  ./svc.sh status"
echo "  Check GitHub → Settings → Actions → Runners for 'Online' status"
