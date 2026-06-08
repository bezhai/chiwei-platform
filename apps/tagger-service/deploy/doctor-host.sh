#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  TAGGER_DEPLOY_HOST=<host> TAGGER_DEPLOY_ROLE=<entry|backend> ./deploy/doctor-host.sh

Required environment:
  TAGGER_DEPLOY_HOST       SSH host, optionally user@host
  TAGGER_DEPLOY_ROLE       entry or backend

Optional environment:
  TAGGER_DEPLOY_ROOT       Remote release root. Default: ~/tagger-service
  TAGGER_DEPLOY_UNIT       systemd unit name. Default: tagger-${TAGGER_DEPLOY_ROLE}.service
  TAGGER_DEPLOY_SYSTEMCTL  Remote systemctl command. Default: systemctl --user
  TAGGER_DEPLOY_SSH_OPTS   Extra ssh options.
  TAGGER_REMOTE_ENV_FILE   Role env file on the remote host. If omitted, env checks are skipped.
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

ROOT="${TAGGER_DEPLOY_ROOT:-~/tagger-service}"
UNIT="${TAGGER_DEPLOY_UNIT:-tagger-${ROLE}.service}"
SYSTEMCTL="${TAGGER_DEPLOY_SYSTEMCTL:-systemctl --user}"
SSH_OPTS="${TAGGER_DEPLOY_SSH_OPTS:-}"
ENV_FILE="${TAGGER_REMOTE_ENV_FILE:-}"

ssh ${SSH_OPTS} "${HOST}" \
  "TAGGER_DOCTOR_ROLE='${ROLE}' TAGGER_DOCTOR_ROOT='${ROOT}' TAGGER_DOCTOR_UNIT='${UNIT}' TAGGER_DOCTOR_SYSTEMCTL='${SYSTEMCTL}' TAGGER_DOCTOR_ENV_FILE='${ENV_FILE}' bash -s" <<'REMOTE'
set -euo pipefail

ROLE="${TAGGER_DOCTOR_ROLE}"
ROOT="${TAGGER_DOCTOR_ROOT}"
UNIT="${TAGGER_DOCTOR_UNIT}"
SYSTEMCTL="${TAGGER_DOCTOR_SYSTEMCTL}"
ENV_FILE="${TAGGER_DOCTOR_ENV_FILE}"

ok() {
  printf 'ok: %s\n' "$1"
}

warn() {
  printf 'warn: %s\n' "$1"
}

fail() {
  printf 'fail: %s\n' "$1"
  exit 1
}

command -v uv >/dev/null 2>&1 && ok "uv is available" || warn "uv is not on PATH; unit may use an absolute uv path"
command -v python3 >/dev/null 2>&1 && ok "python3 is available" || warn "python3 is not on PATH"
command -v nvidia-smi >/dev/null 2>&1 && ok "nvidia-smi is available" || warn "nvidia-smi is not available"

if [[ "${SYSTEMCTL}" == *"--user"* ]]; then
  USER_NAME="$(id -un)"
  LINGER="$(loginctl show-user "${USER_NAME}" -p Linger 2>/dev/null | sed 's/^Linger=//')"
  if [[ "${LINGER}" == "yes" ]]; then
    ok "systemd user linger is enabled"
  else
    fail "systemd user linger is disabled; run: loginctl enable-linger ${USER_NAME}"
  fi
fi

test -d "${ROOT}" && ok "release root exists" || fail "release root is missing"
test -L "${ROOT}/current" && ok "current symlink exists" || warn "current symlink is missing"
test -f "${ROOT}/current/pyproject.toml" && ok "current release has pyproject.toml" || warn "current release is not installed yet"
test -f "${ROOT}/current/app/main.py" && ok "current release has app/main.py" || warn "current release app files are missing"

if ${SYSTEMCTL} status "${UNIT}" --no-pager -l >/tmp/tagger-doctor-systemctl.out 2>&1; then
  ok "systemd unit status command succeeded"
else
  warn "systemd unit status command failed"
  sed -n '1,40p' /tmp/tagger-doctor-systemctl.out
fi

if [[ -z "${ENV_FILE}" ]]; then
  warn "TAGGER_REMOTE_ENV_FILE not set; skipping env checks"
  exit 0
fi
test -f "${ENV_FILE}" && ok "env file exists" || fail "env file is missing"

python3 - "${ENV_FILE}" "${ROLE}" <<'PY'
from __future__ import annotations

import os
import sys
from pathlib import Path

env_file = Path(sys.argv[1])
role = sys.argv[2]

values: dict[str, str] = {}
for raw in env_file.read_text("utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    values[key.strip()] = value.strip().strip("\"'")

required = [
    "TAGGER_ROLE",
    "TAGGER_HOST",
    "TAGGER_PORT",
    "UV_PROJECT_ENVIRONMENT",
    "TAGGER_API_TOKENS",
    "MINIO_ENDPOINT",
    "MINIO_PORT",
    "MINIO_ACCESS_KEY",
    "MINIO_SECRET_KEY",
    "MINIO_BUCKET",
]
if role == "entry":
    required += [
        "TAGGER_SQLITE_PATH",
        "TAGGER_QWEN_MODEL_PATH",
        "TAGGER_REMOTE_URL",
        "TAGGER_REMOTE_AUTH_TOKEN",
        "TAGGER_CALLBACK_AUTH_TOKEN",
    ]
else:
    required += ["TAGGER_WD14_MODEL_DIR", "TAGGER_EVA02_MODEL_DIR"]

missing = [key for key in required if not values.get(key) or values[key].startswith("<")]
if missing:
    print("fail: missing env values: " + ", ".join(missing))
    sys.exit(1)

if values.get("TAGGER_ROLE") != role:
    print(f"fail: TAGGER_ROLE does not match deploy role ({role})")
    sys.exit(1)

path_checks = ["UV_PROJECT_ENVIRONMENT"]
if role == "entry":
    path_checks += ["TAGGER_QWEN_MODEL_PATH"]
    sqlite_parent = Path(values["TAGGER_SQLITE_PATH"]).expanduser().parent
    if not sqlite_parent.exists():
        print("fail: TAGGER_SQLITE_PATH parent does not exist")
        sys.exit(1)
else:
    path_checks += ["TAGGER_WD14_MODEL_DIR", "TAGGER_EVA02_MODEL_DIR"]

for key in path_checks:
    path = Path(values[key]).expanduser()
    if not path.exists():
        print(f"fail: {key} path does not exist")
        sys.exit(1)

if role == "backend":
    cache_dir = Path.home() / ".cache/huggingface/hub/models--deepghs--anime_rating"
    if values.get("HF_HUB_OFFLINE") == "1" and not cache_dir.exists():
        print("fail: HF_HUB_OFFLINE=1 but deepghs/anime_rating cache is missing")
        sys.exit(1)

print("ok: env file has required values")
PY
REMOTE
