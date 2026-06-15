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

import app.data.session as session_mod
from app.data.models import (
    Base,
    BotPersona,
    CommonConversation,
    CommonMessage,
    CommonUser,
)
from app.domain.recipient_directory import (
    LarkP2PTarget,
    MailboxTarget,
    UndeliverableRecipient,
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


@pytest.fixture
async def directory_db(test_db):
    """建 recipient_directory 查询要 join 的表（bot_persona + common_*）。"""
    tables = [
        BotPersona.__table__,
        CommonUser.__table__,
        CommonConversation.__table__,
        CommonMessage.__table__,
    ]
    async with test_db.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables)
        )
    yield test_db


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

    target = await resolve_delivery(f"user:{_BEZHAI}")

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
        await resolve_delivery(f"user:{_STRANGER}")
    msg = str(exc.value)
    assert "私聊" in msg or "p2p" in msg.lower()


@pytest.mark.integration
async def test_resolve_unknown_user_uid_fails_loud(directory_db):
    await _seed_bezhai_with_p2p()

    with pytest.raises(UndeliverableRecipient):
        await resolve_delivery(f"user:{uuid.uuid4()}")


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

    with pytest.raises(UndeliverableRecipient) as exc:
        await resolve_delivery(f"user:{user}")
    msg = str(exc.value)
    assert "私聊" in msg or "p2p" in msg.lower()


async def test_resolve_malformed_user_id_fails_loud():
    """``user:<非 uuid>`` 在解析阶段就识别成不可投递、抛 UndeliverableRecipient
    （codex 建议 2），绝不把脏串往 SQL CAST 里送、让底层 DB 错穿出去。"""
    with pytest.raises(UndeliverableRecipient):
        await resolve_delivery("user:not-a-uuid")


# ---------------------------------------------------------------------------
# resolve_delivery — 坏 uid fail-loud
# ---------------------------------------------------------------------------


async def test_resolve_malformed_uid_fails_loud():
    """没有 type 前缀 / 未知 type / 空 → fail-loud，不静默当某种。"""
    for bad in ["akao", "group:xx", "", "persona:", "user:"]:
        with pytest.raises(UndeliverableRecipient):
            await resolve_delivery(bad)
