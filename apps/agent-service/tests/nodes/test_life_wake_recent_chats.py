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


# --- 段头口径：标「我」的是她自己已发出的话 + 没人新开口就是真没人说话 -----------
#
# 2026-07-25 事故（她在群里连发 7 条跟不存在的对话争吵）的第二个落点：每行只用一个字
# 「我」区分自他，而并列的【你刚对这次交流的回复】段有整句框架（「这是你自己刚发出去
# 的话，不是别人对你说的，已经发过了、不用再回一遍」）。两段口径不一致，她把自己刚发
# 的话读回来当成别人在跟她说话。段头补上两层意思，**只改段头、不动每行渲染**（忠实
# 呈现红线仍在：不许改写成「某人对你说 X / 你回了 Y」的叙述体）。


def test_header_marks_her_own_lines_as_hers():
    """段头只做一件事：点出标「我」的那些行是她自己发出去的话（输入溯源）。

    2026-07-25 事故里她把自己的回声当成别人在跟她说话，根因是这一段区分自他只靠一个
    字「我」，而并列的【你刚对这次交流的回复】段有整句框架。补的是**溯源**，不是别的。
    """
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
    header = out.splitlines()[0]
    assert "标「我」的" in header, "段头要点出标「我」的那些行是谁说的"


def test_header_neither_claims_completeness_nor_directs_behavior():
    """**承重红线**：段头不许宣称全量、不许下行为指令、不许给情绪框架。

    ① 不许宣称全量 —— 这一段真实上限是 :data:`_RECENT_CHAT_MAX_CONVERSATIONS` 个会话
       × 每会话 :data:`_RECENT_CHAT_PER_CHAT_LIMIT` 条，还过白名单。说「这里就是全部」
       是**假话**：真有第 6 个会话或第 11 条，她看到的就不是全部，而她会把**截断误当成
       沉默**——比不提示更糟。
    ② 不许下行为指令 / 给情绪框架 —— 「不必再说一遍」是指令，「那份安静也是真的」是
       情绪暗示。沉默本来就是可见的（没人说话，那些行就不在），不需要谁替她解读。赤尾
       宪法：不确定性来自她自己的判断，不是塞给她的暗示。
    """
    convs = [
        LifeChatConversation(
            chat_id="g1",
            scope="group",
            display_name="赤尾应援团",
            messages=[_msg("akao", True, "八点见", "09:02 CST")],
        )
    ]
    out = _format_recent_chats(convs)
    header = out.splitlines()[0]
    for claim in ("全部", "全都在", "所有"):
        assert claim not in header, f"段头不许宣称全量（有 5×10 上限）：命中「{claim}」"
    for directive in ("不必再说", "不用再说", "安静", "没人在跟你说话"):
        assert directive not in header, (
            f"段头只做输入溯源，不许下行为指令 / 给情绪框架：命中「{directive}」"
        )


def test_new_header_keeps_faithful_rendering_red_line():
    """新段头不许把对话改写成叙述体：「对你说」/「你回了」仍然一个字都不许出现。"""
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
    assert "对你说" not in out
    assert "你回了" not in out
    # 段头是提醒她怎么读这些行，不是系统腔的元信息说明
    assert "系统" not in out
    assert "上下文" not in out


def test_header_only_changed_line_format_untouched():
    """改法限定在段头：每行仍是「（时间）发言人：内容」，「我：」前缀不动。"""
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
    assert "  （08:30 CST）贝壳：赤尾在吗" in out
    assert "  （08:31 CST）我：在的在的" in out
