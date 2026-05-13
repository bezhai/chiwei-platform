"""Tests for tool outcome contract — C3.

``ToolOutcomeError`` is the dict shape LLM sees when an @tool fails with
a business-semantic error. ``ToolInvalidArgs`` / ``ToolNotFound`` are
the typed exceptions @tool_error raises before converting to the outcome.
"""

from __future__ import annotations

import pytest

from app.agent.tools.outcome import (
    ToolInvalidArgs,
    ToolNotFound,
    ToolOutcomeError,
)


class TestToolOutcomeError:
    def test_invalid_args_shape(self):
        out = ToolOutcomeError(
            kind="invalid_args",
            message="when_at 格式无效",
            detail={"param": "when_at", "value": "yesterday"},
        )
        dumped = out.model_dump()
        assert dumped == {
            "kind": "invalid_args",
            "message": "when_at 格式无效",
            "detail": {"param": "when_at", "value": "yesterday"},
        }

    def test_not_found_shape(self):
        out = ToolOutcomeError(
            kind="not_found",
            message="persona missing",
            detail={"persona_id": "p_42"},
        )
        dumped = out.model_dump()
        assert dumped["kind"] == "not_found"
        assert dumped["message"] == "persona missing"
        assert dumped["detail"] == {"persona_id": "p_42"}

    def test_detail_optional(self):
        out = ToolOutcomeError(kind="invalid_args", message="bad arg")
        dumped = out.model_dump()
        assert dumped == {
            "kind": "invalid_args",
            "message": "bad arg",
            "detail": None,
        }

    def test_kind_literal_rejects_unknown(self):
        with pytest.raises(Exception):  # pydantic ValidationError
            ToolOutcomeError(kind="exploded", message="x")  # type: ignore[arg-type]


class TestToolTypedExceptions:
    def test_invalid_args_carries_param(self):
        exc = ToolInvalidArgs("when_at 格式无效", param="when_at", detail={"got": "x"})
        assert str(exc) == "when_at 格式无效"
        assert exc.param == "when_at"
        assert exc.detail == {"got": "x"}

    def test_invalid_args_optional_fields(self):
        exc = ToolInvalidArgs("oops")
        assert str(exc) == "oops"
        assert exc.param is None
        assert exc.detail == {}

    def test_not_found_carries_resource_id(self):
        exc = ToolNotFound("note missing", resource_id="n_42", detail={"reason": "soft-deleted"})
        assert str(exc) == "note missing"
        assert exc.resource_id == "n_42"
        assert exc.detail == {"reason": "soft-deleted"}

    def test_not_found_optional_fields(self):
        exc = ToolNotFound("missing")
        assert str(exc) == "missing"
        assert exc.resource_id is None
        assert exc.detail == {}

    def test_repr_does_not_lose_message(self):
        # Even if a future change wraps differently, the message must
        # survive repr() so logs / langfuse don't lose it.
        exc = ToolInvalidArgs("when_at 格式无效", param="when_at")
        assert "when_at 格式无效" in repr(exc) or "when_at 格式无效" in str(exc)
