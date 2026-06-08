#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_DIR="$(cd "${APP_DIR}/../.." && pwd)"
VERSION="${TAGGER_RELEASE_VERSION:-}"
if [[ -z "${VERSION}" ]]; then
  VERSION="$(git -C "${REPO_DIR}" rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M%S)"
fi

OUT="${TAGGER_PACKAGE_OUT:-/tmp/tagger-service-${VERSION}.tar.gz}"
TMP_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT

mkdir -p "${TMP_DIR}/tagger-service"
rsync -a \
  --exclude '.env*' \
  --exclude '*.env' \
  --exclude '.venv/' \
  --exclude '.pytest_cache/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude 'data/' \
  --exclude 'models/' \
  "${APP_DIR}/" "${TMP_DIR}/tagger-service/"

tar -C "${TMP_DIR}" -czf "${OUT}" tagger-service
printf '%s\n' "${OUT}"
