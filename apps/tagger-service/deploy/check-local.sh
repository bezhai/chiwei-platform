#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_DIR="$(cd "${APP_DIR}/../.." && pwd)"

cd "${REPO_DIR}"

bash -n apps/tagger-service/deploy/package.sh
bash -n apps/tagger-service/deploy/push-release.sh
bash -n apps/tagger-service/deploy/render-systemd.sh
bash -n apps/tagger-service/deploy/doctor-host.sh

python3 -m compileall -q apps/tagger-service/app

PYTHONPATH=apps/tagger-service python3 -m pytest \
  apps/tagger-service/tests/test_pipeline_merge.py \
  apps/tagger-service/tests/test_pipeline_orchestrate.py \
  apps/tagger-service/tests/test_pipeline_nsfw_score.py \
  apps/tagger-service/tests/test_pipeline_ocr_clean.py \
  apps/tagger-service/tests/test_pipeline_qwen_stage.py \
  apps/tagger-service/tests/test_qwen_vl_describe.py \
  apps/tagger-service/tests/test_pipeline_run_mvp.py \
  apps/tagger-service/tests/test_service_runner.py \
  apps/tagger-service/tests/test_service_api_validation.py \
  apps/tagger-service/tests/test_service_image_loader.py \
  apps/tagger-service/tests/test_service_results.py \
  apps/tagger-service/tests/test_service_auth.py \
  apps/tagger-service/tests/test_service_task_store.py \
  apps/tagger-service/tests/test_service_callbacks.py \
  -q

PACKAGE="$(TAGGER_RELEASE_VERSION=local-check apps/tagger-service/deploy/package.sh)"
PACKAGE_LIST="$(mktemp)"
trap 'rm -f "${PACKAGE_LIST}"' EXIT
tar -tzf "${PACKAGE}" >"${PACKAGE_LIST}"
grep -Fxq 'tagger-service/app/main.py' "${PACKAGE_LIST}"
grep -Fxq 'tagger-service/pyproject.toml' "${PACKAGE_LIST}"
if grep -E '(__pycache__/|\.pytest_cache/|^tagger-service/data/|^tagger-service/models/|^tagger-service/\.env|\.env$)' "${PACKAGE_LIST}" >/dev/null; then
  echo "package contains local cache or data files" >&2
  exit 1
fi

if rg -n '/data00|qwen-vl-ocr/models|10\.37\.18\.206|10\.37\.78\.98' \
  apps/tagger-service docs/specs/tagger-service.md \
  --glob '!apps/tagger-service/deploy/check-local.sh' -S; then
  echo "private host path or IP leaked into tracked tagger-service files" >&2
  exit 1
fi

rm -rf \
  apps/tagger-service/app/__pycache__ \
  apps/tagger-service/app/pipeline/__pycache__ \
  apps/tagger-service/app/service/__pycache__ \
  apps/tagger-service/tests/__pycache__ \
  apps/tagger-service/.pytest_cache

echo "tagger-service local deployment checks passed"
