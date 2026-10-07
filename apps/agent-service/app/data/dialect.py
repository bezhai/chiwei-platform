"""The asyncpg dialect the process's engine uses: SQLAlchemy's own, except that terminating a
connection is cut short after ``TERMINATE_GRACE_SECONDS``.

SQLAlchemy terminates (invalidates) a connection whenever an operation on it is cancelled or times
out, and terminating starts with a graceful close. When a statement was cut off in flight, asyncpg
has asked the server to stop it over a second connection, and the graceful close first waits, with
no limit, for the server to close that second connection. On a network path that loses the close
(seen on coe, 2026-10-06), the task doing the terminating waits forever, holding its slot in the
pool, unless something cancels it a second time. Deliveries get one from their time limits or the
broker; the interval tasks and the round-start query's own time limit had nothing that would.

``do_terminate`` is the one place every termination goes through: a connection invalidated during
a statement, a commit, a ping or a reset, and a recycled one. It runs in the task whose operation
failed or was cancelled, while that failure is on its way out. When the grace runs out, the task is
cancelled once more; SQLAlchemy then drops the connection at once and raises that cancellation. It
is taken back here, so the original exception or cancellation goes on unchanged: a statement that
timed out still raises a timeout, never a cancellation, which the callers read as "being stopped".
A cancellation someone else asks for in the meantime stands.

Registered with SQLAlchemy under :data:`DRIVERNAME`; :func:`app.data.session.make_engine` builds the
engine with it. Nothing here overrides asyncpg: ``do_terminate`` is the dialect's public hook.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy.dialects import registry
from sqlalchemy.dialects.postgresql.asyncpg import PGDialect_asyncpg

logger = logging.getLogger(__name__)

DRIVERNAME = "postgresql+asyncpg_terminate_in_grace"

# How long terminating a connection may take before it is dropped without further waiting. A
# healthy graceful close is done well inside SQLAlchemy's own 2 s limit for it, so 5 s only cuts
# the closes that are stuck. Dropping a connection that is being thrown away anyway costs nothing.
TERMINATE_GRACE_SECONDS = 5.0


class AsyncpgDialect(PGDialect_asyncpg):
    # Compiles statements exactly as its base does, so SQLAlchemy may keep caching them; a dialect
    # subclass has to say so itself.
    supports_statement_cache = True

    def do_terminate(self, dbapi_connection) -> None:
        try:
            task = asyncio.current_task()
        except RuntimeError:  # no running loop: garbage collection, where SQLAlchemy never waits
            task = None
        if task is None:
            super().do_terminate(dbapi_connection)
            return
        requested_at: int | None = None

        def cut_short() -> None:
            nonlocal requested_at
            requested_at = task.cancelling()
            task.cancel()

        timer = task.get_loop().call_later(TERMINATE_GRACE_SECONDS, cut_short)
        try:
            super().do_terminate(dbapi_connection)
        except asyncio.CancelledError:
            if requested_at is None or task.uncancel() > requested_at:
                raise
            logger.warning(
                "database: terminating a connection took over %ss; dropped it without waiting",
                TERMINATE_GRACE_SECONDS,
            )
        finally:
            timer.cancel()


registry.register(DRIVERNAME.replace("+", "."), __name__, "AsyncpgDialect")
