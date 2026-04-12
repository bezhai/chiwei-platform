# agent-service review findings

Date: 2026-04-12

Scope: reviewed findings that are still worth tracking. Fixed items, retracted items, and step-by-step review notes are intentionally omitted.

## P2

### `app/data`: `akao_schedule` upsert is not concurrency-safe for non-daily plans

Files:

- `apps/agent-service/app/data/models.py`
- `apps/agent-service/app/data/queries.py`

`AkaoSchedule` uses a unique constraint on:

```text
persona_id, plan_type, period_start, period_end, time_start
```

For monthly and weekly schedules, `time_start` is `NULL`. In PostgreSQL, a normal `UNIQUE` constraint allows multiple rows when one constrained column is `NULL`, so it does not reliably prevent duplicate monthly/weekly rows for the same persona and period.

`upsert_schedule()` also performs select-then-insert in application code. Concurrent cron/manual triggers can both miss the existing row and insert duplicates.

Suggested fix:

- Use DB-level `INSERT ... ON CONFLICT`.
- Make the conflict target treat missing `time_start` as a concrete value, for example an expression index on `coalesce(time_start, '')`, or store a sentinel value for non-daily plans instead of `NULL`.

### `app/agent/models.py`: `model_kwargs` protection is still blocklist-based

File:

- `apps/agent-service/app/agent/models.py`

The current `_PROTECTED_KWARGS` blocks direct overrides such as `model`, `base_url`, and `use_responses_api`, but LangChain also accepts other construction-time fields that can change the model boundary, such as `openai_proxy`, `client`, `async_client`, `root_client`, `root_async_client`, `http_client`, and `http_async_client`.

Current app call sites only pass benign kwargs such as `reasoning_effort`, so this is a boundary-hardening issue rather than an observed production bug.

Suggested fix:

- Prefer an allowlist for model behavior parameters, such as `temperature`, `reasoning_effort`, `top_p`, `max_tokens`, `metadata`, `tags`.
- If a protected/dangerous kwarg is passed, raise an explicit error instead of silently dropping it.

### `app/infra/qdrant.py`: Qdrant import still blocks service import in environments without `_sqlite3`

Files:

- `apps/agent-service/app/infra/qdrant.py`
- `apps/agent-service/app/main.py`

`app.main` imports `init_collections` from `app.infra.qdrant` at module load time. `app.infra.qdrant` imports `qdrant_client` and creates the module-level `qdrant` object immediately.

In the current local runtime, this is enough to make both imports fail:

```text
FAIL app.infra.qdrant: ModuleNotFoundError: No module named '_sqlite3'
FAIL app.main: ModuleNotFoundError: No module named '_sqlite3'
```

This may be a runtime-image issue if Qdrant is mandatory, but the code shape still makes the whole FastAPI app unimportable before any feature/config gate can run.

Suggested fix:

- If Qdrant recall/vectorization is mandatory, fix the Python runtime image so `_sqlite3` is available and add a smoke test that imports `app.main`.
- If Qdrant can degrade, move the heavy `qdrant_client` import and `_Qdrant` construction behind an explicit lazy factory/config gate, and make startup log/disable only the vector feature rather than preventing `app.main` import.

### `app/infra/qdrant.py`: Qdrant write failures can still be marked as successful vectorization

Files:

- `apps/agent-service/app/infra/qdrant.py`
- `apps/agent-service/app/workers/vectorize.py`

`upsert_vectors()` and `upsert_hybrid_vectors()` catch all exceptions and return `False`. `vectorize_message()` awaits both calls with `asyncio.gather(...)` but does not inspect the returned booleans, then returns `True`. `process_message()` maps that `True` to `vector_status = "completed"`.

So a Qdrant network/auth/schema failure can log an error but still move the DB record out of `pending`, causing the cron scanner to stop retrying an unindexed message.

Suggested fix:

- Let unexpected Qdrant write failures raise, or make `vectorize_message()` require both gather results to be `True`.
- Keep "collection already exists" handling explicit in collection creation, but do not use the same `False` path for real connectivity/configuration errors.

## P3

### `app/agent/models.py`: first DB lookup still returns the cached mutable dict

File:

- `apps/agent-service/app/agent/models.py`

Cache hits return a copied dict, but the initial DB lookup stores `result` in `_model_info_cache` and returns the same `result` object. If a caller mutates the first returned dict, it can still mutate the cached value.

Suggested fix:

- Return `dict(result)` immediately after writing the cache.
- Add a test for the DB-miss path, not only the cache-hit path.

### `app/data`: relationship memory latest ordering needs a tie-breaker

File:

- `apps/agent-service/app/data/queries.py`

`insert_relationship_memory()` computes `max(version) + 1` in application code. Concurrent writes for the same `(persona_id, user_id)` can produce duplicate versions.

The latest queries now order by `version desc`, but if two rows share the same version, the result remains nondeterministic.

Suggested fix:

- Add `id.desc()` as a tie-breaker in both latest relationship memory queries.
- Stronger option: add a unique constraint on `(persona_id, user_id, version)` and retry on conflict.

### `app/data`: empty safety result JSON is written as NULL

File:

- `apps/agent-service/app/data/queries.py`

`set_safety_status()` currently serializes `result_json` using truthiness:

```python
json.dumps(result_json) if result_json else None
```

This turns `{}` into `NULL`. The current caller passes a non-empty dict, so this is not breaking the current path, but it is still a data-loss edge case.

Suggested fix:

```python
json.dumps(result_json) if result_json is not None else None
```

### `app/agent/tools`: stale docstring for history search limit

File:

- `apps/agent-service/app/agent/tools/history.py`

The runtime default and schema clamp for `search_group_history(limit)` are now `5` with `Field(ge=1, le=10)`, but the docstring still says the default is 10.

Suggested fix:

- Update the docstring to match the current default.

### `app/infra/image.py`: missing `INNER_HTTP_SECRET` is sent as `Bearer None`

File:

- `apps/agent-service/app/infra/image.py`

`_auth_headers()` always adds:

```python
"Authorization": f"Bearer {settings.inner_http_secret}"
```

When `INNER_HTTP_SECRET` is unset, image pipeline requests carry `Authorization: Bearer None`. Other internal clients in this service only add the header when the secret exists, so this can turn a local/config-missing case into a misleading auth failure at tool-service.

Suggested fix:

- Mirror `sandbox_client.py`: build base headers first, then add `Authorization` only when `settings.inner_http_secret` is set.
- If tool-service auth is mandatory for this path, fail fast with a clear configuration error instead of sending `Bearer None`.

### `app/infra/rabbitmq.py`: `publish()` mutates the caller-provided headers dict

File:

- `apps/agent-service/app/infra/rabbitmq.py`

`publish()` uses `msg_headers = headers or {}` and then writes `msg_headers["x-delay"] = delay_ms`. If a caller reuses the same headers dict across publishes, the first delayed publish leaks `x-delay` into the caller object and can affect later messages.

No current call site appears to pass custom headers, so this is an API-footgun rather than an observed bug.

Suggested fix:

```python
msg_headers: dict[str, Any] = dict(headers) if headers else {}
```

### `app/infra/rabbitmq.py`: lane queue helper name looks like a typo

File:

- `apps/agent-service/app/infra/rabbitmq.py`

The helper is currently named `_ensurelane_queue()`, while surrounding names use readable snake_case such as `lane_queue()`, `_lane_rk()`, and `_declared_lane_queues`. This does not break runtime because the only call site uses the same name, but it makes the private API look accidental.

Suggested fix:

- Rename `_ensurelane_queue()` to `_ensure_lane_queue()`.
