"""Phase 7b Gap 18: per-wire manual-review queue + publish_to_review_queue."""
from __future__ import annotations

from typing import Annotated
from unittest.mock import AsyncMock, patch

import pytest

from app.runtime.data import Data, Key
from app.runtime.review_queue import (
    publish_to_review_queue,
    review_queue_name_for,
)
from app.runtime.wire import WireSpec


class _D(Data):
    id: Annotated[str, Key]


def _consumer(): pass


def test_queue_name_is_per_data_per_consumer():
    spec = WireSpec(data_type=_D, consumers=[_consumer], durable=True,
                    on_error="manual-review")
    name = review_queue_name_for(spec, _consumer)
    assert "_review" in name
    assert "_d" in name.lower()
    assert "_consumer" in name


@pytest.mark.asyncio
async def test_publish_returns_true_on_confirmed():
    spec = WireSpec(data_type=_D, consumers=[_consumer], durable=True,
                    on_error="manual-review")
    with patch("app.runtime.review_queue.mq") as mq_mock:
        mq_mock.publish_with_confirm = AsyncMock(return_value=True)
        ok = await publish_to_review_queue(
            wire=spec, consumer=_consumer, data=_D(id="x"),
            exc=RuntimeError("boom"), attempts=2, last_error="boom",
        )
        assert ok is True
        mq_mock.publish_with_confirm.assert_awaited_once()


@pytest.mark.asyncio
async def test_publish_returns_false_when_unconfirmed():
    spec = WireSpec(data_type=_D, consumers=[_consumer], durable=True,
                    on_error="manual-review")
    with patch("app.runtime.review_queue.mq") as mq_mock:
        mq_mock.publish_with_confirm = AsyncMock(return_value=False)
        ok = await publish_to_review_queue(
            wire=spec, consumer=_consumer, data=_D(id="x"),
            exc=RuntimeError("boom"), attempts=2, last_error="boom",
        )
        assert ok is False
