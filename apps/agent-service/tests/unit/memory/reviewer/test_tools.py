"""Tests for app.memory.reviewer.tools — reviewer-only graph mutation tools."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

MODULE = "app.memory.reviewer.tools"


def _noop_session():
    """Async context manager that yields a dummy session."""

    @asynccontextmanager
    async def _cm():
        yield MagicMock()

    return _cm()


# ---------------------------------------------------------------------------
# update_abstract_content
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_abstract_content_writes_content():
    from app.memory.reviewer.tools import update_abstract_content

    with (
        patch(f"{MODULE}.get_session", return_value=_noop_session()),
        patch(f"{MODULE}.update_abstract_content_query", new=AsyncMock()) as q,
    ):
        result = await update_abstract_content.ainvoke(
            {"abstract_id": "a_1", "new_content": "updated content", "reason": "merge"}
        )

    assert result == {"ok": True}
    q.assert_awaited_once()
    call_kwargs = q.await_args.kwargs
    assert call_kwargs["abstract_id"] == "a_1"
    assert call_kwargs["new_content"] == "updated content"


# ---------------------------------------------------------------------------
# fade_node
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fade_node_sets_clarity():
    from app.memory.reviewer.tools import fade_node

    with (
        patch(f"{MODULE}.get_session", return_value=_noop_session()),
        patch(f"{MODULE}.set_clarity", new=AsyncMock()) as q,
    ):
        result = await fade_node.ainvoke(
            {"node_id": "a_1", "node_type": "abstract", "clarity": "vague", "reason": "old"}
        )

    assert result == {"ok": True}
    q.assert_awaited_once()
    call_kwargs = q.await_args.kwargs
    assert call_kwargs["node_id"] == "a_1"
    assert call_kwargs["node_type"] == "abstract"
    assert call_kwargs["clarity"] == "vague"


@pytest.mark.asyncio
async def test_fade_node_rejects_invalid_clarity():
    from app.memory.reviewer.tools import fade_node

    result = await fade_node.ainvoke(
        {"node_id": "a_1", "node_type": "abstract", "clarity": "gone", "reason": "test"}
    )

    assert result["ok"] is False
    assert "invalid clarity" in result["error"]


# ---------------------------------------------------------------------------
# touch_node
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_touch_node_abstract_calls_touch_abstract():
    from app.memory.reviewer.tools import touch_node

    with (
        patch(f"{MODULE}.get_session", return_value=_noop_session()),
        patch(f"{MODULE}.touch_abstract", new=AsyncMock()) as ta,
        patch(f"{MODULE}.touch_fragment", new=AsyncMock()) as tf,
    ):
        result = await touch_node.ainvoke({"node_id": "a_1", "node_type": "abstract"})

    assert result == {"ok": True}
    ta.assert_awaited_once()
    tf.assert_not_awaited()


@pytest.mark.asyncio
async def test_touch_node_fact_calls_touch_fragment():
    from app.memory.reviewer.tools import touch_node

    with (
        patch(f"{MODULE}.get_session", return_value=_noop_session()),
        patch(f"{MODULE}.touch_abstract", new=AsyncMock()) as ta,
        patch(f"{MODULE}.touch_fragment", new=AsyncMock()) as tf,
    ):
        result = await touch_node.ainvoke({"node_id": "f_1", "node_type": "fact"})

    assert result == {"ok": True}
    tf.assert_awaited_once()
    ta.assert_not_awaited()


@pytest.mark.asyncio
async def test_touch_node_rejects_unknown_type():
    from app.memory.reviewer.tools import touch_node

    with (
        patch(f"{MODULE}.get_session", return_value=_noop_session()),
        patch(f"{MODULE}.touch_abstract", new=AsyncMock()),
        patch(f"{MODULE}.touch_fragment", new=AsyncMock()),
    ):
        result = await touch_node.ainvoke({"node_id": "x_1", "node_type": "unknown"})

    assert result["ok"] is False
    assert "unknown node_type" in result["error"]


# ---------------------------------------------------------------------------
# delete_fragment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_fragment_deletes():
    from app.memory.reviewer.tools import delete_fragment

    with (
        patch(f"{MODULE}.get_session", return_value=_noop_session()),
        patch(f"{MODULE}.delete_fragment_query", new=AsyncMock()) as q,
    ):
        result = await delete_fragment.ainvoke(
            {"fragment_id": "f_1", "reason": "trivial noise"}
        )

    assert result == {"ok": True}
    q.assert_awaited_once()
    assert q.await_args.kwargs["fragment_id"] == "f_1"


# ---------------------------------------------------------------------------
# connect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_uses_from_node_persona():
    from app.memory.reviewer.tools import connect

    fake_node = MagicMock()
    fake_node.persona_id = "chiwei"

    with (
        patch(f"{MODULE}.get_session", return_value=_noop_session()),
        patch(f"{MODULE}.get_fragment_by_id", new=AsyncMock(return_value=fake_node)),
        patch(f"{MODULE}.insert_memory_edge", new=AsyncMock()) as ins,
    ):
        result = await connect.ainvoke(
            {
                "from_id": "f_1",
                "from_type": "fact",
                "to_id": "a_1",
                "to_type": "abstract",
                "edge_type": "supports",
                "reason": "evidence",
            }
        )

    assert result == {"ok": True}
    ins.assert_awaited_once()
    call_kwargs = ins.await_args.kwargs
    assert call_kwargs["persona_id"] == "chiwei"
    assert call_kwargs["from_id"] == "f_1"
    assert call_kwargs["to_id"] == "a_1"
    assert call_kwargs["edge_type"] == "supports"
    assert call_kwargs["created_by"] == "reviewer"


@pytest.mark.asyncio
async def test_connect_rejects_invalid_edge_type():
    from app.memory.reviewer.tools import connect

    result = await connect.ainvoke(
        {
            "from_id": "f_1",
            "from_type": "fact",
            "to_id": "a_1",
            "to_type": "abstract",
            "edge_type": "invalidtype",
            "reason": "test",
        }
    )

    assert result["ok"] is False
    assert "invalid edge_type" in result["error"]


@pytest.mark.asyncio
async def test_connect_returns_error_when_from_node_missing():
    from app.memory.reviewer.tools import connect

    with (
        patch(f"{MODULE}.get_session", return_value=_noop_session()),
        patch(f"{MODULE}.get_fragment_by_id", new=AsyncMock(return_value=None)),
        patch(f"{MODULE}.insert_memory_edge", new=AsyncMock()) as ins,
    ):
        result = await connect.ainvoke(
            {
                "from_id": "f_missing",
                "from_type": "fact",
                "to_id": "a_1",
                "to_type": "abstract",
                "edge_type": "supports",
                "reason": "test",
            }
        )

    assert result["ok"] is False
    assert "not found" in result["error"]
    ins.assert_not_awaited()


# ---------------------------------------------------------------------------
# disconnect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disconnect_removes_edge():
    from app.memory.reviewer.tools import disconnect

    with (
        patch(f"{MODULE}.get_session", return_value=_noop_session()),
        patch(f"{MODULE}.delete_edge", new=AsyncMock()) as q,
    ):
        result = await disconnect.ainvoke(
            {"edge_id": "e_1", "reason": "duplicate edge"}
        )

    assert result == {"ok": True}
    q.assert_awaited_once()
    assert q.await_args.kwargs["edge_id"] == "e_1"


# ---------------------------------------------------------------------------
# make_reviewer_tools
# ---------------------------------------------------------------------------


def test_make_reviewer_tools_returns_all_eight():
    with (
        patch("app.agent.tools.commit_abstract.detect_conflict", new=AsyncMock()),
        patch("app.agent.tools.commit_abstract.insert_abstract_memory", new=AsyncMock()),
        patch("app.agent.tools.commit_abstract.insert_memory_edge", new=AsyncMock()),
        patch("app.agent.tools.commit_abstract.enqueue_abstract_vectorize", new=AsyncMock()),
    ):
        from app.memory.reviewer.tools import make_reviewer_tools

        tools = make_reviewer_tools()

    assert len(tools) == 8
    tool_names = {t.name for t in tools}
    assert "update_abstract_content" in tool_names
    assert "fade_node" in tool_names
    assert "touch_node" in tool_names
    assert "delete_fragment" in tool_names
    assert "connect" in tool_names
    assert "disconnect" in tool_names
    assert "commit_abstract_memory" in tool_names
    assert "recall" in tool_names
