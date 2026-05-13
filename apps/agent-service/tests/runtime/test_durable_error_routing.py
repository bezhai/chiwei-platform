"""Phase 7b Gap 18: durable handler routes consumer exceptions per wire.on_error."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.runtime.errors import DuplicateData, NeedsReview


@dataclass
class _FakeWire:
    on_error: str = "dlq"
    retry: Any = None


# We'll import the helper after it exists; keep a thin wrapper.
async def _call_helper(*, exc, wire, data=None, **kw):
    from app.runtime.durable import _route_consumer_exception
    async def _fake_consumer(): pass
    if data is None:
        data = MagicMock()
        data.model_dump.return_value = {}
    return await _route_consumer_exception(
        exc, wire=wire, consumer=_fake_consumer,
        inflight_key=("edge", "key"),
        data=data, attempts=kw.get("attempts", 1),
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
         patch("app.runtime.durable._route_for", return_value=("queue", "key")), \
         patch("app.runtime.durable.publish_with_confirm", new=AsyncMock(return_value=True)):
        dr.return_value = type("D", (), {"action": "retry", "attempt": 2, "delay_ms": 100})()
        # retry envelope publish; helper returns (ack)
        await _call_helper(exc=RuntimeError("boom"), wire=wire)


@pytest.mark.asyncio
async def test_generic_retry_publish_unconfirmed_falls_through_to_dlq():
    wire = _FakeWire(on_error="dlq")
    with patch("app.runtime.durable.mark_failed", new=AsyncMock()), \
         patch("app.runtime.durable.decide_retry") as dr, \
         patch("app.runtime.durable._route_for", return_value=("queue", "key")), \
         patch("app.runtime.durable.publish_with_confirm", new=AsyncMock(return_value=False)):
        dr.return_value = type("D", (), {"action": "retry", "attempt": 2, "delay_ms": 100})()
        original = RuntimeError("boom")
        with pytest.raises(RuntimeError) as ei:
            await _call_helper(exc=original, wire=wire)
        assert ei.value is original


# -- B4: swallow_and_log policy ------------------------------------------------
# Contract §4.2: swallow_and_log is the 4th on_error policy. Consumer raised a
# generic Exception, we log + mark_succeeded + return (caller's `async with
# message.process()` will ack). Typed exceptions (DuplicateData / NeedsReview)
# matching their own policy still take precedence — swallow_and_log only covers
# the generic-Exception last-resort bucket. Mismatched typed exceptions fall
# into the generic bucket and get swallowed (consistent with current "typed
# exception in mismatched policy ⇒ generic path" rule).


@pytest.mark.asyncio
async def test_swallow_and_log_marks_succeeded_and_returns():
    """Generic RuntimeError + swallow_and_log: ack silently, do not retry/dlq."""
    wire = _FakeWire(on_error="swallow_and_log")
    with patch("app.runtime.durable.mark_succeeded", new=AsyncMock()) as ms, \
         patch("app.runtime.durable.mark_failed", new=AsyncMock()) as mf, \
         patch("app.runtime.durable.decide_retry") as dr, \
         patch("app.runtime.durable.publish_with_confirm", new=AsyncMock()) as pub:
        # If the swallow branch runs, decide_retry / publish_with_confirm must
        # never be touched.
        await _call_helper(exc=RuntimeError("boom"), wire=wire)
        ms.assert_awaited_once()
        mf.assert_not_awaited()
        dr.assert_not_called()
        pub.assert_not_awaited()


@pytest.mark.asyncio
async def test_swallow_and_log_does_not_raise():
    """Original exception must not propagate — caller will ack."""
    wire = _FakeWire(on_error="swallow_and_log")
    with patch("app.runtime.durable.mark_succeeded", new=AsyncMock()):
        # Should return cleanly, no raise.
        await _call_helper(exc=ValueError("ugly"), wire=wire)


@pytest.mark.asyncio
async def test_swallow_and_log_does_not_override_duplicate_data():
    """Contract: typed DuplicateData still routes via ignore-duplicate semantics
    when wire.on_error matches. swallow_and_log is the generic Exception bucket
    only — but here the wire is swallow_and_log, NOT ignore-duplicate, so the
    DuplicateData falls into the generic bucket and gets swallowed (per the
    'typed exception in mismatched policy ⇒ generic path' rule)."""
    wire = _FakeWire(on_error="swallow_and_log")
    with patch("app.runtime.durable.mark_succeeded", new=AsyncMock()) as ms, \
         patch("app.runtime.durable.mark_failed", new=AsyncMock()) as mf:
        await _call_helper(exc=DuplicateData("dup"), wire=wire)
        ms.assert_awaited_once()
        mf.assert_not_awaited()


@pytest.mark.asyncio
async def test_swallow_and_log_does_not_override_matching_typed_exception():
    """ignore-duplicate wire still routes DuplicateData via mark_succeeded —
    sanity that swallow_and_log being implemented didn't break the existing
    typed-policy match. (Mirror of the existing duplicate-with-ignore test
    but kept here for B4 regression coverage.)"""
    wire = _FakeWire(on_error="ignore-duplicate")
    with patch("app.runtime.durable.mark_succeeded", new=AsyncMock()) as ms, \
         patch("app.runtime.durable.mark_failed", new=AsyncMock()) as mf:
        await _call_helper(exc=DuplicateData("dup"), wire=wire)
        ms.assert_awaited_once()
        mf.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_review_still_takes_precedence_over_swallow_when_matching():
    """Sanity: manual-review wire + NeedsReview still publishes to review queue.
    swallow_and_log is a separate policy — wires can't have two policies, so
    this is just verifying we didn't accidentally make swallow_and_log a
    'global swallow' that overrides everything."""
    wire = _FakeWire(on_error="manual-review")
    with patch("app.runtime.durable.publish_to_review_queue",
               new=AsyncMock(return_value=True)) as pub, \
         patch("app.runtime.durable.mark_review", new=AsyncMock()) as mr, \
         patch("app.runtime.durable.mark_succeeded", new=AsyncMock()) as ms:
        await _call_helper(exc=NeedsReview("needs op"), wire=wire)
        pub.assert_awaited_once()
        mr.assert_awaited_once()
        ms.assert_not_awaited()
