"""Tests for @tool_error routing — C3.

``@tool_error`` no longer stringifies every exception. Routing table:

* ``CapabilityInvalidArg``  → caught, returned as ``ToolOutcomeError(kind="invalid_args")`` dict (LLM-visible)
* ``CapabilityNotFound``    → caught, returned as ``ToolOutcomeError(kind="not_found")`` dict (LLM-visible)
* ``CapabilityTimeout``     → propagated (wire ``on_error`` decides)
* ``CapabilityRateLimited`` → propagated
* ``CapabilityCallFailed``  → propagated
* anything else             → propagated (no swallow)
* success                   → unchanged passthrough
"""

from __future__ import annotations

import pytest

from app.agent.tools._common import tool_error
from app.agent.tools.outcome import ToolOutcomeError
from app.capabilities._errors import (
    CapabilityCallFailed,
    CapabilityInvalidArg,
    CapabilityNotFound,
    CapabilityRateLimited,
    CapabilityTimeout,
)


# ---------------------------------------------------------------------------
# LLM-visible: invalid_args
# ---------------------------------------------------------------------------


class TestInvalidArgRouting:
    @pytest.mark.asyncio
    async def test_capability_invalid_arg_returns_outcome_dict(self):
        @tool_error("笔记保存失败")
        async def fn():
            raise CapabilityInvalidArg("when_at 格式无效", meta={"param": "when_at"})

        result = await fn()
        # Returned shape is a dict — that's what LangGraph turns into ToolMessage content.
        assert isinstance(result, dict)
        assert result["kind"] == "invalid_args"
        assert "when_at 格式无效" in result["message"]
        # meta passthrough into detail
        assert result["detail"] == {"param": "when_at"}

    @pytest.mark.asyncio
    async def test_outcome_validates_against_model(self):
        """The dict must round-trip through ToolOutcomeError — i.e. it really
        conforms to the contract, not just any dict."""

        @tool_error("note save failed")
        async def fn():
            raise CapabilityInvalidArg("bad")

        result = await fn()
        # Should successfully parse back into the model.
        roundtrip = ToolOutcomeError.model_validate(result)
        assert roundtrip.kind == "invalid_args"
        # Both the call-site prefix and the underlying cause survive.
        assert "note save failed" in roundtrip.message
        assert "bad" in roundtrip.message


# ---------------------------------------------------------------------------
# LLM-visible: not_found
# ---------------------------------------------------------------------------


class TestNotFoundRouting:
    @pytest.mark.asyncio
    async def test_capability_not_found_returns_outcome_dict(self):
        @tool_error("找不到了")
        async def fn():
            raise CapabilityNotFound("note missing", meta={"note_id": "n_42"})

        result = await fn()
        assert isinstance(result, dict)
        assert result["kind"] == "not_found"
        assert "note missing" in result["message"]
        assert result["detail"] == {"note_id": "n_42"}


# ---------------------------------------------------------------------------
# Propagated (NOT LLM-visible, wire on_error decides)
# ---------------------------------------------------------------------------


class TestPropagatedExceptions:
    @pytest.mark.asyncio
    async def test_capability_timeout_propagates(self):
        @tool_error("x")
        async def fn():
            raise CapabilityTimeout("upstream took too long")

        with pytest.raises(CapabilityTimeout):
            await fn()

    @pytest.mark.asyncio
    async def test_capability_rate_limited_propagates(self):
        @tool_error("x")
        async def fn():
            raise CapabilityRateLimited("429")

        with pytest.raises(CapabilityRateLimited):
            await fn()

    @pytest.mark.asyncio
    async def test_capability_call_failed_propagates(self):
        @tool_error("x")
        async def fn():
            raise CapabilityCallFailed("502")

        with pytest.raises(CapabilityCallFailed):
            await fn()

    @pytest.mark.asyncio
    async def test_runtime_error_propagates(self):
        @tool_error("x")
        async def fn():
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            await fn()

    @pytest.mark.asyncio
    async def test_value_error_propagates(self):
        @tool_error("x")
        async def fn():
            raise ValueError("nope")

        with pytest.raises(ValueError):
            await fn()


# ---------------------------------------------------------------------------
# Success passthrough
# ---------------------------------------------------------------------------


class TestSuccessPassthrough:
    @pytest.mark.asyncio
    async def test_dict_passthrough(self):
        @tool_error("x")
        async def fn():
            return {"ok": True, "id": "n_1"}

        assert await fn() == {"ok": True, "id": "n_1"}

    @pytest.mark.asyncio
    async def test_string_passthrough(self):
        @tool_error("x")
        async def fn():
            return "hello"

        assert await fn() == "hello"

    @pytest.mark.asyncio
    async def test_list_passthrough(self):
        blocks = [{"type": "text", "text": "x"}, {"type": "image_url", "image_url": {"url": "u"}}]

        @tool_error("x")
        async def fn():
            return blocks

        assert await fn() == blocks
