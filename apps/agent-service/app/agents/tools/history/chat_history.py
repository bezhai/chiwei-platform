"""check_chat_history 工具 — 翻聊天记录

读 conversation_messages 原始消息，按时间和关键词过滤。
"""

import logging
from datetime import datetime, timedelta, timezone

from langchain.tools import tool
from langgraph.runtime import get_runtime

from app.agents.core.context import AgentContext
from app.agents.tools.decorators import tool_error_handler
from app.orm.crud import get_chat_messages_in_range, get_username
from app.services.content_parser import parse_content

logger = logging.getLogger(__name__)
CST = timezone(timedelta(hours=8))
MAX_MESSAGES = 50
LOOKBACK_HOURS = 24


@tool
@tool_error_handler(error_message="翻不到了...")
async def check_chat_history(what_to_look_for: str, time_hint: str = "") -> str:
    """翻翻聊天记录看看。
    你没印象但想确认一下的时候，可以翻翻聊天记录。
    Args:
        what_to_look_for: 你想找什么
        time_hint: 大概什么时候的（如"今天上午"、"昨天"），不确定可以不填
    """
    context = get_runtime(AgentContext).context
    chat_id = context.message.chat_id

    now = datetime.now(CST)
    hours = _parse_time_hint(time_hint)
    start_ts = int((now - timedelta(hours=hours)).timestamp() * 1000)
    end_ts = int(now.timestamp() * 1000)

    messages = await get_chat_messages_in_range(chat_id, start_ts, end_ts)
    if not messages:
        return "这段时间好像没有聊天记录..."

    messages = messages[-MAX_MESSAGES:]
    lines = []
    for msg in messages:
        msg_time = datetime.fromtimestamp(msg.create_time / 1000, tz=CST)
        time_str = msg_time.strftime("%m/%d %H:%M")
        if msg.role == "assistant":
            speaker = "我"
        else:
            name = await get_username(msg.user_id)
            speaker = name or "?"
        rendered = parse_content(msg.content).render()
        if rendered and rendered.strip():
            lines.append(f"[{time_str}] {speaker}: {rendered[:150]}")

    if not lines:
        return "翻了但没看到什么..."

    keywords = [w for w in what_to_look_for.split() if len(w) >= 2]
    if keywords:
        filtered = [line for line in lines if any(k in line for k in keywords)]
        if filtered:
            return "找到了一些相关的记录：\n" + "\n".join(filtered[-20:])

    return "最近的聊天记录：\n" + "\n".join(lines[-20:])


def _parse_time_hint(hint: str) -> int:
    if not hint:
        return LOOKBACK_HOURS
    hint = hint.strip()
    if "昨天" in hint:
        return 48
    if "前天" in hint:
        return 72
    if "今天" in hint or "刚才" in hint:
        return 12
    if "上午" in hint or "下午" in hint:
        return 12
    return LOOKBACK_HOURS
