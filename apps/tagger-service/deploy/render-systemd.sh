#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  TAGGER_DEPLOY_ROLE=<entry|backend> \
  TAGGER_DEPLOY_ROOT=<remote_release_root> \
  TAGGER_ENV_FILE=<machine_local_env_file> \
  TAGGER_UV_BIN=<uv_binary> \
  apps/tagger-service/deploy/render-systemd.sh > tagger-entry.service

Optional:
  TAGGER_UNIT_DESCRIPTION   systemd Description value
  TAGGER_SYSTEMD_TARGET     install target. Default: default.target
  TAGGER_UV_RUN_ARGS        extra arguments inserted after `uv run`
  TAGGER_DEPLOY_APP_COMMAND command executed by `uv run`
USAGE
}

ROLE="${TAGGER_DEPLOY_ROLE:-}"
ROOT="${TAGGER_DEPLOY_ROOT:-}"
ENV_FILE="${TAGGER_ENV_FILE:-}"
UV_BIN="${TAGGER_UV_BIN:-}"
UV_RUN_ARGS="${TAGGER_UV_RUN_ARGS:-}"
APP_COMMAND="${TAGGER_DEPLOY_APP_COMMAND:-}"
if [[ -z "${APP_COMMAND}" ]]; then
  APP_COMMAND='uvicorn app.main:app --host ${TAGGER_HOST} --port ${TAGGER_PORT}'
fi
if [[ -z "${ROLE}" || -z "${ROOT}" || -z "${ENV_FILE}" || -z "${UV_BIN}" ]]; then
  usage >&2
  exit 2
fi
if [[ "${ROLE}" != "entry" && "${ROLE}" != "backend" ]]; then
  printf 'TAGGER_DEPLOY_ROLE must be entry or backend, got %s\n' "${ROLE}" >&2
  exit 2
fi

DESCRIPTION="${TAGGER_UNIT_DESCRIPTION:-Pixiv tagger ${ROLE} service}"
TARGET="${TAGGER_SYSTEMD_TARGET:-default.target}"

cat <<EOF
[Unit]
Description=${DESCRIPTION}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${ROOT}/current
EnvironmentFile=${ENV_FILE}
ExecStart=${UV_BIN} run ${UV_RUN_ARGS} --no-sync ${APP_COMMAND}
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=60

[Install]
WantedBy=${TARGET}
EOF
