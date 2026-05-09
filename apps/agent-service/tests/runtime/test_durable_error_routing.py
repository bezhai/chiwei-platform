"""Phase 7b Gap 18: durable handler routes consumer exceptions per wire.on_error."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.runtime.errors import DuplicateData, NeedsReview


@dataclass
class _FakeWire:
    on_error: str = "dlq"
    retry: Any = None


# We'll import the helper after it exists; keep a thin wrapper.
async def _call_helper(*, exc, wire, **kw):
    from app.runtime.durable import _route_consumer_exception
    async def _fake_consumer(): pass
    return await _route_consumer_exception(
        exc, wire=wire, consumer=_fake_consumer,
        inflight_key=("edge", "key"),
        data=object(), attempts=kw.get("attempts", 1),
        headers={},
    )


@pytest.mark.asyncio
async def test_duplicate_data_with_ignore_policy_marks_succeeded_and_returns():
    wire = _FakeWire(on_error="ignore-duplicate")
    with patch("app.runtime.durable.mark_succeeded", new=AsyncMock()) as ms, \
         patch("app.runtime.durable.mark_failed", new=AsyncMock()) as mf:
        # return (no raise) means caller will ack
        await _call_helper(exc=DuplicateData("dup"), wire=wire)
        ms.assert_awaited_once()
        mf.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_data_with_dlq_policy_falls_through_and_raises():
    wire = _FakeWire(on_error="dlq")
    with patch("app.runtime.durable.mark_succeeded", new=AsyncMock()), \
         patch("app.runtime.durable.mark_failed", new=AsyncMock()) as mf, \
         patch("app.runtime.durable.decide_retry") as dr:
        dr.return_value = type("D", (), {"action": "dlq"})()
        with pytest.raises(DuplicateData):
            await _call_helper(exc=DuplicateData("dup"), wire=wire)
        mf.assert_awaited_once()


@pytest.mark.asyncio
async def test_needs_review_with_manual_review_publishes_and_marks_review():
    wire = _FakeWire(on_error="manual-review")
    with patch("app.runtime.durable.publish_to_review_queue", new=AsyncMock(return_value=True)) as pub, \
         patch("app.runtime.durable.mark_review", new=AsyncMock()) as mr:
        await _call_helper(exc=NeedsReview("needs op"), wire=wire)
        pub.assert_awaited_once()
        mr.assert_awaited_once()


@pytest.mark.asyncio
async def test_needs_review_publish_confirm_failed_falls_through_to_dlq():
    wire = _FakeWire(on_error="manual-review")
    with patch("app.runtime.durable.publish_to_review_queue", new=AsyncMock(return_value=False)), \
         patch("app.runtime.durable.mark_failed", new=AsyncMock()) as mf, \
         patch("app.runtime.durable.mark_review", new=AsyncMock()) as mr:
        with pytest.raises(NeedsReview):
            await _call_helper(exc=NeedsReview("needs op"), wire=wire)
        mf.assert_awaited_once()
        mr.assert_not_awaited()


@pytest.mark.asyncio
async def test_generic_retry_publish_confirmed_acks_silently():
    wire = _FakeWire(on_error="dlq")
    with patch("app.runtime.durable.mark_failed", new=AsyncMock()), \
         patch("app.runtime.durable.decide_retry") as dr, \
         patch("app.runtime.durable.publish_with_confirm", new=AsyncMock(return_value=True)):
        dr.return_value = type("D", (), {"action": "retry", "attempt": 2, "delay_ms": 100})()
        # retry envelope publish; helper returns (ack)
        await _call_helper(exc=RuntimeError("boom"), wire=wire)


@pytest.mark.asyncio
async def test_generic_retry_publish_unconfirmed_falls_through_to_dlq():
    wire = _FakeWire(on_error="dlq")
    with patch("app.runtime.durable.mark_failed", new=AsyncMock()), \
         patch("app.runtime.durable.decide_retry") as dr, \
         patch("app.runtime.durable.publish_with_confirm", new=AsyncMock(return_value=False)):
        dr.return_value = type("D", (), {"action": "retry", "attempt": 2, "delay_ms": 100})()
        original = RuntimeError("boom")
        with pytest.raises(RuntimeError) as ei:
            await _call_helper(exc=original, wire=wire)
        assert ei.value is original
