# DLQ Replay Runbook

Phase 7b Gap 12 — operator playbook for `runtime_inflight` + RabbitMQ DLQ replay.

## Which dead letters these targets read

The `make dlq-*` targets call agent-service's `/admin/dlq/*` routes, which read the RabbitMQ
queue named in `QUEUE`. Today that is the shared `dead_letters` queue: the outbound queues
agent-service publishes to (`chat_response_<channel>`, `recall_<channel>`, consumed by
lark-outbound and chat-response-worker) dead-letter through the exchange `post_processing_dlx`
into it when their consumer rejects a message for good.

Messages between participants (messaging's inboxes) do not land there. Each lane's inboxes
dead-letter into that lane's own `isolated_dead_letters_<lane>`, which messaging's operator routes
inspect and replay (`/admin/messaging/dead-letters` and `/admin/messaging/dead-letters/replay`),
not these targets.

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

## Routine checks (oncall checklist)

- Any DLQ over a per-queue threshold? Indicates an unfixed bug in production — open an incident.
- Any `runtime_dlq_audit` rows with `status NOT IN ('requeued', 'zombie_acked')` older than 24h? Stale `cleared` / `publish_failed` — needs operator intervention.

```sql
SELECT id, queue, action, status, recovery_hint, created_at
  FROM runtime_dlq_audit
 WHERE status NOT IN ('requeued', 'zombie_acked')
   AND created_at < now() - interval '24 hours'
 ORDER BY created_at;
```
