"""Tests for nodes/safety.py (Phase 2)."""
from __future__ import annotations


def test_module_imports():
    """烟囱测试：模块能加载，含必要 helper / 常量。"""
    from app.nodes import safety as m

    assert hasattr(m, "_check_banned_word")
    assert hasattr(m, "_check_injection")
    assert hasattr(m, "_check_politics")
    assert hasattr(m, "_check_nsfw")
    assert hasattr(m, "_check_output")
    assert hasattr(m, "_run_audit")
    assert hasattr(m, "BlockReason")
    assert hasattr(m, "TERMINAL_STATUSES")
    # TERMINAL_STATUSES 内容
    assert m.TERMINAL_STATUSES == frozenset(
        {"passed", "blocked", "recalled", "recall_failed"}
    )


# === run_post_safety ===

import pytest
from datetime import UTC, datetime  # noqa: F401  # parity with module
from unittest.mock import AsyncMock, MagicMock, patch

from app.domain.safety import PostSafetyRequest, Recall


def _make_req(session_id="sess-1") -> PostSafetyRequest:
    return PostSafetyRequest(
        session_id=session_id,
        trigger_message_id="msg-1",
        chat_id="chat-1",
        response_text="hello world",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["passed", "blocked", "recalled", "recall_failed"])
async def test_run_post_safety_short_circuits_on_terminal_status(status):
    """terminal 状态下短路 return None，不调 _run_audit / set_safety_status."""
    from app.nodes import safety as m

    req = _make_req()
    fake_get = AsyncMock(return_value=status)
    fake_audit = AsyncMock()
    fake_set = AsyncMock()
    fake_session = AsyncMock()
    fake_session.__aenter__.return_value = fake_session
    fake_session.__aexit__.return_value = None
    fake_get_session = MagicMock(return_value=fake_session)

    with (
        patch.object(m, "get_safety_status", fake_get),
        patch.object(m, "_run_audit", fake_audit),
        patch.object(m, "set_safety_status", fake_set),
        patch.object(m, "get_session", fake_get_session),
    ):
        result = await m.run_post_safety(req)

    assert result is None
    fake_audit.assert_not_called()
    fake_set.assert_not_called()


@pytest.mark.asyncio
async def test_run_post_safety_raises_when_row_missing():
    """row 不存在 → raise RuntimeError → durable handler 进 DLQ."""
    from app.nodes import safety as m

    fake_get = AsyncMock(return_value=None)
    fake_session = AsyncMock()
    fake_session.__aenter__.return_value = fake_session
    fake_session.__aexit__.return_value = None
    fake_get_session = MagicMock(return_value=fake_session)

    with (
        patch.object(m, "get_safety_status", fake_get),
        patch.object(m, "get_session", fake_get_session),
    ):
        with pytest.raises(RuntimeError) as excinfo:
            await m.run_post_safety(_make_req("missing-row"))
    assert "missing-row" in str(excinfo.value)
    assert "lark-server" in str(excinfo.value)


@pytest.mark.asyncio
async def test_run_post_safety_passed_writes_status_and_returns_none():
    """audit pass → set_safety_status('passed', ...) + return None（不产 Recall）."""
    from app.nodes import safety as m

    fake_get = AsyncMock(return_value="pending")
    fake_audit = AsyncMock(return_value=m._PostAuditOutcome(is_blocked=False))
    fake_set = AsyncMock()
    fake_session = AsyncMock()
    fake_session.__aenter__.return_value = fake_session
    fake_session.__aexit__.return_value = None
    fake_get_session = MagicMock(return_value=fake_session)

    with (
        patch.object(m, "get_safety_status", fake_get),
        patch.object(m, "_run_audit", fake_audit),
        patch.object(m, "set_safety_status", fake_set),
        patch.object(m, "get_session", fake_get_session),
    ):
        result = await m.run_post_safety(_make_req("sess-pass"))

    assert result is None
    fake_set.assert_awaited_once()
    args = fake_set.await_args.args
    assert args[1] == "sess-pass"
    assert args[2] == "passed"


@pytest.mark.asyncio
async def test_run_post_safety_blocked_returns_recall_without_writing_status():
    """audit blocked → return Recall，不调 set_safety_status（recall-worker 写终态）."""
    from app.nodes import safety as m

    fake_get = AsyncMock(return_value="pending")
    fake_audit = AsyncMock(
        return_value=m._PostAuditOutcome(
            is_blocked=True, reason="output_unsafe", detail="confidence=0.9"
        )
    )
    fake_set = AsyncMock()
    fake_session = AsyncMock()
    fake_session.__aenter__.return_value = fake_session
    fake_session.__aexit__.return_value = None
    fake_get_session = MagicMock(return_value=fake_session)

    with (
        patch.object(m, "get_safety_status", fake_get),
        patch.object(m, "_run_audit", fake_audit),
        patch.object(m, "set_safety_status", fake_set),
        patch.object(m, "get_session", fake_get_session),
        patch.object(m, "get_lane", MagicMock(return_value="dev")),
    ):
        result = await m.run_post_safety(_make_req("sess-block"))

    assert isinstance(result, Recall)
    assert result.session_id == "sess-block"
    assert result.reason == "output_unsafe"
    assert result.detail == "confidence=0.9"
    assert result.lane == "dev"
    fake_set.assert_not_called()
