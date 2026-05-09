# DLQ Replay Runbook

Phase 7b Gap 12 — operator playbook for `runtime_inflight` + RabbitMQ DLQ replay.

## When does a message land in DLQ?

A durable consumer raised an exception that:

- Was not classified as `DuplicateData` with `on_error="ignore-duplicate"`
- Was not classified as `NeedsReview` with `on_error="manual-review"`
- Exhausted the wire's `.retry(...)` budget (or the wire had no retry policy)

The broker routed the original message via DLX → DLQ. Default DLQ name is `durable_<data>_<consumer>-dlx` (matches `_route_for(...)` naming).

## Decision tree

1. **Inspect**: `make dlq-inspect QUEUE=<name>`
   Look at the topmost messages — `data_type`, `last_error`, `attempts`, `trace_id`.

2. **Diagnose root cause** (out of band — Loki logs / source code / infra dashboards).

3. **Decide action**:
   - **Bug fixed, replay safe** → goto step 4 (replay).
   - **Bug not yet fixed** → leave the DLQ alone. Messages are evidence; do NOT delete them.
   - **Replay would create duplicate side effect (consumer not idempotent)** → DO NOT use `CLEAR=true`. Use `make dlq-replay QUEUE=... CLEAR=false LIMIT=1` per individual message after verifying consumer state out of band.

4. **Dry-run first** (recommended for `LIMIT > 1`):
   `make dlq-dry-run QUEUE=<name>`
   Reports what would be cleared and where it would be re-published. No state changes.

5. **Replay**: `make dlq-replay QUEUE=<name> CLEAR=true LIMIT=10`
   - `CLEAR=true` clears `runtime_inflight` rows for the messages being replayed; without it the consumer's idempotent dedup will silently skip the redelivery (the historical "replay no-op" bug).
   - `LIMIT=N` caps the batch.
   - An audit row lands in `runtime_dlq_audit` per replayed message.
   - `X-Operator` header is auto-populated from `git config user.name`.

## Failure recovery

### `publish_failed > 0`

- Original DLQ messages have been NACKed back (still in DLQ — ready for another attempt once the broker is healthy).
- `runtime_inflight` rows for those messages have been cleared (idempotent for retry).
- `runtime_dlq_audit` rows show `status='publish_failed'` with `recovery_hint`.

Inspect:

```sql
SELECT id, queue, recovery_hint, created_at
  FROM runtime_dlq_audit
 WHERE status = 'publish_failed'
 ORDER BY created_at DESC
 LIMIT 20;
```

### `zombie_acked > 0`

- These were "second-replay zombies": the consumer had already succeeded between the original failure and this replay attempt. The 6-step protocol detected `state='succeeded'` on the inflight row and acked the DLQ message silently.
- `runtime_dlq_audit` row has `status='zombie_acked'`. No action needed — consumer side already at terminal `succeeded`.

## Manual-review queues

Same `make dlq-*` commands work; pass `KIND=review`. Example:

```
make dlq-inspect QUEUE=durable_<data>_<consumer>_review KIND=review
```

To **dispose** of a review message (operator decides "ignore"):

```
make dlq-replay QUEUE=<review queue> KIND=review CLEAR=false LIMIT=1
```

The message is acked but NOT re-published (`CLEAR=false` leaves the `runtime_inflight` row in `state='review'`; `claim_inflight` will skip it on any redelivery).

To **re-process** a review message after fixing the underlying issue:

1. `make dlq-replay QUEUE=<review queue> KIND=review CLEAR=true` — clears the `state='review'` row from `runtime_inflight`.
2. Manually re-publish the original message body to the **original durable queue** (NOT the review queue) via a one-off `mq.publish_with_confirm` call from a Python REPL bound to the agent-service runtime — credentials per ConfigBundle.

## Routine checks (oncall checklist)

- Any non-empty review queues? Run `make dlq-inspect KIND=review` per known review queue name.
- Any DLQ over a per-queue threshold? Indicates an unfixed bug in production — open an incident.
- Any `runtime_dlq_audit` rows with `status NOT IN ('requeued', 'zombie_acked')` older than 24h? Stale `cleared` / `publish_failed` — needs operator intervention.

```sql
SELECT id, queue, action, status, recovery_hint, created_at
  FROM runtime_dlq_audit
 WHERE status NOT IN ('requeued', 'zombie_acked')
   AND created_at < now() - interval '24 hours'
 ORDER BY created_at;
```
