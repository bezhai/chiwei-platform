"""Tests for ``app/chat/pre_safety.py`` — thin emit_and_wait wrapper.

The heavy lifting lives in ``tests/runtime/test_emit_wait.py`` (the
generic primitive) and ``tests/wiring/test_safety_wiring.py`` (the wire).
Here we only check the chat-specific glue: PreSafetyRequest fields are
populated from the call args, the correlation id is unique per call,
and the return type is whatever ``emit_and_wait`` resolves to.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.chat.pre_safety import run_pre_safety_check
from app.domain.safety import PreSafetyVerdict


@pytest.mark.asyncio
async def test_run_pre_safety_check_builds_request_and_returns_verdict():
    captured: dict = {}

    async def fake_emit_and_wait(
        data, *, wait_for, correlation, correlation_field, timeout_s
    ):
        captured["data"] = data
        captured["wait_for"] = wait_for
        captured["correlation"] = correlation
        captured["correlation_field"] = correlation_field
        captured["timeout_s"] = timeout_s
        return PreSafetyVerdict(
            pre_request_id=correlation,
            message_id=data.message_id,
            is_blocked=False,
        )

    with patch("app.chat.pre_safety.emit_and_wait", fake_emit_and_wait):
        verdict = await run_pre_safety_check(
            message_id="m1", content="hello", persona_id="p1"
        )

    assert isinstance(verdict, PreSafetyVerdict)
    assert verdict.is_blocked is False
    req = captured["data"]
    assert req.message_id == "m1"
    assert req.message_content == "hello"
    assert req.persona_id == "p1"
    assert captured["wait_for"] is PreSafetyVerdict
    assert captured["correlation_field"] == "pre_request_id"
    assert captured["correlation"] == req.pre_request_id
    # Non-zero positive timeout; the exact value is a tuning knob and
    # we only assert the contract (>0) to avoid coupling to the number.
    assert captured["timeout_s"] > 0


@pytest.mark.asyncio
async def test_run_pre_safety_check_correlation_is_unique_per_call():
    seen: list[str] = []

    async def fake_emit_and_wait(
        data, *, wait_for, correlation, correlation_field, timeout_s
    ):
        seen.append(correlation)
        return PreSafetyVerdict(
            pre_request_id=correlation, message_id=data.message_id, is_blocked=False,
        )

    with patch("app.chat.pre_safety.emit_and_wait", fake_emit_and_wait):
        await run_pre_safety_check(message_id="m1", content="a", persona_id="p")
        await run_pre_safety_check(message_id="m1", content="a", persona_id="p")

    assert len(seen) == 2
    assert seen[0] != seen[1], "each call must allocate a fresh correlation id"
