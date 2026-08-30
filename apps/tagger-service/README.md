# tagger-service

Bare-metal FastAPI service for the Pixiv GPU tagging pipeline. It is intentionally not wired into PaaS/K8s and does not provide a Dockerfile: CUDA, ONNX weights, and model directories stay on the GPU hosts.

## Roles

- `TAGGER_ROLE=entry`: public internal entrypoint. Accepts basename MinIO object names in `paths` plus `callback_url`, stores sqlite task state, gets Qwen3-VL describe/OCR over HTTP, calls the backend for taggers, then posts the callback.
- `TAGGER_ROLE=backend`: synchronous lightweight backend. Loads images from MinIO and runs wd14/eva02/anime_rating/phash.

## Qwen3-VL Over HTTP

The entry role does not load a vision model in-process. Qwen3-VL is served by llama-swap through its OpenAI-compatible API, so the GPU it runs on stays shared with other models instead of being pinned by this service. Entry hosts therefore need no CUDA, torch, or vLLM install at all.

Per image the stage issues three chat completions: describe group A and group B (both through the `tools` parameter, so the server's grammar constraint keeps enum values inside the schema) and one free-text OCR pass. Every request sends `chat_template_kwargs.enable_thinking=false` — Qwen3-VL is a thinking model and will otherwise spend the whole token budget on `reasoning_content` and return empty `content`.

Failures are isolated per capability: a timeout, a 5xx, a non-JSON body, an unrecognised JSON shape, a response without `tool_calls`, or an OCR pass cut short by the token budget marks only that capability with `error` plus `raw_output`, and the rest of the batch still completes. Sending the request and parsing the response sit inside the same guard, so a parser that trips over an unexpected body cannot abort the whole batch.

Describe only accepts `tool_calls`. Free-text content is never parsed as a fallback: the grammar constraint that keeps enum values inside the schema applies to the `tools` path only, and unconstrained output has been observed emitting values outside the enum (`flat_design`). OCR only accepts a string `content` that stopped normally (`finish_reason=stop`); a truncated pass keeps the partial text but is still flagged with `error`.

Entry env:

| Variable | Meaning |
| --- | --- |
| `TAGGER_QWEN_BASE_URL` | OpenAI-compatible root including the path prefix, e.g. `http://<host>:8088/v1`. It must be reachable from the entry host — a llama-swap bound to `127.0.0.1` has to run on the same host or be tunnelled. |
| `TAGGER_QWEN_MODEL` | Model id (or alias) registered in llama-swap. |
| `TAGGER_QWEN_API_KEY` | Bearer token, empty when llama-swap has no auth. |
| `TAGGER_QWEN_TIMEOUT_SECONDS` | Per-request timeout. Keep it generous: the first request after a model swap waits for llama-swap to load the model. |
| `TAGGER_QWEN_CONCURRENCY` | Images processed in parallel. Match the `-np` slot count of llama-server; more only queues server-side. |
| `TAGGER_QWEN_MAX_NEW_TOKENS` | `max_tokens` per request. |
| `TAGGER_QWEN_MAX_VISION_TOKENS` | Client-side pixel cap (1 vision token ≈ 1024 pixels). Images are downscaled before encoding so a request fits the server's per-slot context (`-c` divided by `-np`). |

`TAGGER_LOCAL_INFER_TIMEOUT_SECONDS` bounds `3 × images ÷ concurrency` HTTP round trips instead of one batched GPU pass, and blowing it calls `os._exit(1)` so systemd restarts the process. It therefore has to cover the largest batch you can submit, plus one model swap. Measured on the llama-swap host: 4.53s + 4.35s + 3.53s = 12.41s for one image's three calls serially, and 41.4s wall clock for 4 images at concurrency 2, i.e. 10.4s per image. At the `TAGGER_MAX_BATCH_PATHS=64` cap that is 64 × 10.4 ≈ 666s; a cold load of Qwen3-VL when another model holds the VRAM adds a measured 28s, giving ≈ 694s. The default is **1200s**, leaving roughly 1.7× headroom for MinIO fetches and jitter. Raise `TAGGER_MAX_BATCH_PATHS` or lower concurrency and this number has to be recomputed — `deploy/doctor-host.sh` warns when the configured value falls under the batch budget.

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
# Entry host: no GPU runtime needed, it only talks HTTP to llama-swap.
uv sync

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
