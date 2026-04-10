"""统一的消息时间线格式化

将消息列表格式化为 [HH:MM] speaker: content 的文本时间线。
合并了原来分散在 relationship_memory、glimpse、identity_drift、afterthought 中的 4 份实现。
"""

from collections.abc import Callable
from datetime import datetime, timezone

from app.orm.crud import get_username
from app.utils.content_parser import parse_content


async def format_timeline(
    messages: list,
    persona_name: str,
    *,
    tz: timezone | None = None,
    max_messages: int | None = None,
    with_ids: bool = False,
    username_resolver: Callable | None = None,
) -> str:
    """Format message list as timestamped timeline text.

    Format: [HH:MM] speaker: content (truncated to 200 chars)
    with_ids=True: #id [HH:MM] speaker: content

    Args:
        messages: ConversationMessage 列表（需要 .role, .content, .user_id, .create_time, .id）
        persona_name: assistant 消息使用的名字
        tz: 时区，默认 UTC
        max_messages: 最多保留几条（从末尾截取），None 表示不限
        with_ids: 是否在每行前加 #msg_id（供 LLM 引用消息）
        username_resolver: 自定义用户名解析函数，签名 async (user_id) -> str | None
    """
    if tz is None:
        tz = timezone.utc

    if max_messages is not None:
        messages = messages[-max_messages:]

    resolve_name = username_resolver or get_username

    lines: list[str] = []
    for msg in messages:
        msg_time = datetime.fromtimestamp(msg.create_time / 1000, tz=tz)
        time_str = msg_time.strftime("%H:%M")

        if msg.role == "assistant":
            speaker = persona_name
        else:
            name = await resolve_name(msg.user_id)
            speaker = name or msg.user_id[:6]

        rendered = parse_content(msg.content).render()
        if rendered and rendered.strip():
            prefix = f"#{msg.id} " if with_ids and msg.id else ""
            lines.append(f"{prefix}[{time_str}] {speaker}: {rendered[:200]}")

    return "\n".join(lines)
