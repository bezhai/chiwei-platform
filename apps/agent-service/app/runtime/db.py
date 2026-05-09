"""DB session capability — Phase 7d Gap 13.

业务永远不直接拿 session。三个对外 API：

  - ``async with tx():``  — 表达「这几行原子」
  - ``await emit_tx(data)``  — 在 tx 内追加 outbox row（强制：tx 外调用 raise）
  - ``current_session()``  — query 函数内部用；业务区禁止 import

session 走 contextvar。**AsyncSession 单 session 单 connection 不支持并发使用，
所以同一 tx 内 DB 操作只能顺序 await — 在 tx 内塞 ``asyncio.gather`` 跑多条
query 没有并发收益（最终被 SQLAlchemy 锁串成顺序），并且会触发
``InvalidRequestError``。** 想并发查就让每个分支自己进独立 tx，**前提是调用方
自身不在外层 tx 里**（ContextVar 继承会让嵌套 tx 走 SAVEPOINT 复用外层 session）。
"""
from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar

from sqlalchemy.ext.asyncio import AsyncSession

from app.data.session import get_session as _get_session_internal
from app.runtime.data import Data
from app.runtime.outbox import OutboxEmitter

logger = logging.getLogger(__name__)

_session_var: ContextVar[AsyncSession | None] = ContextVar("_session", default=None)

_TX_SLOW_THRESHOLD_S = 5.0


def current_session() -> AsyncSession:
    s = _session_var.get()
    if s is None:
        raise RuntimeError(
            "current_session() called outside tx() — wrap your DB calls in "
            "`async with tx():` or rely on a query function's auto_tx fallback"
        )
    return s


@asynccontextmanager
async def tx() -> AsyncIterator[None]:
    existing = _session_var.get()
    if existing is not None:
        async with existing.begin_nested():
            yield
        return

    started = time.monotonic()
    async with _get_session_internal() as s:
        token = _session_var.set(s)
        try:
            yield
        finally:
            _session_var.reset(token)
            elapsed = time.monotonic() - started
            if elapsed > _TX_SLOW_THRESHOLD_S:
                logger.warning(
                    "tx() held for %.2fs (threshold=%.1fs) — review for "
                    "external IO inside tx block",
                    elapsed, _TX_SLOW_THRESHOLD_S,
                )


async def emit_tx(data: Data) -> None:
    """Append an outbox row in the current tx. Raises if not in a tx.

    Why strict: outbox MUST commit atomically with business writes.
    Allowing emit_tx outside tx would let the row sneak into a one-shot
    transaction that doesn't include the caller's business writes —
    exactly the bug Gap 8 outbox closed.
    """
    s = current_session()
    emitter = OutboxEmitter(s)
    await emitter.append(data)


@asynccontextmanager
async def auto_tx() -> AsyncIterator[None]:
    """Internal helper for query functions: if not in tx, open one for
    this single call. Allows business code to call a single query
    without an explicit `with tx():` while still working inside an
    explicit tx block.
    """
    if _session_var.get() is not None:
        yield
        return
    async with tx():
        yield
