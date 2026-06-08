#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  TAGGER_DEPLOY_HOST=<host> TAGGER_DEPLOY_ROLE=<entry|backend> ./deploy/push-release.sh [package.tar.gz]

Required environment:
  TAGGER_DEPLOY_HOST       SSH host, optionally user@host
  TAGGER_DEPLOY_ROLE       entry or backend

Optional environment:
  TAGGER_DEPLOY_ROOT       Remote release root. Default: ~/tagger-service
  TAGGER_DEPLOY_UNIT       systemd unit name. Default: tagger-${TAGGER_DEPLOY_ROLE}.service
  TAGGER_DEPLOY_SYSTEMCTL  Remote systemctl command. Default: systemctl --user
  TAGGER_DEPLOY_SSH_OPTS   Extra ssh/scp options.
  TAGGER_DEPLOY_SYNC_DEPS  1 to run uv sync before restart. Default: 0
  TAGGER_DEPLOY_DRY_RUN    1 to print the planned release and exit. Default: 0
  TAGGER_DEPLOY_ENV_ONLY   1 to only upload TAGGER_DEPLOY_LOCAL_ENV_FILE. Default: 0
  TAGGER_DEPLOY_UV         Remote uv binary. Default: uv
  TAGGER_DEPLOY_UV_INDEX_URL Python package index URL passed to remote uv sync.
  TAGGER_DEPLOY_UV_RUN_ARGS Extra arguments inserted after `uv run` in systemd ExecStart.
  TAGGER_DEPLOY_APP_COMMAND Command executed by `uv run`. Default: uvicorn app.main:app --host ${TAGGER_HOST} --port ${TAGGER_PORT}
  TAGGER_DEPLOY_LOCAL_UV_BIN Local uv binary to upload to TAGGER_DEPLOY_UV.
  TAGGER_DEPLOY_EXTRA      uv extra when syncing deps. Default: qwen for entry, backend-gpu for backend
  TAGGER_DEPLOY_VENV       UV_PROJECT_ENVIRONMENT when syncing deps.
  TAGGER_DEPLOY_INSTALL_UNIT 1 to install and enable the systemd unit. Default: 0
  TAGGER_DEPLOY_UNIT_DIR   Remote systemd unit dir. Default: user dir when SYSTEMCTL contains --user.
  TAGGER_SYSTEMD_TARGET    systemd install target. Default: default.target
  TAGGER_DEPLOY_LOCAL_ENV_FILE  Local env file to upload to the remote host.
  TAGGER_DEPLOY_ENV_FILE   Remote env file to upload/init.
  TAGGER_DEPLOY_INIT_ENV   1 to create TAGGER_DEPLOY_ENV_FILE from template if it does not exist. Default: 0
  TAGGER_DEPLOY_WD14_MODEL_SRC   Local wd14 model directory to sync.
  TAGGER_DEPLOY_WD14_MODEL_DEST  Remote wd14 model directory.
  TAGGER_DEPLOY_EVA02_MODEL_SRC  Local eva02 model directory to sync.
  TAGGER_DEPLOY_EVA02_MODEL_DEST Remote eva02 model directory.
USAGE
}

HOST="${TAGGER_DEPLOY_HOST:-}"
ROLE="${TAGGER_DEPLOY_ROLE:-}"
if [[ -z "${HOST}" || -z "${ROLE}" ]]; then
  usage >&2
  exit 2
fi
if [[ "${ROLE}" != "entry" && "${ROLE}" != "backend" ]]; then
  printf 'TAGGER_DEPLOY_ROLE must be entry or backend, got %s\n' "${ROLE}" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE="${1:-}"

ROOT="${TAGGER_DEPLOY_ROOT:-~/tagger-service}"
UNIT="${TAGGER_DEPLOY_UNIT:-tagger-${ROLE}.service}"
SYSTEMCTL="${TAGGER_DEPLOY_SYSTEMCTL:-systemctl --user}"
SSH_OPTS="${TAGGER_DEPLOY_SSH_OPTS:-}"
SYNC_DEPS="${TAGGER_DEPLOY_SYNC_DEPS:-0}"
DRY_RUN="${TAGGER_DEPLOY_DRY_RUN:-0}"
ENV_ONLY="${TAGGER_DEPLOY_ENV_ONLY:-0}"
UV_BIN="${TAGGER_DEPLOY_UV:-uv}"
UV_INDEX_URL="${TAGGER_DEPLOY_UV_INDEX_URL:-${UV_INDEX_URL:-}}"
UV_RUN_ARGS="${TAGGER_DEPLOY_UV_RUN_ARGS:-}"
APP_COMMAND="${TAGGER_DEPLOY_APP_COMMAND:-}"
if [[ -z "${APP_COMMAND}" ]]; then
  APP_COMMAND='uvicorn app.main:app --host ${TAGGER_HOST} --port ${TAGGER_PORT}'
fi
LOCAL_UV_BIN="${TAGGER_DEPLOY_LOCAL_UV_BIN:-}"
EXTRA="${TAGGER_DEPLOY_EXTRA:-}"
if [[ -z "${EXTRA}" ]]; then
  if [[ "${ROLE}" == "entry" ]]; then
    EXTRA="qwen"
  else
    EXTRA="backend-gpu"
  fi
fi
REMOTE_VENV="${TAGGER_DEPLOY_VENV:-}"
INSTALL_UNIT="${TAGGER_DEPLOY_INSTALL_UNIT:-0}"
UNIT_DIR="${TAGGER_DEPLOY_UNIT_DIR:-}"
SYSTEMD_TARGET="${TAGGER_SYSTEMD_TARGET:-default.target}"
LOCAL_ENV_FILE="${TAGGER_DEPLOY_LOCAL_ENV_FILE:-}"
REMOTE_ENV_FILE="${TAGGER_DEPLOY_ENV_FILE:-}"
INIT_ENV="${TAGGER_DEPLOY_INIT_ENV:-0}"
WD14_MODEL_SRC="${TAGGER_DEPLOY_WD14_MODEL_SRC:-}"
WD14_MODEL_DEST="${TAGGER_DEPLOY_WD14_MODEL_DEST:-}"
EVA02_MODEL_SRC="${TAGGER_DEPLOY_EVA02_MODEL_SRC:-}"
EVA02_MODEL_DEST="${TAGGER_DEPLOY_EVA02_MODEL_DEST:-}"
if [[ -n "${LOCAL_ENV_FILE}" && ! -f "${LOCAL_ENV_FILE}" ]]; then
  printf 'local env file not found: %s\n' "${LOCAL_ENV_FILE}" >&2
  exit 2
fi
if [[ -n "${LOCAL_ENV_FILE}" && -z "${REMOTE_ENV_FILE}" ]]; then
  printf 'TAGGER_DEPLOY_ENV_FILE is required when TAGGER_DEPLOY_LOCAL_ENV_FILE is set\n' >&2
  exit 2
fi
if [[ "${ENV_ONLY}" == "1" && -z "${LOCAL_ENV_FILE}" ]]; then
  printf 'TAGGER_DEPLOY_LOCAL_ENV_FILE is required when TAGGER_DEPLOY_ENV_ONLY=1\n' >&2
  exit 2
fi
if [[ -n "${LOCAL_UV_BIN}" && ! -f "${LOCAL_UV_BIN}" ]]; then
  printf 'local uv binary not found: %s\n' "${LOCAL_UV_BIN}" >&2
  exit 2
fi
if [[ -n "${LOCAL_UV_BIN}" && "${UV_BIN}" != /* && "${UV_BIN}" != "~/"* ]]; then
  printf 'TAGGER_DEPLOY_UV must be an absolute remote path when TAGGER_DEPLOY_LOCAL_UV_BIN is set\n' >&2
  exit 2
fi
if [[ "${INSTALL_UNIT}" == "1" && -z "${REMOTE_ENV_FILE}" ]]; then
  printf 'TAGGER_DEPLOY_ENV_FILE is required when TAGGER_DEPLOY_INSTALL_UNIT=1\n' >&2
  exit 2
fi
if [[ -z "${UNIT_DIR}" ]]; then
  if [[ "${SYSTEMCTL}" == *"--user"* ]]; then
    UNIT_DIR="~/.config/systemd/user"
  else
    UNIT_DIR="/etc/systemd/system"
  fi
fi
for pair in \
  "TAGGER_DEPLOY_WD14_MODEL_SRC:${WD14_MODEL_SRC}:${WD14_MODEL_DEST}" \
  "TAGGER_DEPLOY_EVA02_MODEL_SRC:${EVA02_MODEL_SRC}:${EVA02_MODEL_DEST}"; do
  IFS=: read -r label src dest <<<"${pair}"
  if [[ -n "${src}" && ! -d "${src}" ]]; then
    printf '%s directory not found: %s\n' "${label}" "${src}" >&2
    exit 2
  fi
  if [[ -n "${src}" && -z "${dest}" ]]; then
    printf '%s destination is required when source is set\n' "${label/_SRC/_DEST}" >&2
    exit 2
  fi
done

if [[ "${ENV_ONLY}" != "1" ]]; then
  if [[ -z "${PACKAGE}" ]]; then
    PACKAGE="$("${SCRIPT_DIR}/package.sh")"
  fi
  if [[ ! -f "${PACKAGE}" ]]; then
    printf 'package not found: %s\n' "${PACKAGE}" >&2
    exit 2
  fi
  PKG_BASE="$(basename "${PACKAGE}")"
  RELEASE_NAME="${PKG_BASE%.tar.gz}"
  REMOTE_PACKAGE="${ROOT}/packages/${PKG_BASE}"
  REMOTE_RELEASE="${ROOT}/releases/${RELEASE_NAME}"
else
  PACKAGE="<env-only>"
  REMOTE_PACKAGE="<env-only>"
  REMOTE_RELEASE="<env-only>"
fi

if [[ "${DRY_RUN}" == "1" ]]; then
  cat <<EOF
tagger-service deploy dry-run
  host:           ${HOST}
  role:           ${ROLE}
  package:        ${PACKAGE}
  remote package: ${REMOTE_PACKAGE}
  remote release: ${REMOTE_RELEASE}
  current link:   ${ROOT}/current
  systemctl:      ${SYSTEMCTL}
  unit:           ${UNIT}
  env only:       ${ENV_ONLY}
  install unit:   ${INSTALL_UNIT}
  unit dir:       ${UNIT_DIR}
  sync deps:      ${SYNC_DEPS}
  uv extra:       ${EXTRA}
  uv binary:      ${UV_BIN}
  uv index url:   ${UV_INDEX_URL:-<not set>}
  uv run args:    ${UV_RUN_ARGS:-<not set>}
  app command:    ${APP_COMMAND}
  local uv:       ${LOCAL_UV_BIN:-<not set>}
  init env:       ${INIT_ENV}
  local env:      ${LOCAL_ENV_FILE:-<not set>}
  env file:       ${REMOTE_ENV_FILE:-<not set>}
  wd14 model:     ${WD14_MODEL_SRC:-<not set>} -> ${WD14_MODEL_DEST:-<not set>}
  eva02 model:    ${EVA02_MODEL_SRC:-<not set>} -> ${EVA02_MODEL_DEST:-<not set>}
EOF
  exit 0
fi

upload_env_file() {
  if [[ -z "${LOCAL_ENV_FILE}" ]]; then
    return
  fi
  local remote_tmp="${ROOT}/packages/.$(basename "${REMOTE_ENV_FILE}").tmp"
  scp ${SSH_OPTS} "${LOCAL_ENV_FILE}" "${HOST}:${remote_tmp}"
  ssh ${SSH_OPTS} "${HOST}" "mkdir -p \$(dirname ${REMOTE_ENV_FILE}) && install -m 600 ${remote_tmp} ${REMOTE_ENV_FILE} && rm -f ${remote_tmp}"
}

upload_uv_binary() {
  if [[ -z "${LOCAL_UV_BIN}" ]]; then
    return
  fi
  local remote_tmp="${ROOT}/packages/.$(basename "${UV_BIN}").tmp"
  scp ${SSH_OPTS} "${LOCAL_UV_BIN}" "${HOST}:${remote_tmp}"
  ssh ${SSH_OPTS} "${HOST}" "mkdir -p \$(dirname ${UV_BIN}) && install -m 755 ${remote_tmp} ${UV_BIN} && rm -f ${remote_tmp}"
}

install_systemd_unit() {
  if [[ "${INSTALL_UNIT}" != "1" ]]; then
    return
  fi
  local remote_tmp="${ROOT}/packages/.${UNIT}.tmp"
  ssh ${SSH_OPTS} "${HOST}" "cat > ${remote_tmp}" <<EOF
[Unit]
Description=Pixiv tagger ${ROLE} service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${ROOT}/current
EnvironmentFile=${REMOTE_ENV_FILE}
ExecStart=${UV_BIN} run ${UV_RUN_ARGS} --no-sync ${APP_COMMAND}
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=60

[Install]
WantedBy=${SYSTEMD_TARGET}
EOF
  ssh ${SSH_OPTS} "${HOST}" "mkdir -p ${UNIT_DIR} && install -m 644 ${remote_tmp} ${UNIT_DIR}/${UNIT} && rm -f ${remote_tmp} && ${SYSTEMCTL} daemon-reload && ${SYSTEMCTL} enable ${UNIT}"
}

if [[ "${ENV_ONLY}" == "1" ]]; then
  ssh ${SSH_OPTS} "${HOST}" "mkdir -p ${ROOT}/packages"
  upload_env_file
  exit 0
fi

ssh ${SSH_OPTS} "${HOST}" "mkdir -p ${ROOT}/packages ${ROOT}/releases"
scp ${SSH_OPTS} "${PACKAGE}" "${HOST}:${REMOTE_PACKAGE}"
ssh ${SSH_OPTS} "${HOST}" "rm -rf ${REMOTE_RELEASE} && mkdir -p ${REMOTE_RELEASE} && tar -xzf ${REMOTE_PACKAGE} -C ${REMOTE_RELEASE} --strip-components=1"

upload_env_file

sync_model_dir() {
  local src="$1"
  local dest="$2"
  if [[ -z "${src}" ]]; then
    return
  fi
  ssh ${SSH_OPTS} "${HOST}" "mkdir -p ${dest}"
  rsync -a --delete -e "ssh ${SSH_OPTS}" "${src}/" "${HOST}:${dest}/"
}

sync_model_dir "${WD14_MODEL_SRC}" "${WD14_MODEL_DEST}"
sync_model_dir "${EVA02_MODEL_SRC}" "${EVA02_MODEL_DEST}"
upload_uv_binary

if [[ "${INIT_ENV}" == "1" ]]; then
  if [[ -z "${REMOTE_ENV_FILE}" ]]; then
    printf 'TAGGER_DEPLOY_ENV_FILE is required when TAGGER_DEPLOY_INIT_ENV=1\n' >&2
    exit 2
  fi
  ssh ${SSH_OPTS} "${HOST}" "if [ ! -f ${REMOTE_ENV_FILE} ]; then mkdir -p \$(dirname ${REMOTE_ENV_FILE}) && cp ${REMOTE_RELEASE}/deploy/tagger-${ROLE}.env.example ${REMOTE_ENV_FILE}; fi"
fi

if [[ "${SYNC_DEPS}" == "1" ]]; then
  UV_SYNC_ENV=""
  if [[ -n "${UV_INDEX_URL}" ]]; then
    UV_SYNC_ENV="UV_INDEX_URL=${UV_INDEX_URL}"
  fi
  if [[ -n "${REMOTE_VENV}" ]]; then
    ssh ${SSH_OPTS} "${HOST}" "cd ${REMOTE_RELEASE} && UV_PROJECT_ENVIRONMENT=${REMOTE_VENV} ${UV_SYNC_ENV} ${UV_BIN} sync --extra ${EXTRA}"
  else
    ssh ${SSH_OPTS} "${HOST}" "cd ${REMOTE_RELEASE} && ${UV_SYNC_ENV} ${UV_BIN} sync --extra ${EXTRA}"
  fi
fi

ssh ${SSH_OPTS} "${HOST}" "ln -sfn ${REMOTE_RELEASE} ${ROOT}/current"
install_systemd_unit
ssh ${SSH_OPTS} "${HOST}" "${SYSTEMCTL} restart ${UNIT} && ${SYSTEMCTL} status ${UNIT} --no-pager -l"
