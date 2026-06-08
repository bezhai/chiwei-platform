# tagger-service

Bare-metal FastAPI service for the Pixiv GPU tagging pipeline. It is intentionally not wired into PaaS/K8s and does not provide a Dockerfile: CUDA, vLLM, ONNX weights, and model directories stay on the GPU hosts.

## Roles

- `TAGGER_ROLE=entry`: public internal entrypoint. Accepts basename MinIO object names in `paths` plus `callback_url`, stores sqlite task state, runs local Qwen describe/OCR, calls the backend for taggers, then posts the callback.
- `TAGGER_ROLE=backend`: synchronous lightweight backend. Loads images from MinIO and runs wd14/eva02/anime_rating/phash.

All business endpoints require `Authorization: Bearer <token>`. `/health` stays unauthenticated for process checks. The entry role uses `TAGGER_API_TOKENS` for callers and `TAGGER_REMOTE_AUTH_TOKEN` when calling the backend; the backend role uses `TAGGER_API_TOKENS` for the entry-to-backend token. Callback POSTs from entry include `Authorization: Bearer <TAGGER_CALLBACK_AUTH_TOKEN>`, which should be validated by the callback receiver.

## Bare-Metal Setup

1. Create or reuse a Python 3.11 venv on each GPU host.
2. Install app dependencies from this directory. On the backend host, install exactly one ONNX runtime variant for the machine (`backend-gpu` for CUDA, `backend-cpu` for CPU fallback).
3. Fill a machine-local env file from `deploy/tagger-entry.env.example` or `deploy/tagger-backend.env.example`.
4. Install a systemd unit from the matching `deploy/*.service.example`, replacing placeholder paths locally.
5. For `systemctl --user` units, enable linger once with `loginctl enable-linger <user>` so services keep running after SSH exits.
6. Start through systemd so restarts and stop signals are supervised by the original process manager.

Do not commit real model paths, MinIO credentials, or host-specific systemd paths. Use env files outside the repository for those values.

See `deploy/DEPLOYMENT.md` for the tarball + rsync + systemd release flow.
That flow can also sync local ignored env files to a remote host with `TAGGER_DEPLOY_ENV_ONLY=1`.

Before pushing to a host:

```bash
apps/tagger-service/deploy/check-local.sh
```

For first-time host setup, use `deploy/render-systemd.sh` to generate the unit and `deploy/doctor-host.sh` to verify the host without printing secrets.

Example install shapes:

```bash
# Entry host, using the existing CUDA/vLLM-compatible environment.
uv sync --extra qwen

# Backend host with CUDA ONNX runtime.
uv sync --extra backend-gpu
```

## Commands

Backend:

```bash
uv run --no-sync uvicorn app.main:app --host "$TAGGER_HOST" --port "$TAGGER_PORT"
```

Entry:

```bash
uv run --no-sync uvicorn app.main:app --host "$TAGGER_HOST" --port "$TAGGER_PORT"
```

Health:

```bash
curl -sf "http://<host>:<port>/health"
```

Submit to entry:

```bash
curl -sf -X POST "http://<entry-host>:<port>/api/v1/tagger/submit" \
  -H "Authorization: Bearer <CALLER_TOKEN>" \
  -H 'Content-Type: application/json' \
  -d '{"paths":["5486389_p0.jpg"],"callback_url":"http://<callback-host>/callback"}'
```

Call backend directly:

```bash
curl -sf -X POST "http://<backend-host>:<port>/api/v1/tagger/infer" \
  -H "Authorization: Bearer <BACKEND_TOKEN>" \
  -H 'Content-Type: application/json' \
  -d '{"paths":["5486389_p0.jpg"]}'
```
