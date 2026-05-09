"""Phase 7b Gap 8: outbox pattern — atomic DB-write + emit.

Business mutation nodes use ``async with transactional_emit(session)``
INSIDE their ``async with get_session()`` block. The append writes a
``runtime_outbox`` row in the same transaction; commit makes it visible
and the dispatcher (Task 9) picks it up to fire ``emit(data)``.

Lane normalization MUST go through ``current_lane()`` (infra/rabbitmq.py)
so the dispatcher SELECT (which also uses ``current_lane()``) picks up
its own rows. Reading lane_var.get() bare from background paths (e.g.
cron, retries) returns None even when LANE env is set.
"""
from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.middleware import trace_id_var
from app.infra.rabbitmq import current_lane
from app.runtime.data import Data
from app.runtime.placement import DEFAULT_APP

RUNTIME_OUTBOX_DDL: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS runtime_outbox (
        id              BIGSERIAL PRIMARY KEY,
        data_type       TEXT NOT NULL,
        payload_json    JSONB NOT NULL,
        origin_app      TEXT NOT NULL,
        lane            TEXT,
        trace_id        TEXT,
        state           TEXT NOT NULL DEFAULT 'pending',
        attempts        INT  NOT NULL DEFAULT 0,
        last_error      TEXT,
        next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        dispatched_at   TIMESTAMPTZ
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS runtime_outbox_pending_idx
    ON runtime_outbox (state, next_attempt_at)
    WHERE state = 'pending'
    """,
    """
    CREATE INDEX IF NOT EXISTS runtime_outbox_trace_idx
    ON runtime_outbox (trace_id) WHERE trace_id IS NOT NULL
    """,
]


def _current_app() -> str:
    return os.getenv("APP_NAME") or DEFAULT_APP


class OutboxEmitter:
    """Append-only emitter bound to a caller-provided session."""

    def __init__(self, session: AsyncSession) -> None:
        if session is None:
            raise TypeError("transactional_emit requires an AsyncSession")
        self._session = session

    async def append(self, data: Data) -> None:
        cls = type(data)
        data_type = f"{cls.__module__}.{cls.__qualname__}"
        payload_json = json.dumps(data.model_dump(mode="json"))
        await self._session.execute(text(
            "INSERT INTO runtime_outbox "
            "(data_type, payload_json, origin_app, lane, trace_id) "
            "VALUES (:dt, CAST(:pj AS jsonb), :app, :lane, :tid)"
        ), {
            "dt": data_type,
            "pj": payload_json,
            "app": _current_app(),
            "lane": current_lane(),
            "tid": trace_id_var.get(),
        })


@asynccontextmanager
async def transactional_emit(session: AsyncSession) -> AsyncIterator[OutboxEmitter]:
    """Context manager that yields an OutboxEmitter bound to ``session``.

    Does NOT commit/rollback — the caller's session context owns commit
    semantics, which is the whole point of the outbox pattern.
    """
    yield OutboxEmitter(session)
