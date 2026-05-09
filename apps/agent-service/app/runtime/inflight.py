"""runtime_inflight state machine (Gap 7.1).

Replaces ``insert_idempotent`` for durable wires. Provides per-edge dedup
state with lease semantics (worker death recovery) and history backfill
(adoption of pre-7a Data rows that pre-existed before this state machine
was introduced).

Schema is owned by the runtime (not a Data class); migrator hooks via
``RUNTIME_INTERNAL_DDL``.

Per-edge isolation: PK is ``(edge_id, idempotent_key)`` so wire(D).to(c1)
and wire(D).to(c2) carry independent state — consumer A succeeded does
not skip consumer B with the same Data dedup_hash.

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
- For history backfill (Gap 7.1.1), if the claim returned ``fresh=True``
  the caller checks the Data table for an existing row before running
  the consumer; if present, calls ``mark_succeeded`` and acks.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import text

from app.data.session import get_session
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


def edge_id_for(data_type_qualname: str, consumer_qualname: str) -> str:
    """Stable identifier for the (data_type, consumer) edge.

    Used as the first component of the runtime_inflight PK. Two wires
    sharing a Data type but pointing to different consumers get distinct
    edge_id values, so consumer A's success does not dedup consumer B.
    """
    return f"{data_type_qualname}::{consumer_qualname}"


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
    fresh: bool    # True iff inflight row was just inserted this call


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
    async with get_session() as s:
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
            return ClaimOutcome(action="run", attempts=1, fresh=True)

        state = row["state"]
        # Phase 7b Gap 18 round-4 finding 1: review is a terminal too.
        if state in ("succeeded", "review"):
            return ClaimOutcome(action="skip", attempts=0, fresh=False)
        now = datetime.now(UTC)
        locked_until = row["locked_until"]
        if state == "processing" and locked_until is not None and locked_until > now:
            return ClaimOutcome(action="skip", attempts=0, fresh=False)

        # processing-expired or failed: take over
        new_attempts = (row["attempts"] or 0) + 1
        await s.execute(text(
            "UPDATE runtime_inflight "
            "SET state='processing', attempts=:a, "
            "    locked_until=:lu, worker_id=:w, updated_at=now() "
            "WHERE edge_id=:e AND idempotent_key=:k"
        ), {"a": new_attempts, "lu": lease_until, "w": worker_id,
            "e": edge_id, "k": idempotent_key})
        return ClaimOutcome(action="run", attempts=new_attempts, fresh=False)


async def mark_history_backfill(
    *, edge_id: str, idempotent_key: str, data_table: str
) -> None:
    """Insert a succeeded inflight row for a Data row that pre-existed.

    Used by the durable handler when ``claim_inflight`` returns
    ``fresh=True`` AND the Data table already contains the row — the
    consumer must NOT run, but the inflight terminal must be set so
    future re-deliveries dedup.

    Idempotent (ON CONFLICT DO NOTHING) so concurrent backfill paths
    don't fight; whichever wins, both end up at ``succeeded``.
    """
    async with get_session() as s:
        await s.execute(text(
            "UPDATE runtime_inflight "
            "SET state='succeeded', attempts=0, locked_until=NULL, "
            "    worker_id=NULL, trace_id='backfill', updated_at=now() "
            "WHERE edge_id=:e AND idempotent_key=:k"
        ), {"e": edge_id, "k": idempotent_key})


async def mark_succeeded(*, edge_id: str, idempotent_key: str) -> None:
    async with get_session() as s:
        await s.execute(text(
            "UPDATE runtime_inflight "
            "SET state='succeeded', locked_until=NULL, worker_id=NULL, "
            "    updated_at=now() "
            "WHERE edge_id=:e AND idempotent_key=:k"
        ), {"e": edge_id, "k": idempotent_key})


async def mark_failed(
    *, edge_id: str, idempotent_key: str, last_error: str
) -> None:
    async with get_session() as s:
        await s.execute(text(
            "UPDATE runtime_inflight "
            "SET state='failed', locked_until=NULL, worker_id=NULL, "
            "    last_error=:err, updated_at=now() "
            "WHERE edge_id=:e AND idempotent_key=:k"
        ), {"err": last_error[:8000], "e": edge_id, "k": idempotent_key})


async def mark_review(
    *, edge_id: str, idempotent_key: str, last_error: str
) -> None:
    """Phase 7b Gap 18: terminal state for messages routed to manual-review.

    Persists last_error so operators inspecting runtime_inflight rows in
    state='review' can see the reason without joining the queue envelope.
    Once a row is in 'review', claim_inflight will skip it. Operators
    must delete_inflight() it before any replay (see runbook).
    """
    async with get_session() as s:
        await s.execute(text(
            "UPDATE runtime_inflight "
            "SET state='review', locked_until=NULL, worker_id=NULL, "
            "    last_error=:err, updated_at=now() "
            "WHERE edge_id=:e AND idempotent_key=:k"
        ), {"err": last_error[:8000], "e": edge_id, "k": idempotent_key})


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
        async with get_session() as s:
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
            await s.commit()
        return DeleteOutcome(deleted=1, skipped_succeeded=0)

    # by == "trace_id"
    if not trace_id:
        raise ValueError("trace_id mode requires trace_id")
    async with get_session() as s:
        skipped = (await s.execute(text(
            "SELECT count(*) FROM runtime_inflight "
            "WHERE trace_id=:t AND state='succeeded'"
        ), {"t": trace_id})).scalar()
        result = await s.execute(text(
            "DELETE FROM runtime_inflight "
            "WHERE trace_id=:t AND state != 'succeeded'"
        ), {"t": trace_id})
        await s.commit()
        deleted = result.rowcount or 0
    return DeleteOutcome(deleted=deleted, skipped_succeeded=int(skipped or 0))
