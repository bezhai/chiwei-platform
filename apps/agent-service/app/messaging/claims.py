"""The ``runtime_inflight`` table: the per-message claims messaging dedupes on.

Per-edge dedup state with lease semantics (worker death recovery). An edge is
one inbox in one lane (``inbox:<name>@<lane>``, :mod:`app.messaging.receiving`);
the key is the message id.

The table is not a Data class; the host passes :data:`RUNTIME_INFLIGHT_DDL` to
the migrator. It keeps the name it had when the dataflow runtime owned it.

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
- ``succeeded`` is the only dedup terminal — ``failed`` (a failure with a
  retry still to come, or a cancelled delivery that let go) and
  ``processing-expired`` resume.
- ``dead_lettered``: the handler failed with no attempt left and the message
  went to the dead letters. Every claim skips it, until the dead-letter
  replay turns it back into ``failed``
  (:func:`reopen_dead_lettered`) just before it publishes the message again.
  The holder writes it straight over its ``processing`` claim
  (:func:`mark_dead_lettered`), so the row is never ``failed`` in between for
  another copy to take over. A copy that finds it is not handled; the
  messaging layer sends it to the dead letters too.

Caller protocol:

- ``claim_inflight(...)`` opens a short transaction. The advisory lock is
  released on commit; the consumer runs OUTSIDE that transaction.
- After the consumer call, ``mark_succeeded(...)``, ``mark_failed(...)`` or
  ``mark_dead_lettered(...)`` closes the row in a separate short transaction.
"""

from __future__ import annotations

import hashlib
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import text

from app.data.tx import auto_tx, current_session

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
    # succeeded, or was dead-lettered, and this copy never needs another look.
    locked_until: datetime | None = None
    # A skip because the key was dead-lettered (and not replayed since): the
    # copy is not handled, and the caller dead-letters it too.
    dead_lettered: bool = False


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
        if state == "dead_lettered":
            return ClaimOutcome(action="skip", attempts=0, dead_lettered=True)
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

    ``succeeded`` 是最终的：:func:`claim_inflight` 对它一律跳过，
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
    """The handler failed with a retry still to come, or the delivery was
    cancelled and lets go of its claim: the next claim takes over."""
    return await _end_with_error(
        "failed", edge_id=edge_id, idempotent_key=idempotent_key,
        last_error=last_error, worker_id=worker_id,
    )


async def mark_dead_lettered(
    *, edge_id: str, idempotent_key: str, last_error: str, worker_id: str
) -> bool:
    """The handler failed with no attempt left and the message goes to the
    dead letters: every claim skips the key until :func:`reopen_dead_lettered`.
    One statement over the holder's own ``processing`` claim."""
    return await _end_with_error(
        "dead_lettered", edge_id=edge_id, idempotent_key=idempotent_key,
        last_error=last_error, worker_id=worker_id,
    )


async def _end_with_error(
    state: Literal["failed", "dead_lettered"],
    *,
    edge_id: str,
    idempotent_key: str,
    last_error: str,
    worker_id: str | None,
) -> bool:
    params = {
        "s": state, "err": last_error[:8000], "e": edge_id, "k": idempotent_key,
        "w": worker_id,
    }
    async with auto_tx():
        result = await current_session().execute(text(
            "UPDATE runtime_inflight "
            "SET state=:s, locked_until=NULL, worker_id=NULL, "
            "    last_error=:err, updated_at=now() "
            "WHERE edge_id=:e AND idempotent_key=:k"
            + (_HELD_BY if worker_id is not None else "")
        ), params)
    return bool(getattr(result, "rowcount", 0))


async def reopen_dead_lettered(*, edge_id: str, idempotent_key: str) -> bool:
    """The dead-letter replay is about to publish this message again: a
    ``dead_lettered`` key becomes ``failed``, so the replayed copy takes it
    over (``attempts`` and ``last_error`` stay). Any other state, or no row,
    is left alone; returns whether a row changed."""
    async with auto_tx():
        result = await current_session().execute(text(
            "UPDATE runtime_inflight SET state='failed', updated_at=now() "
            "WHERE edge_id=:e AND idempotent_key=:k AND state='dead_lettered'"
        ), {"e": edge_id, "k": idempotent_key})
    return bool(getattr(result, "rowcount", 0))
