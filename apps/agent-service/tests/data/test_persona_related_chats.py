"""life 醒来实时拉对话的查询 — 给定 persona，查她相关会话（她的真人私聊 +
白名单内的群）的最近一段消息，让 life 渲染成「按会话分组的消息列表」。

口径（spec 决策）：
- 「她相关会话」= 她在最近时间窗内**发过言**的会话（同 chat 被动唤起模型口径：
  她没发言就没被唤起、就没看见）。私聊放行；群必须过 ``life_feed_chat_whitelist``
  白名单——白名单挪到拉取侧做掉，不依赖调用方先过滤。
- 每条消息分清谁说的，含赤尾自己的回复（``is_self``），含发言者展示名、文本、
  CST 时间。私聊里真人 user 行用展示名兜底、不暴露 raw user_id。
- 数量控制不字符截断：会话数上限 + 每会话最近条数上限。

集成测试（真 Postgres）：正确性全在 join / 窗口 / 白名单 / 分组 / 上限的 SQL +
白名单语义。白名单读 Dynamic Config，集成测试里 monkeypatch ``dynamic_config.get``。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

import app.data.session as session_mod
from app.data.models import (
    Base,
    CommonAgentResponse,
    CommonConversation,
    CommonMessage,
)
from app.data.queries.messages import find_persona_related_chats_recent
from app.life import feed_whitelist as fw

_CHAT_GROUP = uuid.uuid5(uuid.NAMESPACE_OID, "rel-chat-group")
_CHAT_GROUP_2 = uuid.uuid5(uuid.NAMESPACE_OID, "rel-chat-group-2")
_CHAT_DIRECT = uuid.uuid5(uuid.NAMESPACE_OID, "rel-chat-direct")
_USER_1 = uuid.uuid5(uuid.NAMESPACE_OID, "rel-user-1")
_USER_2 = uuid.uuid5(uuid.NAMESPACE_OID, "rel-user-2")
# proactive 出站行真实落库时 common_user_id = bot 自己的 common_user_id（非 NULL）。
_BOT_USER = uuid.uuid5(uuid.NAMESPACE_OID, "rel-bot-user")

# bot_config 由 channel-server 管理、不在 agent-service 的 SQLAlchemy 模型里。proactive
# 出站行只带 bot_name（response_id=NULL、无 agent_response），发言 persona 必须经
# bot_config(bot_name → persona_id) 兜底拿到，集成测试要手动建这张表 + 灌行。
_BOT_CONFIG_DDL = (
    "CREATE TABLE bot_config ("
    "  bot_name VARCHAR(50) PRIMARY KEY,"
    "  persona_id VARCHAR(50),"
    "  is_active BOOLEAN NOT NULL DEFAULT TRUE"
    ")"
)


@pytest.fixture
async def chat_db(test_db):
    """Build the common_* tables the query joins on (+ bot_config for proactive)."""
    tables = [
        CommonMessage.__table__,
        CommonAgentResponse.__table__,
        CommonConversation.__table__,
    ]
    async with test_db.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables)
        )
        await conn.execute(text(_BOT_CONFIG_DDL))
    yield test_db


@pytest.fixture
def allow_all_groups(monkeypatch):
    """白名单默认放行所有群（除非单独 patch）：让会话筛选逻辑可独立验证。"""

    def fake_get(key: str, *, default: str = "") -> str:
        return ",".join(
            [str(_CHAT_GROUP), str(_CHAT_GROUP_2)]
        )

    monkeypatch.setattr(fw.dynamic_config, "get", fake_get)


def _patch_whitelist(monkeypatch, *chat_ids: uuid.UUID) -> None:
    """把 Dynamic Config 白名单换成固定的群 id 集合。"""

    def fake_get(key: str, *, default: str = "") -> str:
        return ",".join(str(c) for c in chat_ids)

    monkeypatch.setattr(fw.dynamic_config, "get", fake_get)


async def _seed_conversation(chat_id, *, scope="group", name="测试群"):
    async with session_mod.get_session() as s:
        s.add(
            CommonConversation(
                common_conversation_id=chat_id,
                channel="lark",
                scope=scope,
                display_name=name,
            )
        )


async def _seed_user_message(
    chat_id, *, event_time, text, user_id=_USER_1, username="贝壳",
    scope="group", message_type=None, bot_name=None,
):
    async with session_mod.get_session() as s:
        s.add(
            CommonMessage(
                common_message_id=uuid.uuid4(),
                channel="lark",
                common_conversation_id=chat_id,
                common_user_id=user_id,
                sender_display_name=username,
                role="user",
                content=[{"kind": "text", "text": text}],
                content_text=text,
                scope=scope,
                message_type=message_type,
                bot_name=bot_name,
                event_time=event_time,
            )
        )


async def _seed_bot_message(
    chat_id, *, event_time, text, persona_id, scope="group"
):
    """assistant 消息 + 配套 agent_response（persona 归属经 response join 出来）。"""
    session_id = f"sess-{uuid.uuid4().hex}"
    async with session_mod.get_session() as s:
        s.add(
            CommonAgentResponse(
                response_id=uuid.uuid4(),
                session_id=session_id,
                trigger_common_message_id=uuid.uuid4(),
                common_conversation_id=chat_id,
                persona_id=persona_id,
            )
        )
        s.add(
            CommonMessage(
                common_message_id=uuid.uuid4(),
                channel="lark",
                common_conversation_id=chat_id,
                common_user_id=None,
                sender_display_name=None,
                role="assistant",
                content=[{"kind": "text", "text": text}],
                content_text=text,
                scope=scope,
                response_id=session_id,
                event_time=event_time,
            )
        )


async def _seed_proactive_bot_message(
    chat_id, *, event_time, text_, bot_name, scope="direct"
):
    """proactive 出站 assistant 行真实形态：role=assistant、带 bot_name、
    **response_id=NULL**、**无** CommonAgentResponse 行（worker 落库口径）。"""
    async with session_mod.get_session() as s:
        s.add(
            CommonMessage(
                common_message_id=uuid.uuid4(),
                channel="lark",
                common_conversation_id=chat_id,
                common_user_id=_BOT_USER,
                sender_display_name=None,
                role="assistant",
                content=[{"kind": "text", "text": text_}],
                content_text=text_,
                scope=scope,
                message_type="post",
                bot_name=bot_name,
                response_id=None,
                event_time=event_time,
            )
        )


async def _seed_bot_config(bot_name, persona_id, *, is_active=True):
    async with session_mod.get_session() as s:
        await s.execute(
            text(
                "INSERT INTO bot_config (bot_name, persona_id, is_active) "
                "VALUES (:bn, :pid, :active)"
            ),
            {"bn": bot_name, "pid": persona_id, "active": is_active},
        )


async def _seed_user_attachment_message(
    chat_id, *, event_time, file_key, file_name, item_type="file", tos_file=None,
    user_id=_USER_1, username="贝壳", scope="direct",
):
    """造一条真人发来的**附件**消息（content 里一条 file / media 项）。

    读小说：真人在飞书发个 txt/epub → 文件作为 common_message.content 里一条普通文件项落库
    （和图片同款）。``item_type`` 默认 "file"（真文件，可读）；传 "media" 造视频项（应被
    read_book 候选排除——media/视频不是可读文件）。``tos_file`` 仅为兼容旧测试保留，文件的
    对象存储引用现在由 agent-service 从 file_key **确定性派生**（files/<file_key>），不依赖回填。
    """
    item = {"type": item_type, "value": file_key, "meta": {"file_name": file_name}}
    if tos_file is not None:
        item["tos_file"] = tos_file
    mid = uuid.uuid4()
    async with session_mod.get_session() as s:
        s.add(
            CommonMessage(
                common_message_id=mid,
                channel="lark",
                common_conversation_id=chat_id,
                common_user_id=user_id,
                sender_display_name=username,
                role="user",
                content=[item],
                content_text="",
                scope=scope,
                event_time=event_time,
            )
        )
    return str(mid)


@pytest.mark.integration
async def test_private_chat_returned_with_self_and_other(chat_db, allow_all_groups):
    """她的真人私聊：返回成一个 conversation digest，含真人 user 行 + 她自己的回复。

    私聊放行（不过白名单）。每条消息分清谁说的：她自己的回复 is_self=True、
    真人话 is_self=False；真人话展示名走 sender_display_name 兜底、不暴露 raw user_id。
    """
    await _seed_conversation(_CHAT_DIRECT, scope="direct", name=None)
    await _seed_user_message(
        _CHAT_DIRECT, event_time=1000, text="赤尾在吗", scope="direct",
        username="贝壳",
    )
    await _seed_bot_message(
        _CHAT_DIRECT, event_time=2000, text="在的在的", persona_id="akao",
        scope="direct",
    )

    got = await find_persona_related_chats_recent(
        persona_id="akao", since_ms=0, max_conversations=10, per_chat_limit=50
    )

    assert len(got) == 1
    convo = got[0]
    assert convo.chat_id == str(_CHAT_DIRECT)
    assert convo.scope == "direct"
    assert convo.display_name is None, "私聊没有群名"
    assert len(convo.messages) == 2
    # 按发生先后升序
    user_msg, bot_msg = convo.messages
    assert "赤尾在吗" in user_msg.text
    assert user_msg.is_self is False
    assert user_msg.speaker_display_name == "贝壳"
    assert str(_USER_1) not in user_msg.speaker_display_name, (
        "真人私聊不把 raw user_id 当展示名暴露"
    )
    assert "在的在的" in bot_msg.text
    assert bot_msg.is_self is True, "她自己的回复要标成她自己"
    # CST 时间是个非空展示串（不是裸毫秒数）
    assert bot_msg.cst_time and "CST" in bot_msg.cst_time


@pytest.mark.integration
async def test_whitelisted_group_returned_non_whitelisted_excluded(chat_db, monkeypatch):
    """群必须过白名单：白名单内的群进结果，白名单外的群不出现（私聊不受影响）。"""
    _patch_whitelist(monkeypatch, _CHAT_GROUP)  # 只放行 group-1
    await _seed_conversation(_CHAT_GROUP, scope="group", name="一家人")
    await _seed_conversation(_CHAT_GROUP_2, scope="group", name="同学群")
    # 两个群她都发过言
    await _seed_bot_message(_CHAT_GROUP, event_time=1000, text="家里群我说话了", persona_id="akao")
    await _seed_bot_message(_CHAT_GROUP_2, event_time=1000, text="同学群我也说了", persona_id="akao")

    got = await find_persona_related_chats_recent(
        persona_id="akao", since_ms=0, max_conversations=10, per_chat_limit=50
    )

    chat_ids = {c.chat_id for c in got}
    assert str(_CHAT_GROUP) in chat_ids, "白名单内的群要出现"
    assert str(_CHAT_GROUP_2) not in chat_ids, "白名单外的群不能出现"


@pytest.mark.integration
async def test_passive_presence_chat_excluded(chat_db, allow_all_groups):
    """她在某会话被动在场但没发言 → 不算她相关会话（同 chat 被动唤起边界）。"""
    await _seed_conversation(_CHAT_GROUP, scope="group", name="一家人")
    await _seed_user_message(_CHAT_GROUP, event_time=1000, text="群里热闹但她没说话")

    got = await find_persona_related_chats_recent(
        persona_id="akao", since_ms=0, max_conversations=10, per_chat_limit=50
    )

    assert got == []


@pytest.mark.integration
async def test_recency_window_excludes_old_chats(chat_db, allow_all_groups):
    """only 最近活跃：她只在窗口外发过言的会话不算（since_ms 之前的不拉）。"""
    await _seed_conversation(_CHAT_GROUP, scope="group", name="一家人")
    await _seed_bot_message(_CHAT_GROUP, event_time=100, text="很久以前说的", persona_id="akao")

    got = await find_persona_related_chats_recent(
        persona_id="akao", since_ms=1000, max_conversations=10, per_chat_limit=50
    )

    assert got == [], "她只在 since_ms 之前发过言的会话不算最近活跃"


@pytest.mark.integration
async def test_per_chat_limit_keeps_most_recent_ascending(chat_db, allow_all_groups):
    """每会话条数上限：超限只保最近 N 条（条目数量控制、不字符截断），仍升序。"""
    await _seed_conversation(_CHAT_GROUP, scope="group", name="一家人")
    await _seed_bot_message(_CHAT_GROUP, event_time=1000, text="她说了话", persona_id="akao")
    for i in range(5):
        await _seed_user_message(_CHAT_GROUP, event_time=2000 + i, text=f"第{i}条")

    got = await find_persona_related_chats_recent(
        persona_id="akao", since_ms=0, max_conversations=10, per_chat_limit=3
    )

    assert len(got) == 1
    texts = [m.text for m in got[0].messages]
    assert len(texts) == 3, "超限只保最近 3 条"
    assert "第2" in texts[0] and "第3" in texts[1] and "第4" in texts[2], (
        f"保最近的、按先后升序，实际 {texts}"
    )


@pytest.mark.integration
async def test_max_conversations_caps_number_of_chats(chat_db, allow_all_groups):
    """会话数上限：超出 max_conversations 只保最近活跃的几个会话。"""
    await _seed_conversation(_CHAT_GROUP, scope="group", name="一家人")
    await _seed_conversation(_CHAT_GROUP_2, scope="group", name="同学群")
    await _seed_conversation(_CHAT_DIRECT, scope="direct", name=None)
    # 三个会话她都发过言，按最近活跃排：direct 最新、group2 次之、group 最旧
    await _seed_bot_message(_CHAT_GROUP, event_time=1000, text="最旧", persona_id="akao")
    await _seed_bot_message(_CHAT_GROUP_2, event_time=2000, text="居中", persona_id="akao")
    await _seed_bot_message(_CHAT_DIRECT, event_time=3000, text="最新", persona_id="akao", scope="direct")

    got = await find_persona_related_chats_recent(
        persona_id="akao", since_ms=0, max_conversations=2, per_chat_limit=50
    )

    assert len(got) == 2, "会话数上限=2"
    chat_ids = {c.chat_id for c in got}
    assert str(_CHAT_DIRECT) in chat_ids and str(_CHAT_GROUP_2) in chat_ids, (
        "保最近活跃的两个会话，最旧的 group 被挤出"
    )
    assert str(_CHAT_GROUP) not in chat_ids


@pytest.mark.integration
async def test_other_persona_speech_does_not_make_chat_hers(chat_db, allow_all_groups):
    """同会话里别的 persona 发过言 ≠ 她相关会话（persona 归属按 response join 判）。"""
    await _seed_conversation(_CHAT_GROUP, scope="group", name="一家人")
    await _seed_bot_message(_CHAT_GROUP, event_time=2000, text="我在", persona_id="chinagi")

    got = await find_persona_related_chats_recent(
        persona_id="akao", since_ms=0, max_conversations=10, per_chat_limit=50
    )

    assert got == []


@pytest.mark.integration
async def test_proactive_only_private_chat_qualifies(chat_db, monkeypatch):
    """她在私聊里只发过 proactive（response_id=NULL、无 agent_response、bot_name 指向
    她的 active bot_config）也算她相关会话，且该行 is_self=True（经 bot_config 兜底归属）。
    """
    # 私聊放行（不读群白名单）：白名单 patch 成空，顺带验证 p2p 短路不被空白名单挡。
    _patch_whitelist(monkeypatch)

    await _seed_conversation(_CHAT_DIRECT, scope="direct", name=None)
    await _seed_bot_config("chiwei", "akao")
    await _seed_user_message(_CHAT_DIRECT, event_time=1000, text="在吗", scope="direct")
    await _seed_proactive_bot_message(
        _CHAT_DIRECT, event_time=2000, text_="我刚在想你", bot_name="chiwei"
    )

    got = await find_persona_related_chats_recent(
        persona_id="akao", since_ms=0, max_conversations=10, per_chat_limit=50
    )

    assert len(got) == 1, "她只发过 proactive 的私聊也算她相关会话"
    by_text = {m.text: m for m in got[0].messages}
    proactive = next(m for t, m in by_text.items() if "我刚在想你" in t)
    assert proactive.is_self is True, "proactive 出站行经 bot_config 归属到她、标 is_self"
    user_msg = next(m for t, m in by_text.items() if "在吗" in t)
    assert user_msg.is_self is False


@pytest.mark.integration
async def test_user_row_with_bot_name_not_marked_self(chat_db, allow_all_groups):
    """承重红线：真人 user 行即便带 bot_name 也不能被兜底成 persona、不能标 is_self。"""
    await _seed_conversation(_CHAT_GROUP, scope="group", name="一家人")
    await _seed_bot_config("chiwei", "akao")
    await _seed_bot_message(_CHAT_GROUP, event_time=1000, text="她说话了", persona_id="akao")
    await _seed_user_message(
        _CHAT_GROUP, event_time=2000, text="真人带 bot_name 的话", bot_name="chiwei",
    )

    got = await find_persona_related_chats_recent(
        persona_id="akao", since_ms=0, max_conversations=10, per_chat_limit=50
    )

    assert len(got) == 1
    by_text = {m.text: m for m in got[0].messages}
    user_msg = next(m for t, m in by_text.items() if "真人带 bot_name 的话" in t)
    assert user_msg.is_self is False, (
        "真人 user 行带 bot_name 也不能被认成她自己"
    )


# ---------------------------------------------------------------------------
# 文件候选（读小说 Task 2）：同一条边界里把可读文件项也带出来（read_book 用它在她
# 可见上下文里按名字认文件）。candidates 来自**同一批已取出的消息 rows**——零额外查询、
# 真同一边界（codex 必改 1：不在 read_book 时重跑 recent 查询，避免边界漂移）。
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_file_candidates_from_same_boundary(chat_db, allow_all_groups):
    """她相关会话里真人发的文件项 → 作为 file_candidates 带出（同一边界、零额外查询）。

    tos 引用由 file_key **确定性派生**（files/<file_key>）—— 不依赖那条对文件根本不跑的
    image-only 回填（codex T3 ①）。
    """
    await _seed_conversation(_CHAT_DIRECT, scope="direct", name=None)
    # 她在这个私聊发过言（才算她相关会话）
    await _seed_bot_message(
        _CHAT_DIRECT, event_time=2000, text="读到了", persona_id="akao", scope="direct"
    )
    mid = await _seed_user_attachment_message(
        _CHAT_DIRECT, event_time=1000, file_key="fk-1", file_name="斜阳.txt",
        scope="direct",
    )

    got = await find_persona_related_chats_recent(
        persona_id="akao", since_ms=0, max_conversations=10, per_chat_limit=50
    )
    assert len(got) == 1
    cands = got[0].file_candidates
    assert len(cands) == 1
    f = cands[0]
    assert f.file_name == "斜阳.txt"
    assert f.tos_file == "files/fk-1", "tos 引用从 file_key 确定性派生，不依赖回填"
    # 附件实例身份 = 收到该文件那次（common_message_id + file_key）
    assert f.attachment_id == f"{mid}:fk-1"


@pytest.mark.integration
async def test_file_candidate_tos_ref_derived_without_backfill(chat_db, allow_all_groups):
    """codex T3 ①：文件项即便没回填过 tos_file，候选的 tos 引用也由 file_key 派生（非空）。

    回填机制（persist_tos_files_node）是 image-only、对文件根本不跑，所以文件项 content 里
    永远没有 tos_file。但 TOS 命名是确定性的 files/<file_key>（Task 1 tool-service file-pipeline
    的存储名），agent-service 直接派生 → read_book 能开读，不会永远卡在"还没准备好"。
    """
    await _seed_conversation(_CHAT_DIRECT, scope="direct", name=None)
    await _seed_bot_message(
        _CHAT_DIRECT, event_time=2000, text="嗯", persona_id="akao", scope="direct"
    )
    # 不传 tos_file（模拟真实：文件项从来没被回填过）
    await _seed_user_attachment_message(
        _CHAT_DIRECT, event_time=1000, file_key="fk-never-backfilled",
        file_name="人间失格.epub", tos_file=None, scope="direct",
    )

    got = await find_persona_related_chats_recent(
        persona_id="akao", since_ms=0, max_conversations=10, per_chat_limit=50
    )
    cands = got[0].file_candidates
    assert len(cands) == 1
    assert cands[0].file_name == "人间失格.epub"
    assert cands[0].tos_file == "files/fk-never-backfilled", (
        "没回填也由 file_key 派生出 tos 引用（read_book 据此能开读，不永远卡住）"
    )


@pytest.mark.integration
async def test_media_video_excluded_from_file_candidates(chat_db, allow_all_groups):
    """codex T3 ④：飞书 media（视频）不混进可读文件候选（只留真 file 项）。"""
    await _seed_conversation(_CHAT_DIRECT, scope="direct", name=None)
    await _seed_bot_message(
        _CHAT_DIRECT, event_time=2000, text="嗯", persona_id="akao", scope="direct"
    )
    # 一条视频（media）+ 一条真文件（file）
    await _seed_user_attachment_message(
        _CHAT_DIRECT, event_time=1000, file_key="vk-1", file_name="家庭录像.mp4",
        item_type="media", scope="direct",
    )
    await _seed_user_attachment_message(
        _CHAT_DIRECT, event_time=1500, file_key="fk-real", file_name="斜阳.txt",
        item_type="file", scope="direct",
    )

    got = await find_persona_related_chats_recent(
        persona_id="akao", since_ms=0, max_conversations=10, per_chat_limit=50
    )
    cands = got[0].file_candidates
    names = {c.file_name for c in cands}
    assert names == {"斜阳.txt"}, "视频(media)排除、只留真文件"


@pytest.mark.integration
async def test_no_file_messages_means_empty_candidates(chat_db, allow_all_groups):
    """纯文字会话 → file_candidates 为空（不影响消息渲染）。"""
    await _seed_conversation(_CHAT_DIRECT, scope="direct", name=None)
    await _seed_bot_message(
        _CHAT_DIRECT, event_time=2000, text="在的", persona_id="akao", scope="direct"
    )
    got = await find_persona_related_chats_recent(
        persona_id="akao", since_ms=0, max_conversations=10, per_chat_limit=50
    )
    assert got[0].file_candidates == []


# ---------------------------------------------------------------------------
# 私聊对方真人身份（主动私聊具名化 Task 1）：私聊会话聚合出「对面是谁」
# （common_user_id + 展示名），挂到 LifeChatConversation.counterparts 上，让渲染层
# 能把私聊段具名成「和 田申（user:<uuid>）的私聊里」。口径（spec 决策 2 / 3）：
# - 锚定 role='user' 行取对方（真人行也写 bot_name、proactive 出站 assistant 行的
#   common_user_id 是 bot 自己的——都不能当锚）。
# - 身份按会话**全历史**解析、与 since 窗口解耦：对方最后发言早于窗口、窗口内只剩
#   她自己独白时也要能具名；仅全历史无真人行才为空（匿名兜底）。
# - 对选中会话批量一次查，不逐会话 N+1。
# ---------------------------------------------------------------------------

_CHAT_DIRECT_2 = uuid.uuid5(uuid.NAMESPACE_OID, "rel-chat-direct-2")
_CHAT_DIRECT_3 = uuid.uuid5(uuid.NAMESPACE_OID, "rel-chat-direct-3")


@pytest.mark.integration
async def test_direct_counterpart_named_with_id(chat_db, allow_all_groups):
    """正常私聊：对方恰好 1 个，id / 展示名都对（渠道约定 p2p 一对一）。"""
    await _seed_conversation(_CHAT_DIRECT, scope="direct", name=None)
    await _seed_user_message(
        _CHAT_DIRECT, event_time=1000, text="赤尾在吗", scope="direct",
        user_id=_USER_1, username="田申",
    )
    await _seed_bot_message(
        _CHAT_DIRECT, event_time=2000, text="在的", persona_id="akao", scope="direct"
    )

    got = await find_persona_related_chats_recent(
        persona_id="akao", since_ms=0, max_conversations=10, per_chat_limit=50
    )

    assert len(got) == 1
    cps = got[0].counterparts
    assert len(cps) == 1, "正常 p2p 私聊对方恰好 1 个"
    assert cps[0].user_id == str(_USER_1)
    assert cps[0].display_name == "田申"


@pytest.mark.integration
async def test_counterpart_resolved_from_full_history_outside_window(
    chat_db, allow_all_groups
):
    """对方最后发言早于 since 窗口（窗口内只剩她自己独白）也要能具名（spec 决策 3）。

    身份是会话的稳定事实、与内容增量窗口解耦——正是"她刚主动发过话、对方还没回"
    这种最需要知道对面是谁的场景。
    """
    await _seed_conversation(_CHAT_DIRECT, scope="direct", name=None)
    # 对方在窗口前说过话；窗口内只有她自己的消息
    await _seed_user_message(
        _CHAT_DIRECT, event_time=100, text="早安", scope="direct",
        user_id=_USER_1, username="田申",
    )
    await _seed_bot_message(
        _CHAT_DIRECT, event_time=2000, text="在想你", persona_id="akao", scope="direct"
    )

    got = await find_persona_related_chats_recent(
        persona_id="akao", since_ms=1000, max_conversations=10, per_chat_limit=50
    )

    assert len(got) == 1
    convo = got[0]
    assert [m.is_self for m in convo.messages] == [True], "窗口内只剩她自己的独白"
    assert len(convo.counterparts) == 1, "对方身份按全历史解析、不受窗口影响"
    assert convo.counterparts[0].user_id == str(_USER_1)
    assert convo.counterparts[0].display_name == "田申"


@pytest.mark.integration
async def test_proactive_outbound_row_not_mistaken_as_counterpart(chat_db, monkeypatch):
    """proactive 出站 assistant 行的 common_user_id 是 bot 自己的（非 NULL）——
    绝不能被误认成对方；对方只按 role='user' 行锚定。"""
    _patch_whitelist(monkeypatch)
    await _seed_conversation(_CHAT_DIRECT, scope="direct", name=None)
    await _seed_bot_config("chiwei", "akao")
    await _seed_user_message(
        _CHAT_DIRECT, event_time=1000, text="在吗", scope="direct",
        user_id=_USER_1, username="田申",
    )
    await _seed_proactive_bot_message(
        _CHAT_DIRECT, event_time=2000, text_="我刚在想你", bot_name="chiwei"
    )

    got = await find_persona_related_chats_recent(
        persona_id="akao", since_ms=0, max_conversations=10, per_chat_limit=50
    )

    assert len(got) == 1
    cps = got[0].counterparts
    assert [c.user_id for c in cps] == [str(_USER_1)], (
        "proactive 出站行（bot 自己的 common_user_id）不进对方列表"
    )


@pytest.mark.integration
async def test_no_human_row_in_full_history_means_no_counterpart(chat_db, monkeypatch):
    """全历史都没有真人 user 行（她只发过 proactive、对方从没回）→ 对象为空，
    渲染层保持匿名兜底。"""
    _patch_whitelist(monkeypatch)
    await _seed_conversation(_CHAT_DIRECT, scope="direct", name=None)
    await _seed_bot_config("chiwei", "akao")
    await _seed_proactive_bot_message(
        _CHAT_DIRECT, event_time=2000, text_="我刚在想你", bot_name="chiwei"
    )

    got = await find_persona_related_chats_recent(
        persona_id="akao", since_ms=0, max_conversations=10, per_chat_limit=50
    )

    assert len(got) == 1
    assert got[0].counterparts == []


@pytest.mark.integration
async def test_group_conversation_counterparts_empty(chat_db, allow_all_groups):
    """群会话不聚合对象：counterparts 恒为空（具名化只针对私聊段）。"""
    await _seed_conversation(_CHAT_GROUP, scope="group", name="一家人")
    await _seed_bot_message(_CHAT_GROUP, event_time=1000, text="我在", persona_id="akao")
    await _seed_user_message(_CHAT_GROUP, event_time=2000, text="哈喽")

    got = await find_persona_related_chats_recent(
        persona_id="akao", since_ms=0, max_conversations=10, per_chat_limit=50
    )

    assert len(got) == 1
    assert got[0].counterparts == []


@pytest.mark.integration
async def test_dirty_direct_chat_lists_all_humans(chat_db, allow_all_groups):
    """约定外脏数据：一个 direct 会话里出现 >1 个真人 → 如实全列（忠实呈现，
    不替她挑"主对象"），最近发言的在前（稳定输出）。"""
    await _seed_conversation(_CHAT_DIRECT, scope="direct", name=None)
    await _seed_user_message(
        _CHAT_DIRECT, event_time=1000, text="我先说", scope="direct",
        user_id=_USER_1, username="田申",
    )
    await _seed_user_message(
        _CHAT_DIRECT, event_time=2000, text="我后说", scope="direct",
        user_id=_USER_2, username="原智鸿",
    )
    await _seed_bot_message(
        _CHAT_DIRECT, event_time=3000, text="都在啊", persona_id="akao", scope="direct"
    )

    got = await find_persona_related_chats_recent(
        persona_id="akao", since_ms=0, max_conversations=10, per_chat_limit=50
    )

    cps = got[0].counterparts
    assert [(c.user_id, c.display_name) for c in cps] == [
        (str(_USER_2), "原智鸿"),
        (str(_USER_1), "田申"),
    ], "多真人如实全列、最近发言的在前"


@pytest.mark.integration
async def test_counterpart_display_name_prefers_named_row(chat_db, allow_all_groups):
    """展示名取该真人**有名字**的最近一行：最新一行 sender_display_name 为空时
    不把名字弄丢；全部行都没名字才落展示名兜底（不暴露 raw user_id、不拼 None）。"""
    await _seed_conversation(_CHAT_DIRECT, scope="direct", name=None)
    await _seed_user_message(
        _CHAT_DIRECT, event_time=1000, text="有名字的行", scope="direct",
        user_id=_USER_1, username="田申",
    )
    await _seed_user_message(
        _CHAT_DIRECT, event_time=2000, text="没名字的行", scope="direct",
        user_id=_USER_1, username=None,
    )
    # 另一个真人：全历史都没写过展示名
    await _seed_user_message(
        _CHAT_DIRECT, event_time=1500, text="无名氏", scope="direct",
        user_id=_USER_2, username=None,
    )
    await _seed_bot_message(
        _CHAT_DIRECT, event_time=3000, text="嗯", persona_id="akao", scope="direct"
    )

    got = await find_persona_related_chats_recent(
        persona_id="akao", since_ms=0, max_conversations=10, per_chat_limit=50
    )

    by_id = {c.user_id: c for c in got[0].counterparts}
    assert by_id[str(_USER_1)].display_name == "田申", "有名字的行优先，不被空名行盖掉"
    no_name = by_id[str(_USER_2)]
    assert no_name.display_name, "全无名也给非空兜底展示名"
    assert str(_USER_2) not in no_name.display_name, "兜底不暴露 raw user_id"
    assert "None" not in no_name.display_name


@pytest.mark.integration
async def test_counterpart_aggregation_batched_not_per_chat(chat_db, allow_all_groups):
    """身份聚合对选中会话批量一次查：**身份聚合的查询次数不随会话数增长**
    （spec Task 1 验收）。只约束这一个不变式——消息查询自身的次数走向（比如
    未来也批量化）不归这个测试管，不钉总查询量常数（codex T3 建议）。"""
    from sqlalchemy import event as sa_event

    captured: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            captured.append(statement)

    async def _identity_query_count() -> int:
        """跑一次查询，数可归类为**身份聚合**的 SELECT 条数。

        归类特征：这条查询路径里只有身份聚合用 ``DISTINCT ON``（每 (会话, 真人)
        取最近有名行）——候选查询走 GROUP BY、消息查询是普通 SELECT，都不含。
        """
        captured.clear()
        sa_event.listen(chat_db.sync_engine, "before_cursor_execute", _capture)
        try:
            await find_persona_related_chats_recent(
                persona_id="akao", since_ms=0, max_conversations=10, per_chat_limit=50
            )
        finally:
            sa_event.remove(chat_db.sync_engine, "before_cursor_execute", _capture)
        return sum("DISTINCT ON" in stmt for stmt in captured)

    async def _seed_direct(chat_id, *, t):
        await _seed_conversation(chat_id, scope="direct", name=None)
        await _seed_user_message(
            chat_id, event_time=t, text="hi", scope="direct", username="田申"
        )
        await _seed_bot_message(
            chat_id, event_time=t + 1, text="在", persona_id="akao", scope="direct"
        )

    await _seed_direct(_CHAT_DIRECT, t=1000)
    n_with_1 = await _identity_query_count()

    await _seed_direct(_CHAT_DIRECT_2, t=2000)
    await _seed_direct(_CHAT_DIRECT_3, t=3000)
    n_with_3 = await _identity_query_count()

    assert n_with_1 == 1, (
        "归类器必须真逮到那条身份聚合查询（不许空转绿）；"
        f"实际 1 会话时归类到 {n_with_1} 条"
    )
    assert n_with_3 == 1, (
        "3 个会话仍只有 1 条身份聚合查询——身份聚合不随会话数 N+1；"
        f"实际 3 会话时归类到 {n_with_3} 条"
    )


@pytest.mark.integration
async def test_proactive_trigger_pseudo_messages_excluded(chat_db, allow_all_groups):
    """proactive_trigger 伪消息不进结果（它是触发器记录、不是真实对话）。"""
    await _seed_conversation(_CHAT_GROUP, scope="group", name="一家人")
    await _seed_bot_message(_CHAT_GROUP, event_time=1000, text="她说了话", persona_id="akao")
    await _seed_user_message(
        _CHAT_GROUP, event_time=2000, text="proactive 伪消息",
        message_type="proactive_trigger",
    )

    got = await find_persona_related_chats_recent(
        persona_id="akao", since_ms=0, max_conversations=10, per_chat_limit=50
    )

    texts = [m.text for m in got[0].messages]
    assert not any("伪消息" in t for t in texts)
