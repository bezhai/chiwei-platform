"""Tests for app.life.proactive."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

MODULE = "app.life.proactive"


@asynccontextmanager
async def _fake_tx():
    yield


def _make_emit_tx_mock():
    captured: list = []

    async def _fake_emit_tx(ev):
        captured.append(ev)

    return _fake_emit_tx, captured


def _make_insert_mock():
    """Capture ConversationMessage entities passed to insert_proactive_message."""
    captured: list = []

    async def _fake_insert(message):
        captured.append(message)

    return _fake_insert, captured


@pytest.mark.asyncio
async def test_submit_proactive_chat_skips_lark_raw_root_to_prevent_reverse_resolve_crash():
    """Bug 2: target_msg.message_id 仍是飞书裸 om_* 时，必须不放进
    ChatTrigger.root_id —— 否则 chat-response-worker 出站
    reverseResolveForLark(rootGlobalId=om_x...) 抛 IdentityNotFoundError，
    回复整段炸（prod 已遇到丢消息）。同样保护 root_message_id 落库链路。

    Pre-T5-5c 数据迁移完成前 conversation_messages.message_id 可能仍是
    飞书裸 id，proactive 取出来不能盲信是全局 ULID。"""
    from app.domain.chat_dataflow import ChatTrigger
    from app.domain.message import Message
    from app.life.proactive import submit_proactive_chat

    target = SimpleNamespace(
        message_id="om_target",
        root_message_id="om_root",
        chat_id="oc_test",
    )
    fake_emit, captured = _make_emit_tx_mock()
    fake_insert, inserted = _make_insert_mock()

    with (
        patch(f"{MODULE}.tx", _fake_tx),
        patch("app.data.queries.insert_proactive_message", fake_insert),
        patch("app.data.queries.find_message_by_id", AsyncMock(return_value=target)),
        patch(
            "app.data.queries.resolve_bot_name_for_persona",
            AsyncMock(return_value="akao"),
        ),
        patch(f"{MODULE}.time.time", return_value=1234.567),
        patch(f"{MODULE}.uuid.uuid4", return_value="session-1"),
        patch(f"{MODULE}.emit_tx", fake_emit),
        patch("app.infra.rabbitmq.current_lane", return_value="prod"),
    ):
        session_id = await submit_proactive_chat(
            chat_id="oc_test",
            persona_id="akao-001",
            target_message_id="om_target",
            stimulus="想接一句",
        )

    assert session_id == "session-1"
    assert len(inserted) == 1, f"expect 1 insert_proactive_message call, got {inserted}"
    added = inserted[0]
    assert added.message_id == "proactive_1234567"
    # 飞书裸 root_message_id / reply_message_id 不可落入 conversation_messages
    # 否则回复链 walk 按全局 message_id 主键失配
    assert added.root_message_id == "proactive_1234567", (
        f"lark raw root_message_id must not be persisted; got {added.root_message_id!r}"
    )
    assert added.reply_message_id is None, (
        f"lark raw reply_message_id must not be persisted; got {added.reply_message_id!r}"
    )

    # Both Message and ChatTrigger are appended to the outbox in call order
    assert len(captured) == 2, f"expect 2 appends, got {len(captured)}: {captured}"
    assert isinstance(captured[0], Message), (
        f"first append must be Message, got {type(captured[0]).__name__}"
    )
    assert isinstance(captured[1], ChatTrigger), (
        f"second append must be ChatTrigger, got {type(captured[1]).__name__}"
    )
    trigger = captured[1]
    assert trigger.message_id == "proactive_1234567"
    assert trigger.session_id == "session-1"
    assert trigger.chat_id == "oc_test"
    assert trigger.is_p2p is False
    # 关键断言：飞书裸 om_* 不能放进 root_id，否则 chat-response-worker 出站
    # reverseResolveForLark 必抛 IdentityNotFoundError 丢消息
    assert trigger.root_id is None, (
        f"lark raw om_* must not leak into ChatTrigger.root_id; "
        f"got {trigger.root_id!r} (would crash chat-response-worker)"
    )
    assert trigger.user_id == "__proactive__"
    assert trigger.bot_name == "akao"
    assert trigger.is_proactive is True
    assert trigger.lane == "prod"


@pytest.mark.asyncio
async def test_submit_proactive_chat_resolves_numeric_target_row_id_skips_lark_raw():
    """Bug 2 同款保护：行号反查回 lark 裸 message_id 时同样不准落
    ChatTrigger.root_id / conversation_messages.reply_message_id。"""
    from app.domain.chat_dataflow import ChatTrigger
    from app.life.proactive import submit_proactive_chat

    target = SimpleNamespace(
        message_id="om_from_row",
        root_message_id="om_root",
        chat_id="oc_test",
    )
    fake_emit, captured = _make_emit_tx_mock()
    fake_insert, inserted = _make_insert_mock()

    with (
        patch(f"{MODULE}.tx", _fake_tx),
        patch("app.data.queries.insert_proactive_message", fake_insert),
        patch(
            "app.data.queries.resolve_message_id_by_row_id",
            AsyncMock(return_value="om_from_row"),
        ) as mock_resolve_row,
        patch("app.data.queries.find_message_by_id", AsyncMock(return_value=target)),
        patch(
            "app.data.queries.resolve_bot_name_for_persona",
            AsyncMock(return_value="akao"),
        ),
        patch(f"{MODULE}.time.time", return_value=1234.567),
        patch(f"{MODULE}.uuid.uuid4", return_value="session-2"),
        patch(f"{MODULE}.emit_tx", fake_emit),
        patch("app.infra.rabbitmq.current_lane", return_value="prod"),
    ):
        await submit_proactive_chat(
            chat_id="oc_test",
            persona_id="akao-001",
            target_message_id="42",
            stimulus="想接一句",
        )

    mock_resolve_row.assert_awaited_once()
    assert len(inserted) == 1
    added = inserted[0]
    # lark 裸 om_* root 不准落库
    assert added.root_message_id == "proactive_1234567"
    assert added.reply_message_id is None

    trigger = next((d for d in captured if isinstance(d, ChatTrigger)), None)
    assert trigger is not None, f"no ChatTrigger in captured: {captured}"
    # lark 裸 om_* 不准放进 ChatTrigger.root_id（会让 chat-response-worker 炸）
    assert trigger.root_id is None


@pytest.mark.asyncio
async def test_submit_proactive_chat_ignores_target_from_other_chat():
    from app.domain.chat_dataflow import ChatTrigger
    from app.life.proactive import submit_proactive_chat

    target = SimpleNamespace(
        message_id="om_other",
        root_message_id="om_other_root",
        chat_id="oc_other",
    )
    fake_emit, captured = _make_emit_tx_mock()
    fake_insert, inserted = _make_insert_mock()

    with (
        patch(f"{MODULE}.tx", _fake_tx),
        patch("app.data.queries.insert_proactive_message", fake_insert),
        patch("app.data.queries.find_message_by_id", AsyncMock(return_value=target)),
        patch(
            "app.data.queries.resolve_bot_name_for_persona",
            AsyncMock(return_value="akao"),
        ),
        patch(f"{MODULE}.time.time", return_value=1234.567),
        patch(f"{MODULE}.uuid.uuid4", return_value="session-3"),
        patch(f"{MODULE}.emit_tx", fake_emit),
        patch("app.infra.rabbitmq.current_lane", return_value="prod"),
    ):
        await submit_proactive_chat(
            chat_id="oc_test",
            persona_id="akao-001",
            target_message_id="om_other",
            stimulus="想接一句",
        )

    assert len(inserted) == 1
    added = inserted[0]
    assert added.root_message_id == "proactive_1234567"
    assert added.reply_message_id is None

    # Cross-chat target is ignored → ChatTrigger.root_id should be None
    trigger = next((d for d in captured if isinstance(d, ChatTrigger)), None)
    assert trigger is not None, f"no ChatTrigger in captured: {captured}"
    assert trigger.root_id is None


# ---------------------------------------------------------------------------
# T5-5c: 全局 ID 下 proactive 目标解析契约
#
# 身份全局化后 target_message_id / chat_id 是全局 internal_*_id（ULID =
# Crockford base32，永远不会是纯数字）。_resolve_target_message 的
# .isdigit() 分支本意是「DB 自增 row id vs message_id」，与「飞书裸 ID vs
# 全局 ID」正交：全局 ULID 永远走 find_message_by_id 直查，绝不被误判进
# resolve_message_id_by_row_id 行号反查路径。跨会话拒绝按全局 chat_id 比较。
# 本测试钉死这两条，证明 proactive 读取路径在全局 ID 下无残留飞书裸 ID 假设。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_proactive_global_ulid_target_skips_row_id_branch():
    """全局 ULID message_id 直查 find_message_by_id，不走 row-id 反查。"""
    from app.domain.chat_dataflow import ChatTrigger
    from app.life.proactive import submit_proactive_chat

    global_msg_id = "01J8XGLOBALMSGID00000000AB"
    global_chat_id = "01J8XGLOBALCHATID0000000000"
    global_root_id = "01J8XGLOBALROOTID0000000000"
    target = SimpleNamespace(
        message_id=global_msg_id,
        root_message_id=global_root_id,
        chat_id=global_chat_id,
    )
    fake_emit, captured = _make_emit_tx_mock()
    fake_insert, inserted = _make_insert_mock()

    with (
        patch(f"{MODULE}.tx", _fake_tx),
        patch("app.data.queries.insert_proactive_message", fake_insert),
        patch(
            "app.data.queries.resolve_message_id_by_row_id",
            AsyncMock(return_value="SHOULD_NOT_BE_USED"),
        ) as mock_resolve_row,
        patch(
            "app.data.queries.find_message_by_id",
            AsyncMock(return_value=target),
        ) as mock_find_by_id,
        patch(
            "app.data.queries.resolve_bot_name_for_persona",
            AsyncMock(return_value="akao"),
        ),
        patch(f"{MODULE}.time.time", return_value=1234.567),
        patch(f"{MODULE}.uuid.uuid4", return_value="session-ulid"),
        patch(f"{MODULE}.emit_tx", fake_emit),
        patch("app.infra.rabbitmq.current_lane", return_value="prod"),
    ):
        await submit_proactive_chat(
            chat_id=global_chat_id,
            persona_id="akao-001",
            target_message_id=global_msg_id,
            stimulus="想接一句",
        )

    # 全局 ULID 非纯数字 → 绝不走 row-id 反查分支
    mock_resolve_row.assert_not_awaited()
    mock_find_by_id.assert_awaited_once_with(global_msg_id)

    assert len(inserted) == 1
    added = inserted[0]
    assert added.root_message_id == global_root_id
    assert added.reply_message_id == global_msg_id

    trigger = next((d for d in captured if isinstance(d, ChatTrigger)), None)
    assert trigger is not None
    assert trigger.root_id == global_msg_id
    assert trigger.chat_id == global_chat_id


@pytest.mark.asyncio
async def test_submit_proactive_cross_chat_rejected_by_global_chat_id():
    """跨会话拒绝按全局 internal_chat_id 比较，不残留飞书裸 chat_id 假设。"""
    from app.domain.chat_dataflow import ChatTrigger
    from app.life.proactive import submit_proactive_chat

    # 目标消息属于另一个全局会话
    target = SimpleNamespace(
        message_id="01J8XOTHERMSG0000000000000",
        root_message_id="01J8XOTHERROOT000000000000",
        chat_id="01J8XOTHERCHAT000000000000",
    )
    fake_emit, captured = _make_emit_tx_mock()
    fake_insert, inserted = _make_insert_mock()

    with (
        patch(f"{MODULE}.tx", _fake_tx),
        patch("app.data.queries.insert_proactive_message", fake_insert),
        patch(
            "app.data.queries.find_message_by_id",
            AsyncMock(return_value=target),
        ),
        patch(
            "app.data.queries.resolve_bot_name_for_persona",
            AsyncMock(return_value="akao"),
        ),
        patch(f"{MODULE}.time.time", return_value=1234.567),
        patch(f"{MODULE}.uuid.uuid4", return_value="session-xchat"),
        patch(f"{MODULE}.emit_tx", fake_emit),
        patch("app.infra.rabbitmq.current_lane", return_value="prod"),
    ):
        await submit_proactive_chat(
            chat_id="01J8XCURRENTCHAT0000000000",
            persona_id="akao-001",
            target_message_id="01J8XOTHERMSG0000000000000",
            stimulus="想接一句",
        )

    # 全局 chat_id 不一致 → 目标被忽略，root_id 落 None
    assert len(inserted) == 1
    added = inserted[0]
    assert added.root_message_id == "proactive_1234567"
    assert added.reply_message_id is None
    trigger = next((d for d in captured if isinstance(d, ChatTrigger)), None)
    assert trigger is not None
    assert trigger.root_id is None
