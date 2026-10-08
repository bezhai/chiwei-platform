"""A durable consumer that raises: the inflight row is marked failed and the message rejected."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Annotated
from unittest.mock import AsyncMock, patch

import pytest

from app.runtime.data import Data, Key
from app.runtime.durable import _build_handler
from app.runtime.inflight import ClaimOutcome
from app.runtime.node import node
from app.runtime.wire import WireSpec


class _Job(Data):
    jid: Annotated[str, Key]


_ORIGINAL = RuntimeError("boom")


@node
async def _failing(j: _Job) -> None:
    raise _ORIGINAL


class _Message:
    """Just enough of an aio-pika message: a body, headers, and ``process`` recording how the
    handler left it (``ack`` on a clean exit, ``reject`` with the exception otherwise)."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.headers: dict = {}
        self.settled: tuple | None = None

    @asynccontextmanager
    async def process(self, *, requeue: bool):
        try:
            yield
        except BaseException as exc:
            self.settled = ("reject", requeue, exc)
            raise
        self.settled = ("ack",)


@pytest.mark.asyncio
async def test_consumer_error_marks_failed_and_rejects_with_the_original_exception():
    handler = _build_handler(WireSpec(data_type=_Job, durable=True), _failing)
    message = _Message(b'{"jid": "j1"}')
    claimed = ClaimOutcome(action="run", attempts=1, fresh=True)

    with patch("app.runtime.durable.claim_inflight", new=AsyncMock(return_value=claimed)), \
         patch("app.runtime.durable.insert_idempotent", new=AsyncMock(return_value=1)), \
         patch("app.runtime.durable.mark_failed", new=AsyncMock()) as mf, \
         patch("app.runtime.durable.mark_succeeded", new=AsyncMock()) as ms:
        with pytest.raises(RuntimeError) as raised:
            await handler(message)

    assert raised.value is _ORIGINAL
    assert message.settled == ("reject", False, _ORIGINAL)
    mf.assert_awaited_once()
    assert mf.await_args.kwargs["last_error"] == "boom"
    ms.assert_not_awaited()
