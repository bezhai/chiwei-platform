"""T2: life stimulus 的「最近聊过的对话」段渲染 —— ``_format_recent_chats`` 纯函数。

把 T1 查询返回的 ``LifeChatConversation`` 列表渲染成按会话分组的消息列表：
私聊 / 群分清（群带群名）、她自己的回复显示「我」、每条 ``（时间）发言人：内容``
忠实呈现，不加工成「某人对你说 X」、不截断单条内容。

主动私聊具名化 Task 2：私聊头部标注对面是谁 + ``user:<uuid>`` 句柄（有 counterparts
才具名，没有保持匿名兜底）；群头部在群名之外带 ``group:<uuid>`` 句柄——让她主动
发消息时首发 uid 即合法，不用编。句柄标注只在会话头部，不进 speaker 名。
"""

from __future__ import annotations

from app.data.message_record import (
    LifeChatConversation,
    LifeChatCounterpart,
    LifeChatMessage,
)
from app.nodes.life_wake import _format_recent_chats


def _msg(
    speaker: str, is_self: bool, text: str, t: str, *, mid: str | None = None
) -> LifeChatMessage:
    return LifeChatMessage(
        message_id=mid or f"m-{abs(hash((speaker, is_self, text, t)))}",
        speaker_display_name=speaker,
        is_self=is_self,
        text=text,
        cst_time=t,
    )


def test_direct_chat_self_shown_as_me():
    """私聊：真人话标真人展示名、她自己的回复显示「我」（不是 persona_id）。"""
    convs = [
        LifeChatConversation(
            chat_id="c1",
            scope="direct",
            display_name=None,
            messages=[
                _msg("贝壳", False, "赤尾在吗", "08:30 CST"),
                _msg("akao", True, "在的在的", "08:31 CST"),
            ],
        )
    ]
    out = _format_recent_chats(convs)
    assert "贝壳：赤尾在吗" in out
    assert "08:30 CST" in out
    assert "我：在的在的" in out
    # 她自己的回复绝不以 persona_id 露出
    assert "akao：在的在的" not in out


def test_group_chat_shows_group_name():
    """群：标群名，群里别人之间的话也忠实呈现（她本来就在群里、会感知到）。"""
    convs = [
        LifeChatConversation(
            chat_id="g1",
            scope="group",
            display_name="赤尾应援团",
            messages=[
                _msg("路人A", False, "今晚直播吗", "09:00 CST"),
                _msg("路人B", False, "求歌单", "09:01 CST"),
                _msg("akao", True, "八点见", "09:02 CST"),
            ],
        )
    ]
    out = _format_recent_chats(convs)
    assert "赤尾应援团" in out
    assert "路人A：今晚直播吗" in out
    assert "路人B：求歌单" in out
    assert "我：八点见" in out


def test_multiple_conversations_grouped_separately():
    """多个会话各自成块、各自带自己的消息，私聊与群分组清楚。"""
    convs = [
        LifeChatConversation(
            chat_id="c1",
            scope="direct",
            display_name=None,
            messages=[_msg("贝壳", False, "嗨", "08:00 CST")],
        ),
        LifeChatConversation(
            chat_id="g1",
            scope="group",
            display_name="家族群",
            messages=[_msg("千凪", False, "吃饭了", "08:05 CST")],
        ),
    ]
    out = _format_recent_chats(convs)
    assert "嗨" in out
    assert "吃饭了" in out
    assert "家族群" in out
    # 有一个总标题
    assert "最近" in out


def test_group_without_name_falls_back():
    """群名缺失（查不到）兜底，不崩、不把 None 拼进文案。"""
    convs = [
        LifeChatConversation(
            chat_id="g1",
            scope="group",
            display_name=None,
            messages=[_msg("某人", False, "在吗", "10:00 CST")],
        )
    ]
    out = _format_recent_chats(convs)
    assert "某人：在吗" in out
    assert "None" not in out


def test_direct_chat_header_names_counterpart_with_handle():
    """私聊具名（Task 2）：头部标对象名 + user:<uuid> 句柄；句柄只在头部、不进 speaker 名。"""
    convs = [
        LifeChatConversation(
            chat_id="c1",
            scope="direct",
            display_name=None,
            messages=[
                _msg("田申", False, "赤尾在吗", "08:30 CST"),
                _msg("akao", True, "在的在的", "08:31 CST"),
            ],
            counterparts=[LifeChatCounterpart(user_id="u-1", display_name="田申")],
        )
    ]
    out = _format_recent_chats(convs)
    assert "· 和 田申（user:u-1）的私聊里：" in out
    assert "一段私聊里" not in out, "具名了就不再用匿名兜底头"
    # 句柄标注在会话头部，不进 speaker 名：消息行仍是「田申：」原样
    assert "田申：赤尾在吗" in out
    assert "user:u-1）：赤尾在吗" not in out


def test_direct_chat_multiple_counterparts_all_listed():
    """约定外脏数据多对象：如实全列在头部（不替她挑「主对象」），各带各的句柄。"""
    convs = [
        LifeChatConversation(
            chat_id="c1",
            scope="direct",
            display_name=None,
            messages=[_msg("原智鸿", False, "在吗", "09:00 CST")],
            counterparts=[
                LifeChatCounterpart(user_id="u-2", display_name="原智鸿"),
                LifeChatCounterpart(user_id="u-1", display_name="田申"),
            ],
        )
    ]
    out = _format_recent_chats(convs)
    assert "· 和 原智鸿（user:u-2）、田申（user:u-1）的私聊里：" in out


def test_direct_chat_without_counterpart_keeps_anonymous_header():
    """全历史无真人行（对方从没回过）：保持现状匿名兜底，不硬造名字、不拼 None。"""
    convs = [
        LifeChatConversation(
            chat_id="c1",
            scope="direct",
            display_name=None,
            messages=[_msg("akao", True, "在想你", "10:00 CST")],
            counterparts=[],
        )
    ]
    out = _format_recent_chats(convs)
    assert "· 一段私聊里：" in out
    assert "user:" not in out, "没有对象就没有句柄，不硬造"
    assert "None" not in out


def test_group_header_carries_group_handle():
    """群头部（Task 2）：群名之外带 group:<uuid> 句柄（口吻同历史动静的群句柄标注）。"""
    convs = [
        LifeChatConversation(
            chat_id="g1",
            scope="group",
            display_name="赤尾应援团",
            messages=[_msg("路人A", False, "今晚直播吗", "09:00 CST")],
        )
    ]
    out = _format_recent_chats(convs)
    assert "群「赤尾应援团」" in out
    assert "群句柄 group:g1" in out


def test_group_without_name_still_carries_handle_no_none():
    """群名缺失：兜底头也带群句柄，绝不把 None 拼进文案。"""
    convs = [
        LifeChatConversation(
            chat_id="g1",
            scope="group",
            display_name=None,
            messages=[_msg("某人", False, "在吗", "10:00 CST")],
        )
    ]
    out = _format_recent_chats(convs)
    assert "群句柄 group:g1" in out
    assert "None" not in out


def test_content_not_truncated_or_rewritten():
    """单条内容不截断、不改写成叙述体。"""
    long = "这是一段很长的真实消息" * 20
    convs = [
        LifeChatConversation(
            chat_id="c1",
            scope="direct",
            display_name=None,
            messages=[_msg("贝壳", False, long, "08:00 CST")],
        )
    ]
    out = _format_recent_chats(convs)
    assert long in out
    assert "对你说" not in out
    assert "你回了" not in out
