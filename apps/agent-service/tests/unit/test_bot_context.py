# apps/agent-service/tests/unit/test_bot_context.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from langchain_core.messages import AIMessage, HumanMessage

from app.services.bot_context import BotContext


def _make_msg(role: str, content: str, bot_name: str | None, username: str):
    from app.services.quick_search import QuickSearchResult
    from datetime import datetime
    import inspect
    sig = inspect.signature(QuickSearchResult.__init__)
    if 'bot_name' in sig.parameters:
        m = QuickSearchResult(
            message_id="m1", content=content, user_id="u1",
            create_time=datetime.now(), role=role, username=username, bot_name=bot_name,
        )
    else:
        m = QuickSearchResult(
            message_id="m1", content=content, user_id="u1",
            create_time=datetime.now(), role=role, username=username,
        )
        m.bot_name = bot_name  # 临时 monkey-patch
    return m


def test_build_chat_history_current_bot_is_assistant():
    """当前 bot 的消息应映射为 AIMessage"""
    ctx = BotContext(chat_id="c1", bot_name="chiwei", chat_type="group")
    msgs = [
        _make_msg("assistant", "你好", "chiwei", "赤尾"),
        _make_msg("user", "嗨", None, "张三"),
    ]
    result = ctx.build_chat_history(msgs)
    assert isinstance(result[0], AIMessage)
    assert isinstance(result[1], HumanMessage)


def test_build_chat_history_other_bot_is_human():
    """其他 bot 的消息应映射为 HumanMessage，带名字前缀"""
    ctx = BotContext(chat_id="c1", bot_name="chiwei", chat_type="group")
    msgs = [
        _make_msg("assistant", "我是千凪", "chinagi", "千凪"),
        _make_msg("assistant", "我是赤尾", "chiwei", "赤尾"),
    ]
    result = ctx.build_chat_history(msgs)
    assert isinstance(result[0], HumanMessage)
    assert "千凪" in str(result[0].content)
    assert isinstance(result[1], AIMessage)


def test_get_error_message_uses_persona():
    """get_error_message 应从 persona 读，不硬编码"""
    ctx = BotContext(chat_id="c1", bot_name="chiwei", chat_type="group")
    ctx._persona = MagicMock()
    ctx._persona.display_name = "赤尾"
    ctx._persona.error_messages = {"guard": "赤尾不想讨论这个~"}
    assert ctx.get_error_message("guard") == "赤尾不想讨论这个~"
    assert "赤尾" in ctx.get_error_message("unknown_key")
