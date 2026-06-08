# Bare-Metal Release Flow

This service should be deployed as a host-local Python process, not as a container image. The release artifact contains only app code and templates. Host-local env files keep API bearer tokens, MinIO credentials, model directories, sqlite paths, venv paths, and unit paths out of git.

## One-Time Host Setup

1. Create a release root, for example a machine-local directory outside the repo.
2. Create a role-specific env file from `tagger-entry.env.example` or `tagger-backend.env.example`.
3. Create a shared venv path and set it as `UV_PROJECT_ENVIRONMENT` in the env file.
4. Install a systemd unit from the matching `.service.example`, or render one with `render-systemd.sh`.
5. If using `systemctl --user`, enable linger once with `loginctl enable-linger <user>` so the service is not stopped when SSH sessions close.
6. Install dependencies once in the release root or let `push-release.sh` run `uv sync` with `TAGGER_DEPLOY_SYNC_DEPS=1`.

For backend hosts, install exactly one ONNX runtime flavor. Use `backend-gpu` for CUDA hosts and `backend-cpu` only for CPU fallback. The GPU extra installs `onnxruntime-gpu` and cuDNN 9 Python wheels; set `LD_LIBRARY_PATH` in the backend env file to include the venv `nvidia/cudnn/lib` and `nvidia/cublas/lib` directories plus the host CUDA lib directory. If `HF_HUB_OFFLINE=1`, preseed the `deepghs/anime_rating` HuggingFace cache on the host.

Generate three different bearer tokens: one caller token for `entry`, one internal token for `entry -> backend`, and one callback token for `entry -> callback receiver`. Put the caller token in entry `TAGGER_API_TOKENS`, put the internal token in entry `TAGGER_REMOTE_AUTH_TOKEN`, put the same internal token in backend `TAGGER_API_TOKENS`, and configure the callback receiver to validate entry `TAGGER_CALLBACK_AUTH_TOKEN`.

Render a unit without editing placeholders by hand:

```bash
TAGGER_DEPLOY_ROLE=entry \
TAGGER_DEPLOY_ROOT=<REMOTE_RELEASE_ROOT> \
TAGGER_ENV_FILE=<REMOTE_ENV_FILE> \
TAGGER_UV_BIN=<ABSOLUTE_PATH_TO_UV> \
apps/tagger-service/deploy/render-systemd.sh > tagger-entry.service
```

After installing the unit and env file, run the read-only remote doctor:

```bash
TAGGER_DEPLOY_HOST=<ENTRY_HOST> \
TAGGER_DEPLOY_ROLE=entry \
TAGGER_DEPLOY_ROOT=<REMOTE_RELEASE_ROOT> \
TAGGER_DEPLOY_UNIT=tagger-entry.service \
TAGGER_REMOTE_ENV_FILE=<REMOTE_ENV_FILE> \
apps/tagger-service/deploy/doctor-host.sh
```

Put the real env file on your local machine, outside git, and let the release script sync it:

```bash
TAGGER_DEPLOY_LOCAL_ENV_FILE=<LOCAL_REAL_ENV_FILE> \
TAGGER_DEPLOY_ENV_FILE=<REMOTE_ENV_FILE> \
TAGGER_DEPLOY_HOST=<ENTRY_HOST> \
TAGGER_DEPLOY_ROLE=entry \
apps/tagger-service/deploy/push-release.sh
```

The script uploads the env file and installs it on the remote host with mode `600`. It does not print env contents.

If only env changed, sync it without packaging or restarting:

```bash
TAGGER_DEPLOY_ENV_ONLY=1 \
TAGGER_DEPLOY_LOCAL_ENV_FILE=<LOCAL_REAL_ENV_FILE> \
TAGGER_DEPLOY_ENV_FILE=<REMOTE_ENV_FILE> \
TAGGER_DEPLOY_HOST=<HOST> \
TAGGER_DEPLOY_ROLE=<entry|backend> \
apps/tagger-service/deploy/push-release.sh
```

For backend, the same deploy can sync local tagger model directories:

```bash
TAGGER_DEPLOY_WD14_MODEL_SRC=<LOCAL_WD14_MODEL_DIR> \
TAGGER_DEPLOY_WD14_MODEL_DEST=<REMOTE_WD14_MODEL_DIR> \
TAGGER_DEPLOY_EVA02_MODEL_SRC=<LOCAL_EVA02_MODEL_DIR> \
TAGGER_DEPLOY_EVA02_MODEL_DEST=<REMOTE_EVA02_MODEL_DIR> \
apps/tagger-service/deploy/push-release.sh
```

If you only want to create a placeholder env on the remote host, use:

```bash
TAGGER_DEPLOY_INIT_ENV=1 \
TAGGER_DEPLOY_ENV_FILE=<REMOTE_ENV_FILE> \
TAGGER_DEPLOY_HOST=<ENTRY_HOST> \
TAGGER_DEPLOY_ROLE=entry \
apps/tagger-service/deploy/push-release.sh
```

## Package Locally

Run the local deployment smoke first:

```bash
apps/tagger-service/deploy/check-local.sh
```

Then build the package:

```bash
apps/tagger-service/deploy/package.sh
```

The script prints the tarball path. It excludes local caches and `data/`.

## Push A Release

Preview the operation before touching a host:

```bash
TAGGER_DEPLOY_DRY_RUN=1 \
TAGGER_DEPLOY_HOST=<ENTRY_HOST> \
TAGGER_DEPLOY_ROLE=entry \
apps/tagger-service/deploy/push-release.sh
```

If you keep host-specific deploy variables in a local ignored file, load it before running the command:

```bash
set -a
source apps/tagger-service/.env.deploy.entry
set +a
TAGGER_DEPLOY_DRY_RUN=1 apps/tagger-service/deploy/push-release.sh
```

Entry:

```bash
TAGGER_DEPLOY_HOST=<ENTRY_HOST> \
TAGGER_DEPLOY_ROLE=entry \
TAGGER_DEPLOY_ROOT=<REMOTE_RELEASE_ROOT> \
TAGGER_DEPLOY_UNIT=tagger-entry.service \
TAGGER_DEPLOY_SYSTEMCTL="systemctl --user" \
TAGGER_DEPLOY_UV=<REMOTE_UV_BIN> \
TAGGER_DEPLOY_INSTALL_UNIT=1 \
apps/tagger-service/deploy/push-release.sh
```

Backend:

```bash
TAGGER_DEPLOY_HOST=<BACKEND_HOST> \
TAGGER_DEPLOY_ROLE=backend \
TAGGER_DEPLOY_ROOT=<REMOTE_RELEASE_ROOT> \
TAGGER_DEPLOY_UNIT=tagger-backend.service \
TAGGER_DEPLOY_SYSTEMCTL="systemctl --user" \
TAGGER_DEPLOY_UV=<REMOTE_UV_BIN> \
TAGGER_DEPLOY_LOCAL_UV_BIN=<LOCAL_UV_BIN_IF_REMOTE_MISSING> \
TAGGER_DEPLOY_INSTALL_UNIT=1 \
apps/tagger-service/deploy/push-release.sh
```

If the unit is a system unit, set `TAGGER_DEPLOY_SYSTEMCTL="sudo -n systemctl"` and make sure the host is configured for that explicit operation.
For user units, keep `TAGGER_SYSTEMD_TARGET=default.target`; for system units, use `TAGGER_SYSTEMD_TARGET=multi-user.target`.

## Dependency Sync

By default `push-release.sh` only uploads code, flips `current`, and restarts the unit. To sync dependencies during deploy:

```bash
TAGGER_DEPLOY_SYNC_DEPS=1 \
TAGGER_DEPLOY_VENV=<REMOTE_SHARED_VENV_DIR> \
TAGGER_DEPLOY_EXTRA=backend-gpu \
apps/tagger-service/deploy/push-release.sh
```

If the default Python index is slow from a GPU host, provide a faster mirror to `uv`:

```bash
TAGGER_DEPLOY_UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
TAGGER_DEPLOY_SYNC_DEPS=1 \
TAGGER_DEPLOY_VENV=<REMOTE_SHARED_VENV_DIR> \
TAGGER_DEPLOY_EXTRA=backend-gpu \
apps/tagger-service/deploy/push-release.sh
```

For entry, use `TAGGER_DEPLOY_EXTRA=qwen`. Keep CUDA/vLLM compatibility pinned in the host venv; do not rely on containers to hide driver mismatches.
