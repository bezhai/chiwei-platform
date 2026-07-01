"""Tests for the ``no_reply`` tool's ``reason`` contract.

``reason`` is a *required* free-text parameter so every no_reply call leaves
an observability trail. Two behaviours matter here:

  - the reflected JSON schema must mark ``reason`` required, so the model
    actually sees it as mandatory;
  - ``dispatch()``'s pre-invoke binding check (``_check_binding``) must keep
    tolerating a call that omits ``reason`` — it should come back as a
    ``ToolResult`` carrying a binding-error outcome, never raise. The turn
    still terminates because ``_is_terminal_tool_call`` only matches on the
    tool *name*, independent of whether dispatch actually bound/ran it —
    that's a known, unmodified edge case documented below, not fixed here.
"""

from __future__ import annotations

import pytest

from app.agent.neutral import ToolCall
from app.agent.tooling import dispatch
from app.agent.tools.no_reply import no_reply

pytestmark = pytest.mark.unit


class TestNoReplyReasonSchema:
    def test_reason_is_required_in_reflected_schema(self):
        params = no_reply.definition.parameters
        assert "reason" in params.get("required", [])
        assert "reason" in params["properties"]


class TestDispatchWithoutReason:
    @pytest.mark.asyncio
    async def test_missing_reason_returns_binding_error_result_not_exception(self):
        call = ToolCall(id="c1", name="no_reply", arguments={})
        result = await dispatch([no_reply], call)
        assert result.tool_call_id == "c1"
        # binding failure comes back as a ToolOutcomeError dict, not a raised
        # TypeError — the whole turn must stay alive.
        assert isinstance(result.content, dict)
        assert result.content["kind"] == "invalid_args"

    def test_missing_reason_is_still_a_terminal_tool_call_by_name(self):
        """Known, unmodified edge case: _is_terminal_tool_call only looks at
        the tool name, so a no_reply call that fails to bind (no real
        model-authored reason) still ends the turn immediately."""
        from app.agent.core import _is_terminal_tool_call

        call = ToolCall(id="c1", name="no_reply", arguments={})
        assert _is_terminal_tool_call(call) is True


class TestDispatchWithReason:
    @pytest.mark.asyncio
    async def test_reason_provided_dispatches_successfully(self):
        call = ToolCall(id="c1", name="no_reply", arguments={"reason": "对方在钓鱼式逼回应"})
        result = await dispatch([no_reply], call)
        assert result.tool_call_id == "c1"
        assert result.content == "本轮不回复。"
