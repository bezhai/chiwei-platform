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
# Non-business errors: surfaced to LLM via kind="tool_error"
# ---------------------------------------------------------------------------
#
# Hotfix 2026-05-13: keeping the agent alive matters more than perfectly
# typed error routing. A bare ``Exception`` propagating out of @tool_error
# (e.g. an httpx 400 from generate_image that wasn't wrapped in a typed
# CapabilityInvalidArg) used to kill the entire agent turn. The new
# contract is "every tool failure is LLM-visible" — the model gets a
# ToolOutcomeError back and can retry / change strategy / give up
# verbally, instead of the user staring at a half-finished reply.


class TestNonBusinessFailuresSurfaceToLLM:
    @pytest.mark.asyncio
    async def test_capability_timeout_becomes_tool_error_outcome(self):
        @tool_error("generate image failed")
        async def fn():
            raise CapabilityTimeout("upstream took too long")

        out = await fn()
        assert out["kind"] == "tool_error"
        assert "generate image failed" in out["message"]
        assert "upstream took too long" in out["message"]
        assert out["detail"]["original_error_type"] == "CapabilityTimeout"

    @pytest.mark.asyncio
    async def test_capability_rate_limited_becomes_tool_error_outcome(self):
        @tool_error("x")
        async def fn():
            raise CapabilityRateLimited("429")

        out = await fn()
        assert out["kind"] == "tool_error"
        assert out["detail"]["original_error_type"] == "CapabilityRateLimited"

    @pytest.mark.asyncio
    async def test_capability_call_failed_becomes_tool_error_outcome(self):
        @tool_error("x")
        async def fn():
            raise CapabilityCallFailed("502")

        out = await fn()
        assert out["kind"] == "tool_error"
        assert out["detail"]["original_error_type"] == "CapabilityCallFailed"

    @pytest.mark.asyncio
    async def test_runtime_error_becomes_tool_error_outcome(self):
        @tool_error("x")
        async def fn():
            raise RuntimeError("boom")

        out = await fn()
        assert out["kind"] == "tool_error"
        assert "boom" in out["message"]
        assert out["detail"]["original_error_type"] == "RuntimeError"

    @pytest.mark.asyncio
    async def test_value_error_becomes_tool_error_outcome(self):
        @tool_error("x")
        async def fn():
            raise ValueError("nope")

        out = await fn()
        assert out["kind"] == "tool_error"

    @pytest.mark.asyncio
    async def test_httpx_400_unwrapped_becomes_tool_error_outcome(self):
        """Acceptance: trace 9b5a451cd00ccf735427cbb2059a95fb on
        ppe-refactor — generate_image got a 400 from Doubao for
        ``size='2K'`` (API wants ``'2k'``); the httpx error wasn't
        wrapped in a typed CapabilityInvalidArg, so the original
        @tool_error let it propagate and the whole agent turn died.
        Now the same path returns a ToolOutcomeError that the LLM can
        see and react to."""
        import httpx

        @tool_error("generate image failed")
        async def fn():
            # Mimics what the doubao client raises today.
            request = httpx.Request("POST", "https://example/v3/images")
            response = httpx.Response(400, request=request)
            raise httpx.HTTPStatusError(
                "size must be one of 'WIDTHxHEIGHT', '1k', '2k', or '4k'",
                request=request,
                response=response,
            )

        out = await fn()
        assert out["kind"] == "tool_error"
        assert "generate image failed" in out["message"]
        assert "1k" in out["message"]  # LLM can read the constraint
        assert out["detail"]["original_error_type"] == "HTTPStatusError"

    @pytest.mark.asyncio
    async def test_cancelled_error_still_propagates(self):
        """BaseException subclasses (CancelledError) must NOT be wrapped —
        they signal shutdown / cancellation and have to travel up to the
        runtime so source loops can unwind cleanly."""
        import asyncio

        @tool_error("x")
        async def fn():
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
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
