"""runtime_inflight state machine (Gap 7.1): the per-message claims messaging dedupes on.

Per-edge dedup state with lease semantics (worker death recovery). An edge is
one inbox in one lane (``inbox:<name>@<lane>``, :mod:`app.messaging.receiving`);
the key is the message id.

Schema is owned by the runtime (not a Data class); the host passes
:data:`RUNTIME_INFLIGHT_DDL` to the migrator.

Per-edge isolation: PK is ``(edge_id, idempotent_key)``, so the same key on
two edges carries independent state — succeeded on one does not skip the
other.

Lease semantics:

- ``processing`` rows carry ``locked_until`` (the deadline by which the
  current worker must finish) plus ``worker_id`` (host:pid).
- A new claim for the same key with ``locked_until > now()`` is skipped:
  the live worker is still running.
- After ``locked_until <= now()`` (worker death, OOM, network loss,
  pod restart), the next claim takes over, increments ``attempts``,
  and runs the consumer fresh.
- ``succeeded`` is the only dedup terminal — both ``failed`` and
  ``processing-expired`` resume.

Caller protocol:

- ``claim_inflight(...)`` opens a short transaction. The advisory lock is
  released on commit; the consumer runs OUTSIDE that transaction.
- After the consumer call, ``mark_succeeded(...)`` or ``mark_failed(...)``
  closes the row in a separate short transaction.
"""

from __future__ import annotations

import hashlib
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import text

from app.data.tx import auto_tx, current_session
from app.runtime.errors import AlreadySucceededError

RUNTIME_INFLIGHT_DDL: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS runtime_inflight (
        edge_id        TEXT NOT NULL,
        idempotent_key TEXT NOT NULL,
        data_table     TEXT NOT NULL,
        state          TEXT NOT NULL,
        attempts       INT  NOT NULL DEFAULT 0,
        last_error     TEXT,
        locked_until   TIMESTAMPTZ,
        worker_id      TEXT,
        trace_id       TEXT,
        created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (edge_id, idempotent_key)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS runtime_inflight_state_idx
    ON runtime_inflight (state, locked_until)
    """,
]


def _lock_key(edge_id: str, idempotent_key: str) -> int:
    """Map (edge_id, idempotent_key) into pg_advisory_xact_lock's int4 space.

    MD5 truncated to 60 bits, modulo 2**31 — collisions across unrelated
    keys are benign (they only serialize more than strictly necessary,
    never miss a lock).
    """
    h = hashlib.md5(f"{edge_id}::{idempotent_key}".encode()).hexdigest()[:15]
    return int(h, 16) % (2**31)


@dataclass(frozen=True)
class ClaimOutcome:
    action: Literal["run", "skip"]
    attempts: int  # 0 if action == 'skip'
    # Set only when skipping because another worker holds a live lease: the
    # moment that lease runs out. A skip with ``None`` means the key has
    # succeeded and never needs another look.
    locked_until: datetime | None = None


@dataclass(frozen=True)
class DeleteOutcome:
    deleted: int
    skipped_succeeded: int


async def claim_inflight(
    *,
    edge_id: str,
    idempotent_key: str,
    data_table: str,
    worker_id: str,
    lease_ms: int,
    trace_id: str | None = None,
) -> ClaimOutcome:
    """Claim runnable state for (edge_id, idempotent_key); return outcome.

    Short transaction: pg_advisory_xact_lock + SELECT/INSERT/UPDATE.
    Caller MUST run consumer OUTSIDE this transaction (lock is released
    on commit) and call mark_succeeded / mark_failed afterwards.
    """
    lock = _lock_key(edge_id, idempotent_key)
    lease_until = datetime.now(UTC) + timedelta(milliseconds=lease_ms)
    async with auto_tx():
        s = current_session()
        await s.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": lock})
        r = await s.execute(text(
            "SELECT state, attempts, locked_until "
            "FROM runtime_inflight WHERE edge_id=:e AND idempotent_key=:k"
        ), {"e": edge_id, "k": idempotent_key})
        row = r.mappings().first()

        if row is None:
            await s.execute(text(
                "INSERT INTO runtime_inflight "
                "(edge_id, idempotent_key, data_table, state, attempts, "
                " locked_until, worker_id, trace_id) "
                "VALUES (:e, :k, :t, 'processing', 1, :lu, :w, :tid)"
            ), {"e": edge_id, "k": idempotent_key, "t": data_table,
                "lu": lease_until, "w": worker_id, "tid": trace_id})
            return ClaimOutcome(action="run", attempts=1)

        state = row["state"]
        if state == "succeeded":
            return ClaimOutcome(action="skip", attempts=0)
        now = datetime.now(UTC)
        locked_until = row["locked_until"]
        if state == "processing" and locked_until is not None and locked_until > now:
            return ClaimOutcome(action="skip", attempts=0, locked_until=locked_until)

        # processing-expired or failed: take over
        new_attempts = (row["attempts"] or 0) + 1
        await s.execute(text(
            "UPDATE runtime_inflight "
            "SET state='processing', attempts=:a, "
            "    locked_until=:lu, worker_id=:w, updated_at=now() "
            "WHERE edge_id=:e AND idempotent_key=:k"
        ), {"a": new_attempts, "lu": lease_until, "w": worker_id,
            "e": edge_id, "k": idempotent_key})
        return ClaimOutcome(action="run", attempts=new_attempts)


async def succeeded_keys(*, edge_id: str, idempotent_keys: Collection[str]) -> set[str]:
    """``idempotent_keys`` 里在 ``edge_id`` 上已经记成 ``succeeded`` 的那些。

    ``succeeded`` 是最终的：:func:`claim_inflight` 对它一律跳过，:func:`delete_inflight` 也不删它，
    所以这里交回的 key，之后再来的同一条都不会再交给处理方。
    """
    if not idempotent_keys:
        return set()
    async with auto_tx():
        rows = await current_session().execute(text(
            "SELECT idempotent_key FROM runtime_inflight "
            "WHERE edge_id=:e AND idempotent_key = ANY(:ks) AND state='succeeded'"
        ), {"e": edge_id, "ks": list(idempotent_keys)})
    return {row[0] for row in rows}


# ``worker_id`` guard for the two marks below: with it, the update only lands
# while the row is still ``processing`` under that exact claim. A worker whose
# lease ran out and was taken over then finds its mark refused (returns False)
# instead of overwriting the new holder's outcome. Callers that don't pass it
# keep the unconditional update.
_HELD_BY = " AND state='processing' AND worker_id=:w"


async def mark_succeeded(
    *, edge_id: str, idempotent_key: str, worker_id: str | None = None
) -> bool:
    params = {"e": edge_id, "k": idempotent_key, "w": worker_id}
    async with auto_tx():
        result = await current_session().execute(text(
            "UPDATE runtime_inflight "
            "SET state='succeeded', locked_until=NULL, worker_id=NULL, "
            "    updated_at=now() "
            "WHERE edge_id=:e AND idempotent_key=:k"
            + (_HELD_BY if worker_id is not None else "")
        ), params)
    return bool(getattr(result, "rowcount", 0))


async def mark_failed(
    *,
    edge_id: str,
    idempotent_key: str,
    last_error: str,
    worker_id: str | None = None,
) -> bool:
    params = {
        "err": last_error[:8000], "e": edge_id, "k": idempotent_key, "w": worker_id,
    }
    async with auto_tx():
        result = await current_session().execute(text(
            "UPDATE runtime_inflight "
            "SET state='failed', locked_until=NULL, worker_id=NULL, "
            "    last_error=:err, updated_at=now() "
            "WHERE edge_id=:e AND idempotent_key=:k"
            + (_HELD_BY if worker_id is not None else "")
        ), params)
    return bool(getattr(result, "rowcount", 0))


async def delete_inflight(
    *,
    by: Literal["edge_idempotent", "trace_id"],
    trace_id: str | None = None,
    edge_id: str | None = None,
    idempotent_key: str | None = None,
) -> DeleteOutcome:
    """Phase 7b Gap 12: clear inflight rows for DLQ replay.

    Modes:
      - edge_idempotent: target a single (edge_id, idempotent_key) row.
        Refuses to delete a 'succeeded' row — raises AlreadySucceededError
        so the DLQ requeue protocol can route to the zombie-ack path.
      - trace_id: delete every non-succeeded row for the trace; preserves
        succeeded rows (mixed-state traces are normal). Returns counts;
        never raises AlreadySucceededError.
    """
    if by not in ("edge_idempotent", "trace_id"):
        raise ValueError(
            f"by must be one of ('edge_idempotent', 'trace_id'), got {by!r}"
        )

    if by == "edge_idempotent":
        if not edge_id or not idempotent_key:
            raise ValueError(
                "edge_idempotent mode requires both edge_id and idempotent_key"
            )
        async with auto_tx():
            s = current_session()
            row = (await s.execute(text(
                "SELECT state FROM runtime_inflight "
                "WHERE edge_id=:e AND idempotent_key=:k"
            ), {"e": edge_id, "k": idempotent_key})).mappings().first()
            if row is None:
                return DeleteOutcome(deleted=0, skipped_succeeded=0)
            if row["state"] == "succeeded":
                raise AlreadySucceededError(
                    edge_id=edge_id, idempotent_key=idempotent_key,
                )
            await s.execute(text(
                "DELETE FROM runtime_inflight "
                "WHERE edge_id=:e AND idempotent_key=:k AND state != 'succeeded'"
            ), {"e": edge_id, "k": idempotent_key})
        return DeleteOutcome(deleted=1, skipped_succeeded=0)

    # by == "trace_id"
    if not trace_id:
        raise ValueError("trace_id mode requires trace_id")
    async with auto_tx():
        s = current_session()
        skipped = (await s.execute(text(
            "SELECT count(*) FROM runtime_inflight "
            "WHERE trace_id=:t AND state='succeeded'"
        ), {"t": trace_id})).scalar()
        result = await s.execute(text(
            "DELETE FROM runtime_inflight "
            "WHERE trace_id=:t AND state != 'succeeded'"
        ), {"t": trace_id})
        deleted = result.rowcount or 0
    return DeleteOutcome(deleted=deleted, skipped_succeeded=int(skipped or 0))
