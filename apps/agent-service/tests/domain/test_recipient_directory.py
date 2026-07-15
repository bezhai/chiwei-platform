"""可发送对象目录（recipient_directory）— 名字模糊查候选 + uid 解析投递目标.

这一层只做「解析」：把名字 / typed uid 翻成「该怎么把消息送到 ta」。它**不发送**
（发送是 task 3 的事），只回答两个问题：

  * ``search_recipients(query)`` —— 按名字模糊查到候选（typed uid + 一段简介帮认人 /
    区分重名）。**只返回候选，绝不排序、不按亲密度 / 活跃度 / 兴趣筛、不自动取第一个**
    （赤尾设计宪法：选谁是 life 自己的决定，代码不替她决定跟谁说话）。
  * ``resolve_delivery(uid)`` —— 把一个 typed uid 解析成投递目标：
      - ``persona:<id>`` → 信箱投递目标（对接 mailbox.deliver_event）；
      - ``user:<common_user_id>`` → 飞书私聊投递目标（已有 p2p 会话地址 + 发送 bot）；
      - 查不到 / 不可投递 → ``UndeliverableRecipient`` fail-loud（明确报「发不了 + 原因」，
        绝不返回伪地址、不静默降级）。

正确性全在 SQL 语义（模糊匹配 / typed uid 解析 / 有无 p2p 会话判定），所以走真
Postgres 集成测试（``test_db`` fixture，对齐 test_persona_spoken_chats.py 风格）。

真人投递地址来源（查实结论，见模块 docstring）：agent-service 只有 common_* 表，
``common_user`` **没有 open_id**——飞书 open_id 活在 channel-server 私有映射表里、
这边查不到。所以真人投递**只能发已有 p2p 会话的真人**：``common_conversation``
（scope='direct'）+ 会话里的 ``bot_name`` 就是可投递目标，worker 反查会话私有映射
拿到飞书裸地址。没 p2p 会话的真人 = 不可投递（fail-loud），不能凭 open_id 主动
发起新私聊（这边根本拿不到 open_id）。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

import app.data.session as session_mod
import app.domain.recipient_directory as rd
from app.data.models import (
    Base,
    BotPersona,
    CommonAgentResponse,
    CommonConversation,
    CommonMessage,
    CommonUser,
)
from app.domain.recipient_directory import (
    GroupTarget,
    LarkP2PTarget,
    MailboxTarget,
    UndeliverableRecipient,
    group_uid,
    persona_uid,
    resolve_delivery,
    search_recipients,
    user_uid,
)

_BEZHAI = uuid.uuid5(uuid.NAMESPACE_OID, "user-bezhai")
_OTHER = uuid.uuid5(uuid.NAMESPACE_OID, "user-other")
_STRANGER = uuid.uuid5(uuid.NAMESPACE_OID, "user-stranger-no-p2p")

_BEZHAI_P2P = uuid.uuid5(uuid.NAMESPACE_OID, "conv-bezhai-p2p")
_OTHER_P2P = uuid.uuid5(uuid.NAMESPACE_OID, "conv-other-p2p")
_SOME_GROUP = uuid.uuid5(uuid.NAMESPACE_OID, "conv-group")


# bot_config 由 channel-server 管理、不在 agent-service 的 SQLAlchemy 模型里。群里
# 解析「赤尾该 persona 的 active bot_name」走 COALESCE(common_agent_response.persona_id,
# bot_config.persona_id)（与 find_recent_chat_messages 同口径）；集成测试要手动建这张
# 裸表，让 COALESCE 的 bot_config 兜底分支能解析。
_BOT_CONFIG_DDL = (
    "CREATE TABLE bot_config ("
    "  bot_name VARCHAR(50) PRIMARY KEY,"
    "  persona_id VARCHAR(50),"
    "  is_active BOOLEAN NOT NULL DEFAULT TRUE"
    ")"
)

# common_bot_presence 同样由 channel-server 管理（裸表，无 SQLAlchemy 模型）。群投递
# 解析出的 bot 必须**当前还在这个群且 active**——口径对齐 persona.py 的
# resolve_bot_name_for_persona（JOIN common_bot_presence bp ON bc.bot_name = bp.bot_name
# WHERE bp.common_conversation_id = :cid AND bp.is_active = true）。bot 被移出群后历史
# 回复还在、但 presence.is_active 翻 false，解析必须 fail-loud（不投递）。复合主键
# (common_conversation_id, bot_name) 与 channel-server 实体一致。
_BOT_PRESENCE_DDL = (
    "CREATE TABLE common_bot_presence ("
    "  common_conversation_id UUID NOT NULL,"
    "  bot_name VARCHAR(50) NOT NULL,"
    "  is_active BOOLEAN NOT NULL DEFAULT TRUE,"
    "  PRIMARY KEY (common_conversation_id, bot_name)"
    ")"
)


@pytest.fixture
async def directory_db(test_db):
    """建 recipient_directory 查询要 join 的表（bot_persona + common_* + bot_config
    + common_bot_presence）。"""
    tables = [
        BotPersona.__table__,
        CommonUser.__table__,
        CommonConversation.__table__,
        CommonMessage.__table__,
        CommonAgentResponse.__table__,
    ]
    async with test_db.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables)
        )
        await conn.execute(text(_BOT_CONFIG_DDL))
        await conn.execute(text(_BOT_PRESENCE_DDL))
    yield test_db


def _patch_whitelist(monkeypatch, value: str) -> None:
    """把 life 感知白名单的 Dynamic Config 读取换成固定串（同 should_feed_chat_to_life
    的来源），让群解析 / 群模糊查的白名单闸在测试里可控。"""
    from app.life import feed_whitelist as fw

    def fake_get(key: str, *, default: str = "") -> str:
        return value if key == fw.LIFE_FEED_CHAT_WHITELIST_KEY else default

    monkeypatch.setattr(fw.dynamic_config, "get", fake_get)


async def _seed_bot_presence(conv_id, bot_name, *, is_active=True):
    """造一条 common_bot_presence：bot 在这个群、active 与否可控（presence 闸的输入）。"""
    async with session_mod.get_session() as s:
        await s.execute(
            text(
                "INSERT INTO common_bot_presence "
                "(common_conversation_id, bot_name, is_active) "
                "VALUES (CAST(:cid AS uuid), :bn, :active)"
            ),
            {"cid": str(conv_id), "bn": bot_name, "active": is_active},
        )


async def _seed_bot_config(bot_name, persona_id, *, is_active=True):
    """造一条 bot_config（bot_name → persona_id 映射）：proactive-only 群无 agent_response
    行时，群解析的 persona 归属靠这张表兜底（COALESCE 第二支）。"""
    async with session_mod.get_session() as s:
        await s.execute(
            text(
                "INSERT INTO bot_config (bot_name, persona_id, is_active) "
                "VALUES (:bn, :pid, :active)"
            ),
            {"bn": bot_name, "pid": persona_id, "active": is_active},
        )


async def _seed_group_assistant_response(
    conv_id, *, persona_id, bot_name, event_time, seed_presence=True
):
    """造一条群里赤尾该 persona 的 active 回复：assistant 消息 + common_agent_response
    行（persona_id + bot_name），让群解析能拿到「该 persona 在这个群用哪个 bot」。

    默认顺手播一条 common_bot_presence（active）——发过言的 bot 正常就在群里。要测
    presence 闸（bot 已被移出群）时传 ``seed_presence=False`` 单独 seed inactive presence。
    """
    session_id = f"sess-{uuid.uuid4()}"
    async with session_mod.get_session() as s:
        s.add(
            CommonMessage(
                common_message_id=uuid.uuid4(),
                channel="lark",
                common_conversation_id=conv_id,
                common_user_id=None,
                sender_display_name=None,
                role="assistant",
                content=[{"kind": "text", "text": "hi"}],
                content_text="hi",
                scope="group",
                bot_name=bot_name,
                response_id=session_id,
                event_time=event_time,
            )
        )
        s.add(
            CommonAgentResponse(
                response_id=uuid.uuid4(),
                session_id=session_id,
                trigger_common_message_id=uuid.uuid4(),
                common_conversation_id=conv_id,
                bot_name=bot_name,
                persona_id=persona_id,
            )
        )
    if seed_presence:
        await _seed_bot_presence(conv_id, bot_name, is_active=True)


async def _seed_persona(persona_id, display_name, lite):
    async with session_mod.get_session() as s:
        s.add(
            BotPersona(
                persona_id=persona_id,
                display_name=display_name,
                persona_core="core",
                persona_lite=lite,
                default_reply_style="style",
                error_messages={},
            )
        )


async def _seed_user(user_id, display_name, *, channel="lark"):
    async with session_mod.get_session() as s:
        s.add(
            CommonUser(
                common_user_id=user_id,
                channel=channel,
                display_name=display_name,
            )
        )


async def _seed_conversation(conv_id, *, scope, name=None, channel="lark"):
    async with session_mod.get_session() as s:
        s.add(
            CommonConversation(
                common_conversation_id=conv_id,
                channel=channel,
                scope=scope,
                display_name=name,
            )
        )


async def _seed_message(
    conv_id, *, user_id, bot_name, scope, event_time, role="user", channel="lark"
):
    async with session_mod.get_session() as s:
        s.add(
            CommonMessage(
                common_message_id=uuid.uuid4(),
                channel=channel,
                common_conversation_id=conv_id,
                common_user_id=user_id,
                sender_display_name=None,
                role=role,
                content=[{"kind": "text", "text": "hi"}],
                content_text="hi",
                scope=scope,
                bot_name=bot_name,
                event_time=event_time,
            )
        )


async def _seed_three_sisters():
    await _seed_persona("akao", "赤尾", "你是赤尾（小尾），18 岁，高三。")
    await _seed_persona("chinagi", "千凪", "你是千凪，24 岁，三姐妹大姐。")
    await _seed_persona("ayana", "绫奈", "你是绫奈，14 岁，初中生。")


async def _seed_bezhai_with_p2p():
    """bezhai（原智鸿）有一条与 chiwei bot（persona akao）的 p2p 会话。"""
    await _seed_user(_BEZHAI, "原智鸿")
    await _seed_conversation(_BEZHAI_P2P, scope="direct", name="原智鸿")
    await _seed_message(
        _BEZHAI_P2P,
        user_id=_BEZHAI,
        bot_name="chiwei",
        scope="direct",
        event_time=1000,
    )
    await _seed_bot_config("chiwei", "akao", is_active=True)


# ---------------------------------------------------------------------------
# typed uid helpers
# ---------------------------------------------------------------------------


def test_persona_uid_format():
    assert persona_uid("akao") == "persona:akao"


def test_user_uid_format():
    assert user_uid(_BEZHAI) == f"user:{_BEZHAI}"


# ---------------------------------------------------------------------------
# search_recipients — 模糊名字返候选 + 简介，不排序不取第一个
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_search_sister_by_name_returns_uid_and_intro(directory_db):
    await _seed_three_sisters()

    got = await search_recipients("千凪")

    assert len(got) == 1
    cand = got[0]
    assert cand.uid == "persona:chinagi"
    assert "千凪" in cand.intro
    assert cand.display_name == "千凪"


@pytest.mark.integration
async def test_search_real_person_by_name_returns_user_uid_and_intro(directory_db):
    await _seed_bezhai_with_p2p()

    got = await search_recipients("原智鸿")

    assert len(got) == 1
    cand = got[0]
    assert cand.uid == f"user:{_BEZHAI}"
    assert "原智鸿" in cand.intro


@pytest.mark.integration
async def test_search_partial_name_matches_substring(directory_db):
    await _seed_three_sisters()
    await _seed_bezhai_with_p2p()

    got = await search_recipients("智鸿")

    uids = {c.uid for c in got}
    assert f"user:{_BEZHAI}" in uids


@pytest.mark.integration
async def test_search_returns_all_candidates_for_ambiguous_name(directory_db):
    """重名：两个都叫「小尾」 → 两个候选都返回，绝不替她取第一个。"""
    await _seed_persona("akao", "小尾", "你是赤尾（小尾），18 岁。")
    await _seed_user(_OTHER, "小尾")
    await _seed_conversation(_OTHER_P2P, scope="direct", name="小尾")
    await _seed_message(
        _OTHER_P2P, user_id=_OTHER, bot_name="chiwei", scope="direct", event_time=1
    )

    got = await search_recipients("小尾")

    uids = {c.uid for c in got}
    assert "persona:akao" in uids
    assert f"user:{_OTHER}" in uids
    assert len(got) == 2, "重名两个都返回，不筛、不取第一个"


@pytest.mark.integration
async def test_search_no_match_returns_empty(directory_db):
    await _seed_three_sisters()

    got = await search_recipients("查无此人")

    assert got == []


@pytest.mark.integration
async def test_search_does_not_rank_by_activity_or_intimacy(directory_db):
    """多个候选时顺序是稳定的纯机制序（姐妹按 persona_id 升序），不按亲密度 / 活跃度排。

    三人显示名都带共同后缀「同学」让一个 query 命中全部，验证返回顺序严格按
    persona_id 升序（akao < ayana < chinagi），与播种 / 插入顺序、活跃度无关。
    """
    await _seed_persona("chinagi", "千凪同学", "大姐。")
    await _seed_persona("akao", "赤尾同学", "二姐。")
    await _seed_persona("ayana", "绫奈同学", "小妹。")

    got = await search_recipients("同学")

    ids = [c.uid for c in got if c.uid.startswith("persona:")]
    assert ids == ["persona:akao", "persona:ayana", "persona:chinagi"], (
        f"同类候选按 persona_id 稳定升序，不按播种序 / 亲密度 / 活跃度，实际 {ids}"
    )


# ---------------------------------------------------------------------------
# resolve_delivery — persona uid → 信箱目标
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_resolve_persona_uid_to_mailbox_target(directory_db):
    await _seed_three_sisters()

    target = await resolve_delivery("persona:chinagi")

    assert isinstance(target, MailboxTarget)
    assert target.persona_id == "chinagi"


@pytest.mark.integration
async def test_resolve_unknown_persona_uid_fails_loud(directory_db):
    await _seed_three_sisters()

    with pytest.raises(UndeliverableRecipient) as exc:
        await resolve_delivery("persona:nobody")
    assert "nobody" in str(exc.value)


# ---------------------------------------------------------------------------
# resolve_delivery — user uid → 飞书私聊目标
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_resolve_user_uid_with_p2p_to_lark_target(directory_db):
    await _seed_bezhai_with_p2p()

    target = await resolve_delivery(f"user:{_BEZHAI}", persona_id="akao")

    assert isinstance(target, LarkP2PTarget)
    assert target.common_conversation_id == str(_BEZHAI_P2P)
    assert target.bot_name == "chiwei"
    assert target.channel == "lark"


@pytest.mark.integration
async def test_resolve_user_uid_no_p2p_fails_loud(directory_db):
    """真人存在、但从没 p2p 私聊过 → 不可投递（fail-loud），绝不返回伪地址。"""
    await _seed_user(_STRANGER, "陌生人")
    # 只在群里见过（group scope），没有 direct 会话。
    await _seed_conversation(_SOME_GROUP, scope="group", name="某群")
    await _seed_message(
        _SOME_GROUP,
        user_id=_STRANGER,
        bot_name="chiwei",
        scope="group",
        event_time=1,
    )

    with pytest.raises(UndeliverableRecipient) as exc:
        await resolve_delivery(f"user:{_STRANGER}", persona_id="akao")
    msg = str(exc.value)
    assert "私聊" in msg or "p2p" in msg.lower()


@pytest.mark.integration
async def test_resolve_unknown_user_uid_fails_loud(directory_db):
    await _seed_bezhai_with_p2p()

    with pytest.raises(UndeliverableRecipient):
        await resolve_delivery(f"user:{uuid.uuid4()}", persona_id="akao")


@pytest.mark.integration
async def test_resolve_user_uid_non_lark_direct_fails_loud(directory_db):
    """真人有 direct 会话、但渠道不是 lark → 不可投递（这一刀只接飞书，codex 必改 1）。

    可投递判定不能只看「有没有 direct 会话」，还得限定 ``channel='lark'`` —— 非 lark
    渠道的 direct 会话这边没有出站送达路径（chat-response-worker 的主动发只走飞书），
    把它当可投递会 emit 一条永远送不出去的出站段。fail-loud。
    """
    user = uuid.uuid5(uuid.NAMESPACE_OID, "user-non-lark")
    conv = uuid.uuid5(uuid.NAMESPACE_OID, "conv-non-lark-direct")
    await _seed_user(user, "非飞书的人", channel="wecom")
    # direct（私聊）会话，但渠道是非 lark。
    await _seed_conversation(conv, scope="direct", name="非飞书私聊", channel="wecom")
    await _seed_message(
        conv, user_id=user, bot_name="chiwei", scope="direct", event_time=1, channel="wecom"
    )
    await _seed_bot_config("chiwei", "akao", is_active=True)

    with pytest.raises(UndeliverableRecipient) as exc:
        await resolve_delivery(f"user:{user}", persona_id="akao")
    msg = str(exc.value)
    assert "私聊" in msg or "p2p" in msg.lower()


@pytest.mark.integration
async def test_resolve_user_uid_picks_persona_specific_bot(directory_db):
    """真人同时跟不止一个姐妹的 bot 私聊过 → 解析出**当前发送 persona 自己**的私聊线，
    不管哪条线最近活跃过（这是本次要修的 bug：之前只取全局最近一条，跟发送者是谁无关，
    会把千凪的话用赤尾的 bot 发出去，人设串味）。

    这里 chinagi 的私聊线更晚活跃（event_time 更大），但调用方是 akao → 必须解析出
    akao 自己那条更早的私聊线 + chiwei bot，绝不因为 chinagi 的线最近就选错。
    """
    conv_chinagi = uuid.uuid5(uuid.NAMESPACE_OID, "conv-bezhai-chinagi-p2p")
    await _seed_user(_BEZHAI, "原智鸿")
    await _seed_conversation(_BEZHAI_P2P, scope="direct", name="原智鸿-akao线")
    await _seed_message(
        _BEZHAI_P2P, user_id=_BEZHAI, bot_name="chiwei", scope="direct", event_time=1000,
    )
    await _seed_bot_config("chiwei", "akao", is_active=True)

    await _seed_conversation(conv_chinagi, scope="direct", name="原智鸿-chinagi线")
    await _seed_message(
        conv_chinagi, user_id=_BEZHAI, bot_name="chinagi-bot", scope="direct", event_time=2000,
    )
    await _seed_bot_config("chinagi-bot", "chinagi", is_active=True)

    target = await resolve_delivery(f"user:{_BEZHAI}", persona_id="akao")

    assert isinstance(target, LarkP2PTarget)
    assert target.bot_name == "chiwei", "解析出 akao 自己的 bot，不串到 chinagi 的"
    assert target.common_conversation_id == str(_BEZHAI_P2P)


@pytest.mark.integration
async def test_resolve_user_uid_no_persona_specific_p2p_fails_loud(directory_db):
    """真人只跟**别的 persona** 的 bot 私聊过、没跟当前发送 persona 私聊过 → 不可投递，
    绝不因为别的 persona 那条线存在就误当可投递（fail-loud，不代选）。
    """
    conv_chinagi = uuid.uuid5(uuid.NAMESPACE_OID, "conv-only-chinagi-p2p")
    await _seed_user(_OTHER, "只跟千凪聊过的人")
    await _seed_conversation(conv_chinagi, scope="direct", name="千凪线")
    await _seed_message(
        conv_chinagi, user_id=_OTHER, bot_name="chinagi-bot", scope="direct", event_time=1000,
    )
    await _seed_bot_config("chinagi-bot", "chinagi", is_active=True)

    with pytest.raises(UndeliverableRecipient) as exc:
        await resolve_delivery(f"user:{_OTHER}", persona_id="akao")
    msg = str(exc.value)
    assert "私聊" in msg or "p2p" in msg.lower()


@pytest.mark.integration
async def test_resolve_user_uid_picks_most_recent_among_persona_own_bots(directory_db):
    """同一 persona 名下有不止一个 bot（各自一条私聊线）→ 在**自己名下**的线里选最近
    活跃的那条，不是随便一条（过滤到 persona 名下之后，"选最近"这条既有行为要保留）。
    """
    conv_old = uuid.uuid5(uuid.NAMESPACE_OID, "conv-bezhai-akao-old-bot")
    conv_new = uuid.uuid5(uuid.NAMESPACE_OID, "conv-bezhai-akao-new-bot")
    await _seed_user(_BEZHAI, "原智鸿")
    await _seed_conversation(conv_old, scope="direct", name="旧线")
    await _seed_message(
        conv_old, user_id=_BEZHAI, bot_name="chiwei-old", scope="direct", event_time=1000,
    )
    await _seed_bot_config("chiwei-old", "akao", is_active=True)

    await _seed_conversation(conv_new, scope="direct", name="新线")
    await _seed_message(
        conv_new, user_id=_BEZHAI, bot_name="chiwei-new", scope="direct", event_time=2000,
    )
    await _seed_bot_config("chiwei-new", "akao", is_active=True)

    target = await resolve_delivery(f"user:{_BEZHAI}", persona_id="akao")

    assert target.bot_name == "chiwei-new", "同名下多个 bot 时选最近活跃那条"
    assert target.common_conversation_id == str(conv_new)


@pytest.mark.integration
async def test_resolve_user_uid_falls_back_to_active_bot_when_latest_inactive(
    directory_db,
):
    """最近活跃的那条私聊线所属 bot 已经 inactive（下线 / 停用）→ 不当它可投递，回退
    到同 persona 名下仍 active 的那条旧线，而不是直接 fail-loud（还有一条能发就该发）。
    """
    conv_old_active = uuid.uuid5(uuid.NAMESPACE_OID, "conv-bezhai-akao-still-active")
    conv_new_inactive = uuid.uuid5(uuid.NAMESPACE_OID, "conv-bezhai-akao-retired-bot")
    await _seed_user(_BEZHAI, "原智鸿")
    await _seed_conversation(conv_old_active, scope="direct", name="仍在用的线")
    await _seed_message(
        conv_old_active, user_id=_BEZHAI, bot_name="chiwei-active", scope="direct",
        event_time=1000,
    )
    await _seed_bot_config("chiwei-active", "akao", is_active=True)

    await _seed_conversation(conv_new_inactive, scope="direct", name="bot 已下线的线")
    await _seed_message(
        conv_new_inactive, user_id=_BEZHAI, bot_name="chiwei-retired", scope="direct",
        event_time=2000,
    )
    await _seed_bot_config("chiwei-retired", "akao", is_active=False)

    target = await resolve_delivery(f"user:{_BEZHAI}", persona_id="akao")

    assert target.bot_name == "chiwei-active", "最近那条的 bot 已下线，回退到仍 active 的旧线"
    assert target.common_conversation_id == str(conv_old_active)


async def test_resolve_malformed_user_id_fails_loud():
    """``user:<非 uuid>`` 在解析阶段就识别成不可投递、抛 UndeliverableRecipient
    （codex 建议 2），绝不把脏串往 SQL CAST 里送、让底层 DB 错穿出去。"""
    with pytest.raises(UndeliverableRecipient):
        await resolve_delivery("user:not-a-uuid")


# ---------------------------------------------------------------------------
# resolve_delivery — 坏 uid fail-loud
# ---------------------------------------------------------------------------


async def test_resolve_malformed_uid_fails_loud():
    """没有 type 前缀 / 未知 type / 空 → fail-loud，不静默当某种。

    ``group:`` 现在是合法前缀（走群分支），不再在「未知 type」里——它的不可投递由
    群分支的白名单 / scope / channel / active 闸负责（见群解析测试）。
    """
    for bad in ["akao", "wechat:xx", "", "persona:", "user:"]:
        with pytest.raises(UndeliverableRecipient):
            await resolve_delivery(bad)


# ---------------------------------------------------------------------------
# group uid 体系（Task 1）：群成为一等可投递对象 + 安全闸 + 模糊查
# ---------------------------------------------------------------------------


def test_group_uid_format():
    assert group_uid(_SOME_GROUP) == f"group:{_SOME_GROUP}"


@pytest.mark.integration
async def test_resolve_group_uid_in_whitelist_to_group_target(directory_db, monkeypatch):
    """白名单内 + scope=group + channel=lark + active 的群 → GroupTarget，带正确 bot_name。

    bot_name 是「赤尾该 persona 在这个群用的 active bot」，从群里 akao 的回复
    （common_agent_response.persona_id=akao → bot_name）解析钉死，出站身份确定。
    """
    await _seed_conversation(_SOME_GROUP, scope="group", name="🐢🐢群（飞书版）")
    await _seed_group_assistant_response(
        _SOME_GROUP, persona_id="akao", bot_name="chiwei", event_time=1000
    )
    _patch_whitelist(monkeypatch, str(_SOME_GROUP))

    target = await rd.resolve_delivery(group_uid(_SOME_GROUP), persona_id="akao")

    assert isinstance(target, GroupTarget)
    assert target.common_conversation_id == str(_SOME_GROUP)
    assert target.bot_name == "chiwei"
    assert target.channel == "lark"


@pytest.mark.integration
async def test_resolve_group_uid_not_in_whitelist_fails_loud(directory_db, monkeypatch):
    """白名单外的 group:<id> → fail-loud（安全闸，绝不返回伪地址）。

    这是真正的闸：send_message(group:<id>) 模型可能从别处拿到 / 编出非白名单群 id
    绕过 look_up，所以投递最后一关硬挡。
    """
    await _seed_conversation(_SOME_GROUP, scope="group", name="某业务群")
    await _seed_group_assistant_response(
        _SOME_GROUP, persona_id="akao", bot_name="chiwei", event_time=1000
    )
    # 白名单里是另一个群，本群不在内。
    _patch_whitelist(monkeypatch, str(uuid.uuid4()))

    with pytest.raises(UndeliverableRecipient):
        await rd.resolve_delivery(group_uid(_SOME_GROUP), persona_id="akao")


@pytest.mark.integration
async def test_resolve_group_uid_empty_whitelist_fails_loud(directory_db, monkeypatch):
    """白名单为空（配置缺失 / 误删）→ fail-closed：任何群都不可投递（对称感知侧）。"""
    await _seed_conversation(_SOME_GROUP, scope="group", name="群")
    await _seed_group_assistant_response(
        _SOME_GROUP, persona_id="akao", bot_name="chiwei", event_time=1000
    )
    _patch_whitelist(monkeypatch, "")

    with pytest.raises(UndeliverableRecipient):
        await rd.resolve_delivery(group_uid(_SOME_GROUP), persona_id="akao")


@pytest.mark.integration
async def test_resolve_group_uid_direct_scope_fails_loud(directory_db, monkeypatch):
    """uid 形如 group: 但那个会话其实是 direct（scope 不符）→ fail-loud。"""
    conv = uuid.uuid5(uuid.NAMESPACE_OID, "conv-is-actually-direct")
    await _seed_conversation(conv, scope="direct", name="其实是私聊")
    _patch_whitelist(monkeypatch, str(conv))

    with pytest.raises(UndeliverableRecipient):
        await rd.resolve_delivery(group_uid(conv), persona_id="akao")


@pytest.mark.integration
async def test_resolve_group_uid_non_lark_fails_loud(directory_db, monkeypatch):
    """白名单内 + scope=group 但 channel 非 lark → fail-loud（这一刀只接飞书群）。"""
    conv = uuid.uuid5(uuid.NAMESPACE_OID, "conv-group-wecom")
    await _seed_conversation(conv, scope="group", name="企微群", channel="wecom")
    _patch_whitelist(monkeypatch, str(conv))

    with pytest.raises(UndeliverableRecipient):
        await rd.resolve_delivery(group_uid(conv), persona_id="akao")


@pytest.mark.integration
async def test_resolve_group_uid_inactive_fails_loud(directory_db, monkeypatch):
    """白名单内 + scope=group + lark 但会话 is_active=False（已解散 / 退群）→ fail-loud。"""
    conv = uuid.uuid5(uuid.NAMESPACE_OID, "conv-group-inactive")
    async with session_mod.get_session() as s:
        s.add(
            CommonConversation(
                common_conversation_id=conv,
                channel="lark",
                scope="group",
                display_name="解散的群",
                is_active=False,
            )
        )
    await _seed_group_assistant_response(
        conv, persona_id="akao", bot_name="chiwei", event_time=1000
    )
    _patch_whitelist(monkeypatch, str(conv))

    with pytest.raises(UndeliverableRecipient):
        await rd.resolve_delivery(group_uid(conv), persona_id="akao")


@pytest.mark.integration
async def test_resolve_group_uid_no_active_bot_for_persona_fails_loud(
    directory_db, monkeypatch
):
    """群合法（白名单 + group + lark + active），但解析不到该 persona 的 active bot_name
    → fail-loud（出站身份缺失绝不返回伪地址，proactive 不写 agent_response、worker
    没别处推断 bot）。"""
    await _seed_conversation(_SOME_GROUP, scope="group", name="没赤尾发过言的群")
    # 群里只有别的 persona 回复过（chinagi），没有 akao 的归属 → 解析不出 akao 的 bot。
    await _seed_group_assistant_response(
        _SOME_GROUP, persona_id="chinagi", bot_name="other-bot", event_time=1000
    )
    _patch_whitelist(monkeypatch, str(_SOME_GROUP))

    with pytest.raises(UndeliverableRecipient):
        await rd.resolve_delivery(group_uid(_SOME_GROUP), persona_id="akao")


@pytest.mark.integration
async def test_resolve_group_uid_picks_persona_specific_bot(directory_db, monkeypatch):
    """同群多 persona 发过言 → 解析出**调用方 persona 自己**的 active bot，不串到别人。"""
    await _seed_conversation(_SOME_GROUP, scope="group", name="三姐妹都在的群")
    await _seed_group_assistant_response(
        _SOME_GROUP, persona_id="chinagi", bot_name="chinagi-bot", event_time=900
    )
    await _seed_group_assistant_response(
        _SOME_GROUP, persona_id="akao", bot_name="chiwei", event_time=1000
    )
    _patch_whitelist(monkeypatch, str(_SOME_GROUP))

    target = await rd.resolve_delivery(group_uid(_SOME_GROUP), persona_id="akao")
    assert isinstance(target, GroupTarget)
    assert target.bot_name == "chiwei", "解析出 akao 自己的 bot，不串到 chinagi 的"


@pytest.mark.integration
async def test_resolve_group_uid_bot_not_in_presence_fails_loud(
    directory_db, monkeypatch
):
    """群合法、该 persona 历史发过言，但解析出的 bot 已被移出群（无 active presence 行）
    → fail-loud（codex 必改 2）。

    只看历史 assistant 回复 + bot_config.is_active 不够：bot 被踢出群后历史回复还在、
    bot_config 也可能仍 active，但它已不在这个群、发不出去。presence 闸（对齐 persona.py
    的 resolve_bot_name_for_persona）必须把这种解析判成不可投递、投递前 fail-loud，而不是
    成功解析后让 worker 异步发失败。
    """
    await _seed_conversation(_SOME_GROUP, scope="group", name="bot 已被移出的群")
    # akao 历史在群里发过言（agent_response + assistant 消息），但**不播 presence**，
    # 单独播一条 is_active=False 的 presence（bot 被移出群、presence 翻 false）。
    await _seed_group_assistant_response(
        _SOME_GROUP, persona_id="akao", bot_name="chiwei", event_time=1000,
        seed_presence=False,
    )
    await _seed_bot_presence(_SOME_GROUP, "chiwei", is_active=False)
    _patch_whitelist(monkeypatch, str(_SOME_GROUP))

    with pytest.raises(UndeliverableRecipient):
        await rd.resolve_delivery(group_uid(_SOME_GROUP), persona_id="akao")


@pytest.mark.integration
async def test_resolve_group_uid_bot_presence_row_missing_fails_loud(
    directory_db, monkeypatch
):
    """连 presence 行都没有（bot 从未被记录在这个群）→ 同样 fail-loud（presence 闸的
    缺行分支，对称 inactive 分支）。"""
    await _seed_conversation(_SOME_GROUP, scope="group", name="无 presence 行的群")
    await _seed_group_assistant_response(
        _SOME_GROUP, persona_id="akao", bot_name="chiwei", event_time=1000,
        seed_presence=False,
    )
    _patch_whitelist(monkeypatch, str(_SOME_GROUP))

    with pytest.raises(UndeliverableRecipient):
        await rd.resolve_delivery(group_uid(_SOME_GROUP), persona_id="akao")


@pytest.mark.integration
async def test_resolve_group_uid_proactive_only_resolves_via_bot_config(
    directory_db, monkeypatch
):
    """群里该 persona 只发过 proactive（无 common_agent_response 行、response_id=NULL）
    → 靠 bot_config 把 bot_name 归属到 persona，仍解析出正确 bot（codex 建议 1）。

    proactive 出站不写 common_agent_response，所以 LEFT JOIN car 这一支为空、persona
    归属落到 COALESCE 的 bot_config 兜底支。这条专门覆盖那个 fallback：删掉 bot_config
    分支会让本例解析不出 bot → fail-loud（红），保护到 fallback。
    """
    await _seed_conversation(_SOME_GROUP, scope="group", name="只发过 proactive 的群")
    # 群里只有一条 proactive assistant 消息：bot_name 有，但 response_id=NULL（没有
    # 对应的 common_agent_response 行），persona 归属只能靠 bot_config。
    async with session_mod.get_session() as s:
        s.add(
            CommonMessage(
                common_message_id=uuid.uuid4(),
                channel="lark",
                common_conversation_id=_SOME_GROUP,
                common_user_id=None,
                sender_display_name=None,
                role="assistant",
                content=[{"kind": "text", "text": "主动说一句"}],
                content_text="主动说一句",
                scope="group",
                bot_name="chiwei",
                response_id=None,  # proactive：不写 agent_response
                event_time=1000,
            )
        )
    await _seed_bot_config("chiwei", "akao", is_active=True)
    await _seed_bot_presence(_SOME_GROUP, "chiwei", is_active=True)
    _patch_whitelist(monkeypatch, str(_SOME_GROUP))

    target = await rd.resolve_delivery(group_uid(_SOME_GROUP), persona_id="akao")

    assert isinstance(target, GroupTarget)
    assert target.bot_name == "chiwei", "proactive-only 群靠 bot_config 解析出正确 bot"
    assert target.common_conversation_id == str(_SOME_GROUP)


@pytest.mark.integration
async def test_search_recipients_includes_whitelisted_group(directory_db, monkeypatch):
    """模糊查群名命中白名单内的群 → 候选带 group:<id> uid 混进结果。"""
    await _seed_three_sisters()
    await _seed_conversation(_SOME_GROUP, scope="group", name="🐢🐢群（飞书版）")
    _patch_whitelist(monkeypatch, str(_SOME_GROUP))

    got = await search_recipients("🐢🐢群")

    uids = {c.uid for c in got}
    assert group_uid(_SOME_GROUP) in uids, "白名单群应作为候选混进来"


@pytest.mark.integration
async def test_search_recipients_excludes_non_whitelisted_group(
    directory_db, monkeypatch
):
    """模糊查只返白名单内的群 —— 名字对得上但不在白名单的群绝不出现在候选里。"""
    in_wl = uuid.uuid5(uuid.NAMESPACE_OID, "conv-group-in-wl")
    out_wl = uuid.uuid5(uuid.NAMESPACE_OID, "conv-group-out-wl")
    await _seed_conversation(in_wl, scope="group", name="海龟群A")
    await _seed_conversation(out_wl, scope="group", name="海龟群B")
    _patch_whitelist(monkeypatch, str(in_wl))

    got = await search_recipients("海龟群")

    uids = {c.uid for c in got}
    assert group_uid(in_wl) in uids
    assert group_uid(out_wl) not in uids, "非白名单群绝不出现在候选里"


@pytest.mark.integration
async def test_search_recipients_excludes_direct_conversation_as_group(
    directory_db, monkeypatch
):
    """群模糊查只匹配 scope=group：白名单里即便混进一个 direct 会话也不当群候选返回。"""
    direct_conv = uuid.uuid5(uuid.NAMESPACE_OID, "conv-direct-named-like-group")
    await _seed_conversation(direct_conv, scope="direct", name="像群名的私聊")
    _patch_whitelist(monkeypatch, str(direct_conv))

    got = await search_recipients("像群名的私聊")

    uids = {c.uid for c in got}
    assert group_uid(direct_conv) not in uids, "direct 会话不作群候选"
