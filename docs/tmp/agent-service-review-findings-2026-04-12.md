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

