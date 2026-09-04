"""手机 —— 信封可感，内容要她去看。

五条硬边界，各有对应的用例：

  * **每一缝拿到的只有信封。** 有没有动静、谁、多密多快、跟她刚才干的事有没有牵连。
    正文一个字都不在信封里 —— 不然"看手机"就成了摆设，她躺着就把消息读完了。
  * **打开一条会话看到的是最近若干条往来**，双向、含她自己说过的、含读过的上文。
    窗口、未读、游标是三件事：窗口不看游标，未读是"游标之后别人发的没撤的"，游标只
    推到未读里最新那条。
  * **看手机是她的动作，成功返回之后才推游标。** 中途炸掉 = 一条都不算已读。
  * **挤出窗口的那些永久丢失。** 她只看最后十来条，前面的不会补看：真人"未读 47 条"
    就是这样。
  * **睡觉时消息照堆、不算已读。** 她没做这个动作，游标就不动。
"""
from __future__ import annotations

import datetime as dt
import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from app.data import session as session_mod
from app.living.phone import (
    NEVER_LOOKED,
    PHONE_GLANCE_LIMIT,
    conversation_as_she_knows_it,
    envelopes_for,
    look_at_phone,
    look_up_contact,
    newest_unread_summons,
    phone_envelope,
    reachable_conversations,
    read_through,
    render_envelopes,
)

LANE = "coe-living"
_CST = dt.timezone(dt.timedelta(hours=8))

_AKAO_BOT_UID = uuid.uuid5(uuid.NAMESPACE_OID, "bot-akao-common-user")
_AYANA_BOT_UID = uuid.uuid5(uuid.NAMESPACE_OID, "bot-ayana-common-user")
_BEZHAI = uuid.uuid5(uuid.NAMESPACE_OID, "human-bezhai")
_SOMEONE = uuid.uuid5(uuid.NAMESPACE_OID, "human-someone")

# 同毫秒那条用得着：uuidv7 按生成时刻单调，所以 A < B 就是"A 先生成"。
_UUID7_A = uuid.UUID("01920000-0000-7000-8000-00000000000a")
_UUID7_B = uuid.UUID("01920000-0000-7000-8000-00000000000b")

_DM = uuid.uuid5(uuid.NAMESPACE_OID, "conv-dm-bezhai-akao")
_GROUP = uuid.uuid5(uuid.NAMESPACE_OID, "conv-group-lab")
_OTHERS_DM = uuid.uuid5(uuid.NAMESPACE_OID, "conv-dm-bezhai-ayana")

# 她能拿去撤回的那个编号，两种写法是同一个值：她眼前只见得到 32 位无短横的 hex
# （快照的「你刚做过、说过」印的就是它），而 ``common_message.agent_outbound_id``
# 是 uuid 列、存带短横那一种。**这份向量两侧共读**，TS 那边是
# ``apps/lark-service/src/lark/outbound/proactive-message-id.test.ts``；换算错了全程
# 零报错 —— 她照抄的编号查不到任何行，撤回只说"没有这条"。
_OUTBOUND_VECTOR = json.loads(
    (
        Path(__file__).resolve().parents[4]
        / "contracts"
        / "proactive-message-id.json"
    ).read_text(encoding="utf-8")
)["outbound_id_vector"]

async def _seed_noisy_groups(how_many: int, *, at_from: dt.datetime) -> list[str]:
    """一堆在刷屏、但**没点她名字**的群。信封上限该截的正是这些。"""
    made: list[str] = []
    for i in range(how_many):
        conv = uuid.uuid5(uuid.NAMESPACE_OID, f"conv-noisy-{i}")
        async with session_mod.get_session() as s:
            await s.execute(
                text(
                    "INSERT INTO common_conversation "
                    "(common_conversation_id, channel, scope, display_name, is_active)"
                    " VALUES (CAST(:c AS uuid), 'lark', 'group', :t, true)"
                ),
                {"c": str(conv), "t": f"群{i}"},
            )
            await s.execute(
                text(
                    "INSERT INTO common_bot_presence "
                    "(common_conversation_id, bot_name, is_active) "
                    "VALUES (CAST(:c AS uuid), 'chiwei', true)"
                ),
                {"c": str(conv)},
            )
        await _incoming(
            conv,
            text_body=f"群{i}在闲聊",
            at=at_from + dt.timedelta(minutes=i),
            sender=_SOMEONE,
            sender_name="路人",
            scope="group",
        )
        made.append(str(conv))
    return made


def _at(hour: int, minute: int = 0, second: int = 0) -> dt.datetime:
    return dt.datetime(2026, 7, 25, hour, minute, second, tzinfo=_CST)


def _ms(moment: dt.datetime) -> int:
    return int(moment.timestamp() * 1000)


async def _seed_world() -> None:
    """一份最小的真实世界：两个 bot、两个真人、一条私聊 + 一个群。"""
    async with session_mod.get_session() as s:
        for uid, name in (
            (_AKAO_BOT_UID, "赤尾"),
            (_AYANA_BOT_UID, "绫奈"),
            (_BEZHAI, "bezhai"),
            (_SOMEONE, "路人"),
        ):
            await s.execute(
                text(
                    "INSERT INTO common_user "
                    "(common_user_id, channel, display_name) "
                    "VALUES (CAST(:u AS uuid), 'lark', :n)"
                ),
                {"u": str(uid), "n": name},
            )
        for conv, scope, title in (
            (_DM, "direct", "bezhai"),
            (_GROUP, "group", "宅居研究所"),
            (_OTHERS_DM, "direct", "bezhai"),
        ):
            await s.execute(
                text(
                    "INSERT INTO common_conversation "
                    "(common_conversation_id, channel, scope, display_name, is_active) "
                    "VALUES (CAST(:c AS uuid), 'lark', :s, :t, true)"
                ),
                {"c": str(conv), "s": scope, "t": title},
            )
        for bot, persona, bot_uid in (
            ("chiwei", "akao", _AKAO_BOT_UID),
            ("ayana-bot", "ayana", _AYANA_BOT_UID),
        ):
            await s.execute(
                text(
                    "INSERT INTO bot_config "
                    "(bot_name, persona_id, common_user_id, is_active) "
                    "VALUES (:b, :p, CAST(:u AS uuid), true)"
                ),
                {"b": bot, "p": persona, "u": str(bot_uid)},
            )
        # 群里**三个人的 bot 都在**（prod common_bot_presence 实测就是这样：同一个
        # 群里同时挂着 ayana / chinagi / chiwei）。同群多 persona 是常态，不是边缘
        # 情况 —— 姐姐在这个群里说的话，她本来就该看得见。
        for conv, bot in (
            (_DM, "chiwei"),
            (_GROUP, "chiwei"),
            (_GROUP, "ayana-bot"),
            (_OTHERS_DM, "ayana-bot"),
        ):
            await s.execute(
                text(
                    "INSERT INTO common_bot_presence "
                    "(common_conversation_id, bot_name, is_active) "
                    "VALUES (CAST(:c AS uuid), :b, true)"
                ),
                {"c": str(conv), "b": bot},
            )


async def _incoming(
    conv: uuid.UUID,
    *,
    text_body: str = "",
    at: dt.datetime,
    sender: uuid.UUID = _BEZHAI,
    sender_name: str = "bezhai",
    names_bot: uuid.UUID | None = None,
    names_others: tuple[uuid.UUID, ...] = (),
    mention_unrecorded: bool = False,
    scope: str | None = None,
    bot_name: str = "chiwei",
    message_id: uuid.UUID | None = None,
    items: list[dict] | None = None,
    content_text: str | None = None,
) -> str:
    """真人发来的一条消息。

    ``names_bot`` / ``names_others`` 写进 ``mentioned_common_user_ids`` ——
    「这条消息点了谁的名」是这一列上的事实，**不在 ``content`` 里**。公共层的内容
    契约只有 text/image/audio/file/sticker/unsupported 六种片段，@ 在飞书投影时被
    内联回了正文，所以往 ``content`` 里塞一条 mention item 是造不存在的形状。

    ``mention_unrecorded`` 写 NULL：加列之前的存量行、QQ 的行、飞书新写入方上线
    之前的行都长这样。**NULL 不等于空数组** —— 空数组是"算过、确实谁都没点"，
    NULL 是"没人算过"，后者不能被当成确认没点名。

    ``items`` / ``content_text`` 给了就照原样落库。附件消息上这两列是**两份互不
    等价的事实**：投影层把每个非文本项都拼成字面的 ``[kind]`` 写进 ``content_text``
    （lark-service ``inbound-projection.ts`` 的 ``summarize``、channel-server
    ``common-projector.ts`` 的 ``textProjection``），文件名只留在 items 的
    ``meta.file_name`` 里。要验她看不看得出发来的是什么，就得能分别摆布这两边。
    """
    if items is None:
        items = [{"kind": "text", "text": text_body}]
    named = [str(u) for u in ((names_bot,) if names_bot else ()) + names_others]
    mid = message_id or uuid.uuid4()
    resolved_scope = scope or ("direct" if conv in (_DM, _OTHERS_DM) else "group")
    async with session_mod.get_session() as s:
        await s.execute(
            text(
                "INSERT INTO common_message "
                "(common_message_id, channel, common_conversation_id, common_user_id,"
                " sender_display_name, role, content, content_text, scope, bot_name,"
                " event_time, mentioned_common_user_ids) "
                "VALUES (CAST(:m AS uuid), 'lark', CAST(:c AS uuid), CAST(:u AS uuid),"
                " :sn, 'user', CAST(:body AS jsonb), :txt, :sc, :bn, :et,"
                " CAST(:named AS text[])::uuid[])"
            ),
            {
                "m": str(mid),
                "c": str(conv),
                "u": str(sender),
                "sn": sender_name,
                "body": json.dumps(items, ensure_ascii=False),
                "txt": text_body if content_text is None else content_text,
                "sc": resolved_scope,
                "bn": bot_name,
                "et": _ms(at),
                "named": None if mention_unrecorded else named,
            },
        )
    return str(mid)


async def _bot_said(
    conv: uuid.UUID,
    *,
    text_body: str,
    at: dt.datetime,
    bot_name: str,
    bot_uid: uuid.UUID,
    display_name: str,
    outbound_id: str | None = None,
) -> str:
    """某个 bot 在这条会话里说过的一句（``role='assistant'``）。

    形状照真的出站那一处写（``apps/lark-service/src/lark/outbound/deliver.ts``）：
    content item 是 ``kind``/``text``、``common_user_id`` 和 ``sender_display_name``
    都是那个 bot 的、``bot_name`` 是发这句话的 bot。

    **``role`` 只说明"这是某个 bot 发的"，说不出是哪个。** 三姐妹在同一个群里，
    她们的出站落在这张表里长得一模一样 —— 分得开的只有 ``bot_name``。

    ``outbound_id`` 给了才写 ``agent_outbound_id``（带短横的标准 uuid，投递方剥掉
    ``proactive:`` 前缀之后落的就是它）。**只有主动发起的那些行有这一列**：她回复
    别人的消息走另一条链，那条链不写这一列，所以那些消息她撤不了。留空正是在摆那种
    行的真实形状。
    """
    mid = uuid.uuid4()
    resolved_scope = "direct" if conv in (_DM, _OTHERS_DM) else "group"
    async with session_mod.get_session() as s:
        await s.execute(
            text(
                "INSERT INTO common_message "
                "(common_message_id, channel, common_conversation_id, common_user_id,"
                " sender_display_name, role, content, content_text, scope, bot_name,"
                " event_time, agent_outbound_id) "
                "VALUES (CAST(:m AS uuid), 'lark', CAST(:c AS uuid), CAST(:u AS uuid),"
                " :sn, 'assistant', CAST(:body AS jsonb), :txt, :sc, :bn, :et,"
                " CAST(:oid AS uuid))"
            ),
            {
                "m": str(mid),
                "c": str(conv),
                "u": str(bot_uid),
                "sn": display_name,
                "body": json.dumps(
                    [{"kind": "text", "text": text_body}], ensure_ascii=False
                ),
                "txt": text_body,
                "sc": resolved_scope,
                "bn": bot_name,
                "et": _ms(at),
                "oid": outbound_id,
            },
        )
    return str(mid)


async def _recalled_on_the_channel(message_id: str, *, at: dt.datetime) -> None:
    """渠道那边把这一行撤掉了。

    **撤回不删这一行**：公共层是消息记录，删行会打断历史。所以"这条还在不在会话
    里"由 ``recalled_at`` 说了算 —— 非空 = 渠道上它已经不在了。填这一列的是投递侧
    （lark-service），撤成功才填、撤失败不填。
    """
    async with session_mod.get_session() as s:
        await s.execute(
            text(
                "UPDATE common_message SET recalled_at = :at "
                "WHERE common_message_id = CAST(:m AS uuid)"
            ),
            {"at": at, "m": message_id},
        )


async def _her_own(conv: uuid.UUID, *, text_body: str, at: dt.datetime) -> str:
    """她自己在这条会话里说过的一句（她的 bot 是 ``chiwei``）。

    **不带 ``agent_outbound_id``** —— 这是她**回复**别人时那条链落下的形状，撤不了。
    她主动发起的那些用 :func:`_her_own_proactive`。
    """
    return await _bot_said(
        conv,
        text_body=text_body,
        at=at,
        bot_name="chiwei",
        bot_uid=_AKAO_BOT_UID,
        display_name="赤尾",
    )


async def _her_own_proactive(
    conv: uuid.UUID,
    *,
    text_body: str,
    at: dt.datetime,
    outbound_uuid: str | None = None,
) -> tuple[str, str]:
    """她**主动发起**的一句，带着那次开口的编号。

    返回 ``(这一行的 common_message_id, 她眼前该看到的那种写法)`` —— 后者是 32 位无
    短横的 hex，跟库里那一列（带短横的标准 uuid）是同一个值。

    真链路是：嘴那边派生一个 uuid，把 ``proactive:<uuid>`` 挂在出站信封上，投递方剥掉
    前缀把 uuid 落进 ``agent_outbound_id``。她能撤的严格就是这些行。
    """
    dashed = outbound_uuid or str(uuid.uuid4())
    mid = await _bot_said(
        conv,
        text_body=text_body,
        at=at,
        bot_name="chiwei",
        bot_uid=_AKAO_BOT_UID,
        display_name="赤尾",
        outbound_id=dashed,
    )
    return mid, uuid.UUID(dashed).hex


async def _sister_said(conv: uuid.UUID, *, text_body: str, at: dt.datetime) -> str:
    """姐姐在同一个群里说过的一句（她的 bot 是 ``ayana-bot``）。"""
    return await _bot_said(
        conv,
        text_body=text_body,
        at=at,
        bot_name="ayana-bot",
        bot_uid=_AYANA_BOT_UID,
        display_name="绫奈",
    )


# --------------------------------------------------------------------------
# 一 · 她的手机上有哪些会话
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_conversation_is_on_her_phone_when_her_own_bot_is_in_it(living_db):
    await _seed_world()

    mine = {c.channel_id for c in await reachable_conversations(persona_id="akao")}

    assert mine == {str(_DM), str(_GROUP)}, (
        "私聊和群用的是同一条规则：她自己的 bot 还在这个会话里。"
        "姐姐的私聊线不该出现在她手机上。"
    )


# --------------------------------------------------------------------------
# 二 · 信封可感，内容要她去看
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_the_envelope_never_leaks_a_single_word_of_the_message(living_db):
    await _seed_world()
    await _incoming(_DM, text_body="周末那家抹茶店你去过没", at=_at(21, 30))
    await _incoming(_DM, text_body="想约一下", at=_at(21, 31))

    envelope = await phone_envelope(lane=LANE, persona_id="akao", now=_at(21, 35))

    assert "抹茶店" not in envelope and "想约一下" not in envelope, (
        f"信封漏了正文 —— 那「看手机」这个动作就不存在了。拿到：\n{envelope}"
    )
    assert "bezhai" in envelope and "2" in envelope, (
        f"信封里该有谁、有多少条。拿到：\n{envelope}"
    )


@pytest.mark.integration
async def test_the_envelope_says_when_she_last_spoke_there(living_db):
    """「跟她刚才干的事有没有牵连」是**事实**，不是我们替她算的权重。"""
    await _seed_world()
    await _incoming(_GROUP, text_body="有人在吗", at=_at(21, 30), sender=_SOMEONE,
                    sender_name="路人")

    from app.living.happening import record_happening
    from app.living.records import KIND_SPEECH, MEDIUM_GROUP_CHAT

    await record_happening(
        lane=LANE,
        happening_id="own-1",
        actor="akao",
        place="家/我房间",
        kind=KIND_SPEECH,
        content="我在。",
        occurred_at=_at(21, 25),
        medium=MEDIUM_GROUP_CHAT,
        channel_id=str(_GROUP),
    )

    envelope = await phone_envelope(lane=LANE, persona_id="akao", now=_at(21, 35))

    assert "21:25" in envelope, (
        f"信封里该有「你上次在这儿开口是什么时候」 —— 五分钟前刚说过话的群，"
        f"和三天没说话的群，对她不是一回事。拿到：\n{envelope}"
    )


@pytest.mark.integration
async def test_an_empty_phone_says_so_instead_of_leaving_a_hole(living_db):
    await _seed_world()

    envelope = await phone_envelope(lane=LANE, persona_id="akao", now=_at(21, 35))

    assert envelope.strip() != ""


# --------------------------------------------------------------------------
# 三 · 打开一条会话 —— 窗口、未读、游标是三件事
# --------------------------------------------------------------------------
#
# 改之前这三件事是同一个查询条件的三个身份：那条查询同时带着"不是她自己发的"、
# "没撤掉的"、"游标之后的"，于是"看手机"不是翻聊天记录，是**看未读**。
#
# 实证（coe-living，2026-09-04）：她自己撤回了一句话，8 分钟后还在问主人"你刚才到底
# 发了啥、这么想让我看到又撤回"。那一缝她眼前只有孤零零一句「还真的能撤回啊」——
# 前面的来回全在游标之前，而读过的消息不进任何持久记忆。**决定说什么的那个模型，
# 从来没见过一段双向对话。**
#
# 三件事分开之后：
#
#   * **未读集合 U** = 游标之后的、别人发的、没撤掉的。判据跟改之前逐字相同。
#   * **展示窗口 W** = 这条会话上最近若干条，不看游标、不分谁发的，含她自己撤掉的
#     那条（留痕迹），不含别人撤掉的。
#   * 「其中 N 条是新的」= |U ∩ W|；「前面还有 K 条你没往回翻」= |U − W|。
#   * **游标推到 max(U)，不是 max(W)**：W 里最新那条可能是她自己发的、晚于任何未读，
#     推到它身上会让之后乱序到达、时刻更早的消息被永久跳过，也会让措辞模型那侧把她
#     根本没见过的消息当成"她已经知道的"。


@pytest.mark.integration
async def test_opening_a_conversation_shows_both_sides_of_it(living_db, in_a_moment):
    """她点开一条会话，看到的是一段**双向**的往来，按时间顺序。

    这是根因那一条：只给她看未读，她眼前就永远只有对方那一半，无从知道这段对话进行
    到哪了。真人点开一个会话看到的也正是双向的最近若干条。
    """
    await _seed_world()
    await _incoming(_DM, text_body="你现在能撤回飞书消息没", at=_at(14, 50))
    await _her_own(_DM, text_body="可以哦～", at=_at(14, 50, 30))
    await _incoming(_DM, text_body="还真的能撤回啊", at=_at(14, 58))

    async with in_a_moment("akao", now=_at(14, 59)):
        seen = await look_at_phone.invoke({"channel_id": str(_DM)})

    assert "你：可以哦～" in seen, (
        f"她自己说过的那句不在眼前 —— 她看到的仍然只有对话的一半。拿到：\n{seen}"
    )
    assert (
        seen.index("你现在能撤回飞书消息没")
        < seen.index("你：可以哦～")
        < seen.index("还真的能撤回啊")
    ), f"往来的先后乱了。拿到：\n{seen}"


@pytest.mark.integration
async def test_a_conversation_with_nothing_unread_still_shows_the_recent_exchange(
    living_db, in_a_moment
):
    """一条未读都没有时打开会话，仍然看得到最近的往来。

    改之前这种情况她看到的是空的（"没有新消息"）—— 于是"再看一眼刚才说到哪了"这个
    真人每天都在做的动作，在这个引擎里根本不存在。
    """
    await _seed_world()
    await _incoming(_DM, text_body="周末那家抹茶店你去过没", at=_at(21, 30))
    async with in_a_moment("akao", now=_at(21, 31)):
        await look_at_phone.invoke({"channel_id": str(_DM)})  # 读完，未读归零
    await _her_own(_DM, text_body="去过呀", at=_at(21, 32))

    assert await envelopes_for(lane=LANE, persona_id="akao") == [], (
        "用例前提没成立：这条会话该已经没有未读了"
    )

    async with in_a_moment("akao", now=_at(21, 40)):
        seen = await look_at_phone.invoke({"channel_id": str(_DM)})

    assert "周末那家抹茶店你去过没" in seen and "你：去过呀" in seen, (
        f"一条未读都没有的时候她眼前是空的 —— 她再也回不去看刚才说到哪了。拿到：\n{seen}"
    )
    assert "其中 0 条是新的" in seen, f"没有新消息这件事得说出来。拿到：\n{seen}"


@pytest.mark.integration
async def test_a_conversation_with_nothing_unread_does_not_move_the_cursor(
    living_db, in_a_moment
):
    """没有未读时打开会话，游标一动不动 —— 窗口里那些行不是"读到了这儿"的依据。

    游标只由未读集合决定。让窗口推游标的话，她开口说了句话、下一缝随手点开会话，
    游标就跳到她自己那句上，之后乱序到达、时刻更早的消息永久被跳过。
    """
    await _seed_world()
    first = await _incoming(_DM, text_body="在吗", at=_at(21, 30))
    async with in_a_moment("akao", now=_at(21, 31)):
        await look_at_phone.invoke({"channel_id": str(_DM)})
    landed = await read_through(lane=LANE, persona_id="akao", channel_id=str(_DM))
    assert landed == (_ms(_at(21, 30)), first)

    await _her_own(_DM, text_body="在的", at=_at(21, 32))
    async with in_a_moment("akao", now=_at(21, 40)):
        await look_at_phone.invoke({"channel_id": str(_DM)})

    assert (
        await read_through(lane=LANE, persona_id="akao", channel_id=str(_DM))
    ) == landed, "一条未读都没有，游标却动了"


@pytest.mark.integration
async def test_her_own_latest_word_does_not_take_the_cursor(living_db, in_a_moment):
    """窗口里最新那条是她自己发的时，游标停在**未读**里最新那条上。

    推到她自己那句上有两处后果：之后乱序到达、时刻更早的消息被永久跳过；而且措辞
    模型那侧（``conversation_as_she_knows_it`` 按游标开窗）会把她根本没见过的消息
    当成"她已经知道的"。
    """
    await _seed_world()
    unread = await _incoming(_DM, text_body="在吗", at=_at(21, 30))
    await _her_own(_DM, text_body="在呢", at=_at(21, 31))

    async with in_a_moment("akao", now=_at(21, 35)):
        seen = await look_at_phone.invoke({"channel_id": str(_DM)})

    assert "你：在呢" in seen, "她自己那句本该在窗口里"
    assert (
        await read_through(lane=LANE, persona_id="akao", channel_id=str(_DM))
    ) == (_ms(_at(21, 30)), unread), (
        "游标被推到了她自己发的那条上 —— 比它早、之后才到的消息从此看不见了"
    )


@pytest.mark.integration
async def test_it_says_how_many_of_them_are_new(living_db, in_a_moment):
    """「其中 N 条是新的」= 未读集合与展示窗口的交集。

    窗口里会有她上一缝已经看过的消息（决策 2b：不配任何"防重复回应"的规则），所以
    哪些是新到的必须直接说出来 —— 她读得出来，读不出来也是她的判断。
    """
    await _seed_world()
    await _incoming(_DM, text_body="早上好", at=_at(9, 0))
    async with in_a_moment("akao", now=_at(9, 1)):
        await look_at_phone.invoke({"channel_id": str(_DM)})
    await _her_own(_DM, text_body="你也早", at=_at(9, 2))
    await _incoming(_DM, text_body="中午吃什么", at=_at(12, 0))
    await _incoming(_DM, text_body="想吃拉面", at=_at(12, 1))

    async with in_a_moment("akao", now=_at(12, 5)):
        seen = await look_at_phone.invoke({"channel_id": str(_DM)})

    assert "其中 2 条是新的" in seen, f"新到几条算错了。拿到：\n{seen}"
    assert "早上好" in seen and "你也早" in seen, (
        f"读过的上文被挡在外面了 —— 那正是她看不懂对话进行到哪的原因。拿到：\n{seen}"
    )


@pytest.mark.integration
async def test_her_own_words_take_up_room_in_the_window(living_db, in_a_moment):
    """窗口是"最近若干条"，她自己发的照样占位置，被挤出去的未读因此更多。

    这条把三件事同时钉住：窗口不分谁发的（10 条里有她 4 条）、「其中 N 条是新的」只
    数未读（6 条）、「前面还有 K 条」是被挤出窗口的未读（2 条）。三者用同一条判据算
    的话，这里必然对不上。
    """
    await _seed_world()
    for i in range(8):
        await _incoming(
            _GROUP,
            text_body=f"路人第{i}句",
            at=_at(20, i),
            sender=_SOMEONE,
            sender_name="路人",
        )
    for i in range(4):
        await _her_own(_GROUP, text_body=f"我第{i}句", at=_at(20, 10 + i))

    async with in_a_moment("akao", now=_at(20, 20)):
        seen = await look_at_phone.invoke({"channel_id": str(_GROUP)})

    assert "其中 6 条是新的" in seen and "还有 2 条" in seen, (
        f"窗口 10 条 = 她自己 4 条 + 最近 6 条未读，未读一共 8 条。拿到：\n{seen}"
    )
    assert "路人第0句" not in seen and "路人第1句" not in seen, (
        f"被挤出窗口的那两条还在眼前。拿到：\n{seen}"
    )
    assert "路人第2句" in seen and "我第3句" in seen, f"拿到：\n{seen}"


@pytest.mark.integration
async def test_the_window_and_the_unread_set_come_from_one_query(
    living_db, in_a_moment
):
    """展示窗口和未读集合由**同一条 SQL** 一次问出来。

    分成两条查询时它们来自两个快照：``app/data/session.py`` 没配更强的隔离级别，
    PostgreSQL 默认 ``READ COMMITTED`` 下同一个事务里连续两条 ``SELECT`` 看到的
    快照可以不同。一条新消息刚好在两条查询之间提交，它不在窗口里、却成了未读里最新
    那条 —— 游标推到它身上，这条她从没见过的消息就被永久跳过了，一句报错都没有。
    并发撤回则让「其中 N 条是新的」和未读总数互相对不上。

    库层面的并发在集成测试里造不出来（要卡在两条查询之间提交一条消息），所以这里钉
    的是**可判定的那件事：这一眼只对库发了一条读 ``common_message`` 的语句**。拆回
    两条的话这个数立刻变 2。同时把两条查询各自的产出都验一遍，确认那一条语句真的
    同时回答了三个问题：窗口是哪几行、未读一共几条、未读里最新那条是哪条。
    """
    from sqlalchemy import event

    read_common_message: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if "common_message" in statement and statement.lstrip()[:6].upper() != "INSERT":
            read_common_message.append(statement)

    await _seed_world()
    # 12 条未读 + 她自己最后说的一句：窗口（10 条）装不下全部未读，而窗口里最新那条
    # 是她自己发的 —— 未读总数和 max(U) 都不是窗口自己算得出来的。
    latest_unread = ""
    for i in range(12):
        latest_unread = await _incoming(_DM, text_body=f"第{i}条", at=_at(20, i))
    await _her_own(_DM, text_body="马上回你", at=_at(20, 12))

    event.listen(living_db.sync_engine, "before_cursor_execute", record)
    try:
        async with in_a_moment("akao", now=_at(20, 20)):
            seen = await look_at_phone.invoke({"channel_id": str(_DM)})
    finally:
        event.remove(living_db.sync_engine, "before_cursor_execute", record)

    assert len(read_common_message) == 1, (
        f"这一眼对库发了 {len(read_common_message)} 条读 common_message 的语句 —— "
        f"窗口和未读来自两个快照，中间提交的那条消息会被永久跳过。"
        f"拿到：\n" + "\n---\n".join(read_common_message)
    )
    assert "其中 9 条是新的" in seen, f"窗口里的未读数算错了。拿到：\n{seen}"
    assert "还有 3 条" in seen, f"未读总数算错了。拿到：\n{seen}"
    assert (
        await read_through(lane=LANE, persona_id="akao", channel_id=str(_DM))
    ) == (_ms(_at(20, 11)), latest_unread), "游标没落在未读里最新那条上"


@pytest.mark.integration
async def test_only_the_words_she_can_take_back_carry_a_handle(
    living_db, in_a_moment
):
    """编号只印在她自己发的、**真能撤**的那些行上。

    真人是看着消息上有没有"撤回"这个选项知道边界的 —— 那是眼前的事实，不是规则文本。
    她回复别人的消息走另一条链，库里没有这个编号、撤不了；印上去就是给她一个指了会
    失败的东西。
    """
    await _seed_world()
    await _incoming(_DM, text_body="在吗", at=_at(21, 29))
    _, handle = await _her_own_proactive(_DM, text_body="在呢在呢", at=_at(21, 30))
    await _her_own(_DM, text_body="刚看到消息", at=_at(21, 31))

    async with in_a_moment("akao", now=_at(21, 35)):
        seen = await look_at_phone.invoke({"channel_id": str(_DM)})

    assert f"你：在呢在呢［{handle}］" in seen, (
        f"她主动发的那句没带编号 —— 撤回时她指不动任何一条。拿到：\n{seen}"
    )
    assert seen.count("［") == 1, (
        f"撤不了的行也带上了编号（她回复别人的那条、别人发的那条）。拿到：\n{seen}"
    )
    assert "［］" not in seen, f"印了个空编号出去。拿到：\n{seen}"


@pytest.mark.integration
async def test_the_handle_here_is_the_one_the_snapshot_already_printed(
    living_db, in_a_moment
):
    """会话里印的编号，跟「你刚做过、说过」那段里印的**逐字一致**。

    她见到的写法只有 32 位无短横的 hex，而库里 ``agent_outbound_id`` 是标准 uuid。
    两处形状不一致的话她会以为那是两种编号；换算错了则是照抄之后撤了个空 —— 两种
    都不报错。写法之间的相等关系由两侧共读的成对向量钉住（``_OUTBOUND_VECTOR``）。
    """
    from app.living.records import (
        KIND_SPEECH,
        MEDIUM_PHONE,
        OUTBOUND_HAPPENING_PREFIX,
        Happening,
    )
    from app.living.snapshot import _own_line

    await _seed_world()
    _, handle = await _her_own_proactive(
        _DM,
        text_body="在呢在呢",
        at=_at(21, 30),
        outbound_uuid=_OUTBOUND_VECTOR["uuid"],
    )
    assert handle == _OUTBOUND_VECTOR["hex"], "向量里那两种写法不是同一个值"

    async with in_a_moment("akao", now=_at(21, 35)):
        seen = await look_at_phone.invoke({"channel_id": str(_DM)})

    printed_in_snapshot = _own_line(
        Happening(
            lane=LANE,
            happening_id=f"{OUTBOUND_HAPPENING_PREFIX}{_OUTBOUND_VECTOR['hex']}",
            seq=1,
            actor="akao",
            place="家/我房间",
            kind=KIND_SPEECH,
            medium=MEDIUM_PHONE,
            content="在呢在呢",
            occurred_at=_at(21, 30),
            audience=["bezhai"],
            who_was_where={},
            channel_id=str(_DM),
        )
    )
    marker = f"［{_OUTBOUND_VECTOR['hex']}］"
    assert marker in printed_in_snapshot, (
        f"用例前提没成立：快照那段印的不是这个形状。拿到：{printed_in_snapshot}"
    )
    assert marker in seen, (
        f"会话里的编号跟快照那段对不上 —— 她会以为那是两种编号。拿到：\n{seen}"
    )


@pytest.mark.integration
async def test_the_cursor_only_moves_after_she_actually_looked(
    living_db, in_a_moment
):
    await _seed_world()
    await _incoming(_DM, text_body="在吗", at=_at(21, 30))

    assert await read_through(
        lane=LANE, persona_id="akao", channel_id=str(_DM)
    ) == NEVER_LOOKED

    async with in_a_moment("akao"):
        seen = await look_at_phone.invoke({"channel_id": str(_DM)})

    assert "在吗" in seen
    assert (
        await read_through(lane=LANE, persona_id="akao", channel_id=str(_DM))
    )[0] == _ms(_at(21, 30))


@pytest.mark.integration
async def test_a_glance_that_failed_is_not_counted_as_read(
    living_db, in_a_moment, monkeypatch
):
    """看手机这一步自己炸了 = 一条都不算已读。"""
    await _seed_world()
    await _incoming(_DM, text_body="在吗", at=_at(21, 30))

    from app.living import phone as phone_mod

    def boom(*_a, **_kw):
        raise RuntimeError("渲染这一步炸了")

    monkeypatch.setattr(phone_mod, "_glance_text", boom)

    async with in_a_moment("akao"):
        outcome = await look_at_phone.invoke({"channel_id": str(_DM)})

    assert isinstance(outcome, dict), "工具该把失败报回去，而不是假装看过了"
    assert await read_through(lane=LANE, persona_id="akao", channel_id=str(_DM)) == NEVER_LOOKED, "游标推过去了但内容没到她手上 —— 这条消息就此永久消失，而且没有任何报错"


@pytest.mark.integration
async def test_a_moment_that_never_finished_did_not_read_anything(
    living_db, in_a_moment
):
    """**看手机算不算数，绑在这一缝跑完上。**

    工具返回 ≠ 她看见了：工具结果要先进模型的上下文，这一缝才算真的把内容送到她
    眼前。中间崩掉的话，游标要是已经推过去了，那几条消息就此永久消失、而且一句
    报错都没有 —— 所以游标跟着一缝一起落库，缝没落地就一条都不算已读。
    她下一缝原样再看到（宁可重看，不可漏看）。
    """
    await _seed_world()
    await _incoming(_DM, text_body="在吗", at=_at(21, 30))

    async with in_a_moment("akao", finishes=False):
        seen = await look_at_phone.invoke({"channel_id": str(_DM)})

    assert "在吗" in seen, "工具本身该正常返回"
    assert await read_through(lane=LANE, persona_id="akao", channel_id=str(_DM)) == NEVER_LOOKED, "这一缝没跑完，游标却已经推过去了"
    assert [e.unread for e in await envelopes_for(lane=LANE, persona_id="akao")] == [1]


@pytest.mark.integration
async def test_looking_twice_in_one_moment_shows_the_same_window_and_nothing_new(
    living_db, in_a_moment
):
    """一缝里第二次打开同一条会话：**窗口内容相同**，「其中 N 条是新的」为 0。

    真人再点开一次看到的也是同样的消息 —— 内容不该消失。变的只有"新到几条"，而它按
    **本缝内待落库的游标**算（``_pending_cursor``）：第一次看已经把这条算成读过了，
    只是还没落库。这就是游标延到缝末落库的唯一代价：本缝内的"已经看过"必须自己记着。
    """
    await _seed_world()
    await _incoming(_DM, text_body="在吗", at=_at(21, 30))

    async with in_a_moment("akao"):
        first = await look_at_phone.invoke({"channel_id": str(_DM)})
        second = await look_at_phone.invoke({"channel_id": str(_DM)})

    assert "在吗" in first and "其中 1 条是新的" in first
    assert "在吗" in second, (
        f"第二次点开会话，内容凭空没了 —— 真人再点一次看到的是同样的消息。拿到：{second}"
    )
    assert "其中 0 条是新的" in second, (
        f"同一缝里第二次看，刚看过的又被算成新到的。拿到：{second}"
    )


@pytest.mark.integration
async def test_sleeping_through_it_piles_the_messages_up_unread(living_db):
    """她睡着的时候没做"看手机"这个动作，所以消息照堆、不算已读。"""
    await _seed_world()
    await _incoming(_DM, text_body="第一条", at=_at(2, 0))
    await _incoming(_DM, text_body="第二条", at=_at(3, 0))

    first = await envelopes_for(lane=LANE, persona_id="akao")
    second = await envelopes_for(lane=LANE, persona_id="akao")

    assert [e.unread for e in first] == [2]
    assert [e.unread for e in second] == [2], "什么都没做，未读却变了"


# --------------------------------------------------------------------------
# 四 · 挤出窗口的那些永久丢失
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_she_reads_the_last_few_and_the_ones_before_are_gone_for_good(
    living_db, in_a_moment
):
    """挤出窗口的那些不会补看，游标照样推到未读里最新那条。

    这是设计不是 bug —— 真人"未读 47 条"就是先看最后五到十条，能自洽就到此为止。
    改成"打开会话"之后**窗口里的东西不再消失**（下一缝点开还是那十条），真正丢的是
    被挤出窗口的那五条：它们既不在窗口里，也已经不算未读了。
    """
    await _seed_world()
    total = PHONE_GLANCE_LIMIT + 5
    for i in range(total):
        await _incoming(_DM, text_body=f"第{i}条", at=_at(20, i))

    async with in_a_moment("akao"):
        seen = await look_at_phone.invoke({"channel_id": str(_DM)})

    assert f"第{total - 1}条" in seen
    assert "第0条" not in seen, "她不该一次把 15 条全读完"
    assert "还有 5 条" in seen, f"被挤出窗口的那几条得说出来。拿到：\n{seen}"

    # 下一缝再点开：**同样那十条还在**（真人再点一次看到的就是它们），但一条新的
    # 都没有；被挤出去的那五条不会回来。
    async with in_a_moment("akao"):
        again = await look_at_phone.invoke({"channel_id": str(_DM)})
    assert f"第{total - 1}条" in again and "其中 0 条是新的" in again
    assert "第0条" not in again, "被挤出窗口的那条又冒出来了"
    assert [e.unread for e in await envelopes_for(lane=LANE, persona_id="akao")] == []


# --------------------------------------------------------------------------
# 五 · 谁在叫她 —— 提前一缝的输入（判断在 nudge 那边，这里只验事实）
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_direct_message_is_someone_waiting_for_her(living_db):
    await _seed_world()
    mid = await _incoming(_DM, text_body="在吗", at=_at(21, 30))

    summons = await newest_unread_summons(lane=LANE, persona_id="akao")

    assert summons is not None
    assert (summons.message_id, summons.channel_id) == (mid, str(_DM))


@pytest.mark.integration
async def test_being_named_in_a_group_is_someone_calling_her(living_db):
    await _seed_world()
    mid = await _incoming(
        _GROUP,
        text_body=" 这个你怎么看",
        at=_at(21, 30),
        sender=_SOMEONE,
        sender_name="路人",
        names_bot=_AKAO_BOT_UID,
    )

    summons = await newest_unread_summons(lane=LANE, persona_id="akao")

    assert summons is not None and summons.message_id == mid


@pytest.mark.integration
async def test_group_chatter_that_does_not_name_her_is_background_noise(living_db):
    await _seed_world()
    await _incoming(
        _GROUP, text_body="今天好热", at=_at(21, 30), sender=_SOMEONE,
        sender_name="路人",
    )

    assert await newest_unread_summons(lane=LANE, persona_id="akao") is None


@pytest.mark.integration
async def test_being_named_by_someone_elses_bot_is_not_her_business(living_db):
    """群里点的是姐姐的名字 —— 跟她无关。"""
    await _seed_world()
    await _incoming(
        _GROUP,
        text_body=" 你说呢",
        at=_at(21, 30),
        sender=_SOMEONE,
        sender_name="路人",
        names_bot=_AYANA_BOT_UID,
    )

    assert await newest_unread_summons(lane=LANE, persona_id="akao") is None


@pytest.mark.integration
async def test_a_group_message_nobody_scanned_does_not_call_her(living_db):
    """**没人算过 ≠ 确认没点她。**

    这一列是 NULL 的行有三种来源：加列之前的存量行、QQ 的行（那侧的投影不写这一
    列）、飞书新写入方上线之前那段时间的行。这些行里到底有没有 @ 她，库里没有答案。

    没有答案的时候不叫醒她 —— 反过来（当成点了她）会让她被一整批历史消息轮流叫起
    来。代价是那段窗口里真的 @ 了她的消息她收不到，这是明知的取舍，不是遗漏。
    """
    await _seed_world()
    await _incoming(
        _GROUP,
        text_body="@赤尾 在吗",
        at=_at(21, 30),
        sender=_SOMEONE,
        sender_name="路人",
        mention_unrecorded=True,
    )

    assert await newest_unread_summons(lane=LANE, persona_id="akao") is None


@pytest.mark.integration
async def test_a_direct_message_calls_her_even_when_nobody_scanned_it(living_db):
    """私聊不看这一列 —— 私聊来的任意一条本来就是在叫她。

    这条钉的是"NULL 不算数"那条规则**不能溢出到私聊**：真溢出的话，改动前的所有
    私聊未读会一起变成叫不动她，比它想修的问题严重得多。
    """
    await _seed_world()
    mid = await _incoming(
        _DM, text_body="在吗", at=_at(21, 30), mention_unrecorded=True
    )

    summons = await newest_unread_summons(lane=LANE, persona_id="akao")

    assert summons is not None and summons.message_id == mid


@pytest.mark.integration
async def test_naming_her_alongside_others_still_calls_her(living_db):
    """一条消息里点了好几个人，其中一个是她 —— 照样算在叫她。"""
    await _seed_world()
    mid = await _incoming(
        _GROUP,
        text_body=" 你们俩谁来",
        at=_at(21, 30),
        sender=_SOMEONE,
        sender_name="路人",
        names_bot=_AKAO_BOT_UID,
        names_others=(_AYANA_BOT_UID, _BEZHAI),
    )

    summons = await newest_unread_summons(lane=LANE, persona_id="akao")

    assert summons is not None and summons.message_id == mid


@pytest.mark.integration
async def test_a_message_she_already_read_stops_calling_her(living_db, in_a_moment):
    await _seed_world()
    await _incoming(_DM, text_body="在吗", at=_at(21, 30))

    async with in_a_moment("akao"):
        await look_at_phone.invoke({"channel_id": str(_DM)})

    assert await newest_unread_summons(lane=LANE, persona_id="akao") is None


# --------------------------------------------------------------------------
# 六 · 同一毫秒的消息不能被永久跳过
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_message_landing_in_the_same_millisecond_is_not_skipped(
    living_db, in_a_moment
):
    """游标是复合的（时刻 + 消息 id），不只是时刻。

    只用 ``event_time > 水位`` 的话，**整个那一毫秒**都被排除掉：一条跟她刚读那条
    同毫秒、但晚一步落库的消息就此永久消失，而且一句报错都没有。这跟 T1 当初用提交序
    ``seq`` 解掉的是同一个病，在手机这边不能再犯一遍。

    ``common_message_id`` 在生产里是 uuidv7（按生成时刻单调），所以"同毫秒里谁先谁后"
    有确定答案；复合游标按 ``(event_time, common_message_id)`` 字典序推进。
    """
    await _seed_world()
    same = _at(21, 30)
    first = await _incoming(_DM, text_body="第一句", at=same, message_id=_UUID7_A)

    async with in_a_moment("akao"):
        seen = await look_at_phone.invoke({"channel_id": str(_DM)})
    assert "第一句" in seen

    # 同一毫秒、晚一步落库（uuidv7 更大）。
    await _incoming(_DM, text_body="第二句", at=same, message_id=_UUID7_B)

    assert [e.unread for e in await envelopes_for(lane=LANE, persona_id="akao")] == [1]
    async with in_a_moment("akao"):
        again = await look_at_phone.invoke({"channel_id": str(_DM)})
    assert "第二句" in again, (
        f"同毫秒的那条被整段跳过了 —— 她永远看不到它。第一条是 {first}。拿到：{again}"
    )


# --------------------------------------------------------------------------
# 七 · 信封不替她裁决注意力
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_the_conversation_that_is_calling_her_is_always_in_the_envelope(
    living_db,
):
    """在叫她的那条会话**一定**在信封里，多少群在刷屏都挤不掉它。

    信封的条数上限截的是"她知不知道有这回事"，比"她看多少条内容"严重一个量级：
    被挤出去的那条，她连它存在都不知道，也就永远不会去看。所以上限只管**没在叫她的**
    那些（群里的背景噪音，本来就无上限），在叫她的一条都不许少。
    """
    from app.living.phone import ENVELOPE_LIMIT

    await _seed_world()
    # 私聊最旧 —— 按"最近有动静"排序它会被排到最后。
    await _incoming(_DM, text_body="在吗", at=_at(8, 0))
    # 一堆群在刷屏，全都比私聊新。
    noisy = await _seed_noisy_groups(ENVELOPE_LIMIT + 3, at_from=_at(21, 0))

    envelopes = await envelopes_for(lane=LANE, persona_id="akao")
    channels = [e.channel_id for e in envelopes]

    assert str(_DM) in channels, (
        f"在等她回话的那条私聊被群里的闲聊挤出了信封 —— 她连有人找过她都不知道。"
        f"信封里是：{channels}，群一共 {len(noisy)} 个"
    )


# --------------------------------------------------------------------------
# 八 · 跨夜之后，昨晚那条不能读成今晚
# --------------------------------------------------------------------------
#
# 信箱既没有时间窗也没有 TTL：她睡着的时候消息照堆、一条都不算已读（上面第三节），
# 所以昨晚积压的未读原样进这一缝。裸时分下 ``23:50`` 昨晚和今晚一个形状 —— 线上同一
# 个病炸过（2026-08-03：中午 13:18 往群里发「大半夜的发什么疯」）。出口是
# ``app.infra.cst_time.to_cst_dated``。


@pytest.mark.integration
async def test_an_overnight_pile_says_which_day_it_came_from(living_db):
    """刚过午夜那一段最容易错标：按 UTC 比是同一天，按 CST 才是昨天。

    昨晚 23:50 CST = 07-24 15:50 UTC，此刻 00:20 CST = 07-24 16:20 UTC。
    """
    await _seed_world()
    await _incoming(
        _DM, text_body="睡了没", at=_at(23, 50) - dt.timedelta(days=1)
    )

    envelope = await phone_envelope(
        lane=LANE, persona_id="akao", now=_at(0, 20)
    )

    assert "07-24 23:50" in envelope, (
        f"昨晚那条渲染成了裸时分 —— 她会当成半小时前刚发来的。拿到：\n{envelope}"
    )


@pytest.mark.integration
async def test_when_she_last_spoke_there_is_dated_across_the_night(living_db):
    """「你上次在这儿开口是什么时候」跨了夜就必须说是哪天。

    裸时分下"昨晚 21:25 说过"和"五分钟前说过"一个形状，而这条事实存在的全部意义
    就是让她分得清这两者。
    """
    await _seed_world()
    await _incoming(
        _GROUP, text_body="有人在吗", at=_at(9, 0), sender=_SOMEONE,
        sender_name="路人",
    )

    from app.living.happening import record_happening
    from app.living.records import KIND_SPEECH, MEDIUM_GROUP_CHAT

    await record_happening(
        lane=LANE,
        happening_id="own-overnight",
        actor="akao",
        place="家/我房间",
        kind=KIND_SPEECH,
        content="我在。",
        occurred_at=_at(21, 25) - dt.timedelta(days=1),
        medium=MEDIUM_GROUP_CHAT,
        channel_id=str(_GROUP),
    )

    envelope = await phone_envelope(
        lane=LANE, persona_id="akao", now=_at(9, 10)
    )

    assert "07-24 21:25" in envelope, (
        f"「你上次在这儿开口」跨了一夜却还是裸时分。拿到：\n{envelope}"
    )


@pytest.mark.integration
async def test_todays_envelope_stays_undated(living_db):
    """同一天的**刻意不带**日期：全带上会稀释掉「这条是昨天的」这个真正的信号。"""
    await _seed_world()
    await _incoming(_DM, text_body="在吗", at=_at(21, 30))

    envelope = await phone_envelope(
        lane=LANE, persona_id="akao", now=_at(21, 35)
    )

    assert "21:30 CST" in envelope
    assert "07-25 21:30" not in envelope, (
        f"当天的条目不该带日期。拿到：\n{envelope}"
    )


@pytest.mark.integration
async def test_messages_she_reads_carry_the_day_they_were_sent(
    living_db, in_a_moment
):
    """她拿起手机翻到的那几条同理 —— 昨晚发来的必须看得出是昨晚。"""
    await _seed_world()
    await _incoming(
        _DM, text_body="睡了没", at=_at(23, 50) - dt.timedelta(days=1)
    )

    async with in_a_moment("akao", now=_at(0, 20)):
        seen = await look_at_phone.invoke({"channel_id": str(_DM)})

    assert "07-24 23:50" in seen, (
        f"昨晚那条被渲染成裸时分 —— 她会照着「刚刚发来的」去回。拿到：\n{seen}"
    )


# --------------------------------------------------------------------------
# 六 · 她开口前看到的那段会话尾巴
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_the_tail_she_speaks_from_dates_what_crossed_the_night(living_db):
    """渲染措辞时看到的那段会话，跨夜同样必须说是哪天。

    这一段是她**开口前**读的最后一样东西（``mouth.send_message`` 拿它做上下文），
    ``KNOWN_TAIL_LIMIT=20`` 条、**没有时间窗**——昨晚说过的话原样躺在里面。裸时分下
    「23:50 我先睡了」和五分钟前刚说的长得一个样，她会接着一段其实已经隔夜的对话往
    下说。跟信封、跟快照是同一个坑，同一处修法。
    """
    await _seed_world()
    await _her_own(_DM, text_body="我先睡了", at=_at(23, 50) - dt.timedelta(days=1))

    known = await conversation_as_she_knows_it(
        lane=LANE, persona_id="akao", channel_id=str(_DM), now=_at(0, 20)
    )

    assert "07-24 23:50" in known, (
        f"隔夜那句渲染成了裸时分 —— 她会当成刚说完，接着往下聊。拿到：\n{known}"
    )


@pytest.mark.integration
async def test_the_tail_stays_undated_within_the_same_day(living_db):
    """同一天的不带日期：全带上等于每行都要她过滤一次冗余，反而稀释掉真正的跨天信号。"""
    await _seed_world()
    await _her_own(_DM, text_body="在的在的", at=_at(9, 30))

    known = await conversation_as_she_knows_it(
        lane=LANE, persona_id="akao", channel_id=str(_DM), now=_at(9, 40)
    )

    assert "09:30 CST" in known and "07-25" not in known, (
        f"同一天的行不该背日期。拿到：\n{known}"
    )


# --------------------------------------------------------------------------
# 九 · 同一个群里的姐妹 —— ``role`` 说不出"是谁说的"
# --------------------------------------------------------------------------
#
# 三个 persona 本来就挂在同一个飞书群里（prod ``common_bot_presence`` 实测）。她们的
# 出站落进 ``common_message`` 也是 ``role='assistant'``，跟她自己发的一模一样。所以
# 拿 ``role`` 单独判"是不是她说的"会同时错两次：
#
#   * 姐姐的话被整段排除出"未读" —— 她从手机上**永远**看不到同一个群里姐姐说了什么；
#   * 姐姐的话又被无条件塞进"她已经知道的那段"、还署上"你" —— 她开口前读到的上下文里，
#     姐姐说的话写着是她自己说的。
#
# 分得开这两者的只有 ``bot_name``（``bot_config`` 里 bot → persona 的映射）。
#
# **修完之后姐姐的群聊发言进"未读"，但一个字的召唤力都不多**：群里不点名就是背景音，
# 信封的条数上限照样管得着它。同一个屋檐下的姐妹在群里聊天，不该比陌生人更有召唤力。


@pytest.mark.integration
async def test_a_sister_speaking_in_the_same_group_is_something_she_can_see(
    living_db,
):
    """姐姐在同一个群里说的话，是她本该感知到的动静。"""
    await _seed_world()
    await _sister_said(_GROUP, text_body="今晚吃什么", at=_at(21, 30))

    envelopes = await envelopes_for(lane=LANE, persona_id="akao")

    assert [(e.channel_id, e.unread) for e in envelopes] == [(str(_GROUP), 1)], (
        "同群姐姐说的话被 role 一刀切排除出未读了 —— 她们明明在一个群里，"
        f"她却永远看不到姐姐说了什么。拿到：{envelopes}"
    )
    assert "绫奈" in envelopes[0].senders, (
        f"信封上该有说话的人是谁。拿到：{envelopes[0].senders}"
    )


@pytest.mark.integration
async def test_her_own_words_in_the_group_are_never_unread_to_her(living_db):
    """她自己发出去的那句不是"未读" —— 那是她说的话，不是动静。"""
    await _seed_world()
    await _her_own(_GROUP, text_body="我在", at=_at(21, 30))

    assert await envelopes_for(lane=LANE, persona_id="akao") == [], (
        "她自己刚说的话被算成了未读 —— 她会把自己的回声当成别人在说话"
    )


@pytest.mark.integration
async def test_a_sister_chatting_in_the_group_does_not_summon_her(living_db):
    """姐姐在群里聊天是**动静**，不是**召唤**。

    信封里看得见，但不提前把她带到那一刻 —— 群里的召唤只认 mention item 里那个
    ``bot_common_user_id``，而姐姐的出站是一条纯文本（``[{kind:'text'}]``，见
    ``lark/outbound/deliver.ts``）：**她文字里写"@赤尾"也造不出 mention item**。
    所以姐姐在群里结构上就召唤不动她。

    这条是有意的：真按"姐姐一说话就召唤"来，两个 agent 会在一个群里互相把对方叫醒，
    永远停不下来。同一个屋檐下的姐妹在群里聊天，不该比陌生人更有召唤力。
    """
    await _seed_world()
    await _sister_said(_GROUP, text_body="@赤尾 今晚吃什么", at=_at(21, 30))

    assert await newest_unread_summons(lane=LANE, persona_id="akao") is None, (
        "姐姐在群里随口一句就把她召唤过去了 —— 两个 agent 会互相叫醒，停不下来"
    )
    envelopes = await envelopes_for(lane=LANE, persona_id="akao")
    assert [(e.named_you, e.is_calling_you) for e in envelopes] == [(False, False)], (
        f"姐姐的群聊发言被当成在叫她 —— 它连信封的条数上限都挤不掉了。拿到：{envelopes}"
    )


@pytest.mark.integration
async def test_a_real_person_naming_her_in_the_group_still_calls_her(living_db):
    """群里点名照旧算叫她 —— 那条判据一个字没动，别在修未读的时候顺手改坏了。"""
    await _seed_world()
    mid = await _incoming(
        _GROUP,
        text_body=" 你说呢",
        at=_at(21, 30),
        sender=_SOMEONE,
        sender_name="路人",
        names_bot=_AKAO_BOT_UID,
    )

    summons = await newest_unread_summons(lane=LANE, persona_id="akao")

    assert summons is not None and summons.message_id == mid


@pytest.mark.integration
async def test_the_envelope_says_someone_named_her(living_db):
    """信封上那句「有人点了你的名」得真的出现。

    **这条是整个改动的另一半**，跟召唤判定分开算：把 ``_UNREAD_SUMMARY_SQL`` 的
    ``named_you`` 恒置成假，上面那批召唤用例照样全绿，而她拿起手机时看到的仍然是
    一条平平无奇的群消息 —— 原来那个 bug 就是这样活了很久的。
    """
    await _seed_world()
    await _incoming(
        _GROUP,
        text_body=" 这个你怎么看",
        at=_at(21, 30),
        sender=_SOMEONE,
        sender_name="路人",
        names_bot=_AKAO_BOT_UID,
    )

    envelopes = await envelopes_for(lane=LANE, persona_id="akao")

    assert [(e.named_you, e.is_calling_you) for e in envelopes] == [(True, True)], (
        f"群里点了她的名，信封却没认出来。拿到：{envelopes}"
    )
    assert "有人点了你的名" in render_envelopes(envelopes, now=_at(21, 31))


@pytest.mark.integration
async def test_the_envelope_does_not_claim_she_was_named_when_nobody_scanned(living_db):
    """信封上的「有人点了你的名」同样受 NULL 约束。

    ``BOOL_OR`` 在整批行都是 NULL 时返回的是 NULL、不是 false，靠 Python 那侧
    ``bool(None)`` 才收成假。这条钉的就是那一步 —— 少了它，信封会把"没人算过"
    当成一个真值展示出去，她会拿着一条不存在的点名去翻会话。
    """
    await _seed_world()
    await _incoming(
        _GROUP,
        text_body="@赤尾 在吗",
        at=_at(21, 30),
        sender=_SOMEONE,
        sender_name="路人",
        mention_unrecorded=True,
    )

    envelopes = await envelopes_for(lane=LANE, persona_id="akao")

    assert [(e.named_you, e.is_calling_you) for e in envelopes] == [(False, False)], (
        f"没人算过这条消息，信封却说她被点名了。拿到：{envelopes}"
    )


@pytest.mark.integration
async def test_a_sister_word_she_has_not_read_is_not_something_she_knows(living_db):
    """没看过就是没看过，姐姐的话也一样。

    ``_KNOWN_SQL`` 让 ``role='assistant'`` 无条件绕过已读游标 —— 那是给**她自己**
    说过的话留的门（她当然知道自己说过什么），姐姐的话从这道门溜进来就是白送未读内容：
    她会在措辞里回应一句自己根本没读过的话。
    """
    await _seed_world()
    await _sister_said(_GROUP, text_body="今晚吃什么", at=_at(21, 30))

    known = await conversation_as_she_knows_it(
        lane=LANE, persona_id="akao", channel_id=str(_GROUP), now=_at(21, 35)
    )

    assert "今晚吃什么" not in known, (
        f"姐姐没被她读过的话绕过了游标，直接进了她开口前的上下文。拿到：\n{known}"
    )


@pytest.mark.integration
async def test_a_sister_word_she_did_read_is_attributed_to_the_sister(
    living_db, in_a_moment
):
    """她看过之后那句在上下文里，署的是**姐姐的名字**，不是"你"。

    这是前六次那个病的镜像版：之前是"她把自己的回声当成别人在说话"，这次是"她把姐姐
    的话当成自己说的"。
    """
    await _seed_world()
    await _sister_said(_GROUP, text_body="今晚吃什么", at=_at(21, 30))

    async with in_a_moment("akao"):
        seen = await look_at_phone.invoke({"channel_id": str(_GROUP)})
    assert "今晚吃什么" in seen, f"拿起手机也看不到姐姐说了什么。拿到：{seen}"

    known = await conversation_as_she_knows_it(
        lane=LANE, persona_id="akao", channel_id=str(_GROUP), now=_at(21, 35)
    )
    assert "绫奈：今晚吃什么" in known, (
        f"姐姐说的话在她开口前的上下文里署成了「你」—— 她会以为那是自己说的。"
        f"拿到：\n{known}"
    )
    assert "你：今晚吃什么" not in known


@pytest.mark.integration
async def test_her_own_words_stay_hers_in_what_she_knows(living_db):
    """她自己那句照旧无条件在她已知范围内，署"你"。"""
    await _seed_world()
    await _her_own(_GROUP, text_body="我在", at=_at(21, 30))

    known = await conversation_as_she_knows_it(
        lane=LANE, persona_id="akao", channel_id=str(_GROUP), now=_at(21, 35)
    )

    assert "你：我在" in known, f"她自己说过的话认不出来了。拿到：\n{known}"


@pytest.mark.integration
async def test_the_skipped_count_counts_the_sisters_words_too(
    living_db, in_a_moment
):
    """"前面还有 N 条你没往回翻"这个数也得把姐姐的话算进去。

    未读的口径只有一处才对：信封、看手机那一眼、跳过多少条，三处必须用同一条判据，
    不然她看到的数跟她读到的东西对不上。
    """
    await _seed_world()
    total = PHONE_GLANCE_LIMIT + 3
    for i in range(total):
        await _sister_said(_GROUP, text_body=f"姐姐第{i}句", at=_at(20, i))

    async with in_a_moment("akao"):
        seen = await look_at_phone.invoke({"channel_id": str(_GROUP)})

    assert "还有 3 条" in seen, f"跳过多少条算错了。拿到：\n{seen}"


@pytest.mark.integration
async def test_the_address_sits_next_to_the_name_it_belongs_to(living_db):
    """会话的地址要跟它的名字挨着，不能甩在一行末尾。

    实测（coe-living，2026-08-31 20:21）：信封把标题摆在最显眼处、``channel_id``
    挂在一长串属性的最后，她于是拿人名去调 ``look_at_phone``，被 fail-loud 顶回来，
    白费一缝。**这不是她的错**——在她的认知里那条私聊就叫那个名字，uuid 是工程
    产物；工具描述再喊「照抄别自己编」也是在跟这个错位对抗。名字和地址绑在一起，
    她要指哪条会话时才有一个完整的东西可指。
    """
    await _seed_world()
    await _incoming(_DM, text_body="在吗", at=_at(20, 0))

    envelope = await phone_envelope(lane=LANE, persona_id="akao", now=_at(20, 5))

    line = next(ln for ln in envelope.splitlines() if "bezhai" in ln)
    name_at = line.index("「bezhai」")
    id_at = line.index(f"channel_id={_DM}")
    assert id_at - name_at < 60, (
        f"地址离名字太远，她会拿名字当地址用。这一行是：\n{line}"
    )


# --------------------------------------------------------------------------
# 找人 —— 读完了不等于这个人不存在
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_conversation_with_nothing_unread_can_still_be_found_by_name(
    living_db, in_a_moment
):
    """读完最后一条之后，她还能不能找回这个人。

    实测（coe-living，2026-09-01）：她挂了 13 小时的一条 LooseEnd 写着「想回他但
    手机上找不到他会话，等下次有信封再试」。信封只列有未读的会话
    （:func:`envelopes_for` 见到 ``unread=0`` 就 continue），而 ``look_at_phone`` /
    ``send_message`` 都只认信封上那串 channel_id ——**她读完的那一刻，那个人从她
    世界里消失了**，于是主动发起一次对话在这个引擎里根本不可能发生。
    """
    await _seed_world()
    await _incoming(_DM, text_body="在吗", at=_at(20, 0))
    async with in_a_moment("akao"):
        await look_at_phone.invoke({"channel_id": str(_DM)})  # 读完，未读归零

    assert await envelopes_for(lane=LANE, persona_id="akao") == [], (
        "前提没成立：这条会话该已经从信封里消失了"
    )

    async with in_a_moment("akao"):
        found = await look_up_contact.invoke({"name": "bezhai"})

    assert str(_DM) in found, f"零未读就找不回这个人。拿到：\n{found}"


@pytest.mark.integration
async def test_a_looked_up_address_sits_next_to_the_name(living_db, in_a_moment):
    """查出来的地址要跟名字挨着 —— 跟信封同一条教训，见上面那个用例。"""
    await _seed_world()
    await _incoming(_DM, text_body="在吗", at=_at(20, 0))

    async with in_a_moment("akao"):
        found = await look_up_contact.invoke({"name": "bezhai"})

    line = next(ln for ln in found.splitlines() if str(_DM) in ln)
    assert line.index(f"channel_id={_DM}") - line.index("bezhai") < 60, (
        f"地址离名字太远，她会拿名字当地址用。这一行是：\n{line}"
    )


@pytest.mark.integration
async def test_a_group_is_found_by_its_own_name(living_db, in_a_moment):
    """群按群名找得到（群会话有标题，私聊多半没有，两条路都得走通）。"""
    await _seed_world()
    await _incoming(_GROUP, text_body="今天几点", at=_at(20, 0))

    async with in_a_moment("akao"):
        found = await look_up_contact.invoke({"name": "宅居研究所"})

    assert str(_GROUP) in found, f"群名查不到。拿到：\n{found}"


@pytest.mark.integration
async def test_a_conversation_that_is_not_on_her_phone_is_not_found(
    living_db, in_a_moment
):
    """别人的私聊线不在她手机上，按名字也不该冒出来。

    ``_OTHERS_DM`` 是同一个人跟**绫奈的 bot** 的私聊。查得到就等于给了她一个
    发出去会串人设身份的地址。
    """
    await _seed_world()
    await _incoming(_OTHERS_DM, text_body="绫奈在吗", at=_at(20, 0))

    async with in_a_moment("akao"):
        found = await look_up_contact.invoke({"name": "bezhai"})

    assert str(_OTHERS_DM) not in found, (
        f"把别人的私聊线给她了。拿到：\n{found}"
    )


@pytest.mark.integration
async def test_a_name_that_matches_nothing_says_so_without_telling_her_what_to_do(
    living_db, in_a_moment
):
    """查不到就说查不到。下一步做什么是她的判断，工具不替她安排。"""
    await _seed_world()
    await _incoming(_DM, text_body="在吗", at=_at(20, 0))

    async with in_a_moment("akao"):
        found = await look_up_contact.invoke({"name": "查无此人"})

    assert "查无此人" in found
    assert not any(s in found for s in ("再试", "换个", "或者算了")), (
        f"工具在指挥她下一步该干嘛：{found!r}"
    )


@pytest.mark.integration
async def test_every_match_is_listed_without_picking_one(living_db, in_a_moment):
    """重名全列出来交给她挑：不排序、不筛、不取第一个。"""
    await _seed_world()
    await _incoming(_DM, text_body="在吗", at=_at(20, 0))
    await _incoming(_GROUP, text_body="在吗", at=_at(20, 1))

    async with in_a_moment("akao"):
        found = await look_up_contact.invoke({"name": "bezhai"})

    assert str(_DM) in found and str(_GROUP) in found, (
        f"两条都该在（私聊本身 + 他说过话的群）。拿到：\n{found}"
    )


@pytest.mark.integration
async def test_finding_someone_is_one_of_the_hands_she_actually_has(living_db):
    """这只手要真在她的工具集里。

    没注册是**静默失败**：代码写好了、测试也绿，但她那一缝的工具列表里没有它，
    于是永远不会调——症状跟"零未读就找不回这个人"一模一样，而且更难查。
    """
    from app.living.moment import MOMENT_TOOLS

    assert look_up_contact in MOMENT_TOOLS, "她手里没有这只手"


# --------------------------------------------------------------------------
# 十 · 别人发来的东西，她得看得出那是什么
# --------------------------------------------------------------------------
#
# ``content_text`` 不是正文，是**投影层拼给人扫一眼的摘要**：文本项原样，其余每一项
# 一律拼成字面的 ``[kind]``（lark-service ``inbound-projection.ts`` 的 ``summarize``、
# channel-server ``common-projector.ts`` 的 ``textProjection``）。所以一条文件消息的
# ``content_text`` 就是 ``"[file]"`` —— 优先信它，等于永远不看 items 里的
# ``meta.file_name``。
#
# 实测（coe-living，2026-09-02 22:27）：她看到「某某：[file]」，只知道有个东西、
# 不知道是什么，于是回了一句「发来看看」—— 而那个文件早就发过来了。
#
# 渲染口径跟聊天那条路（``app.chat.content_parser`` 的 ``ParsedContent.render``）
# 对齐，但两边各写各的：living 是独立一层。

# 飞书文件消息的真实形状（lark-service ``inbound-message.ts`` 的 toContentItem）。
_FILE_ITEM = {
    "kind": "file",
    "key": "file_v3_0d1a",
    "meta": {"file_name": "三体.epub", "lark_type": "file"},
}


@pytest.mark.integration
async def test_a_file_someone_sent_carries_its_name(living_db, in_a_moment):
    """别人发来一个文件，她该看得见它叫什么。"""
    await _seed_world()
    await _incoming(_DM, at=_at(22, 27), items=[_FILE_ITEM], content_text="[file]")

    async with in_a_moment("akao", now=_at(22, 30)):
        seen = await look_at_phone.invoke({"channel_id": str(_DM)})

    assert "[文件: 三体.epub]" in seen, (
        f"她只知道有个附件、不知道是什么，于是回一句「发来看看」，"
        f"而那东西早就发过来了。拿到：\n{seen}"
    )
    assert "[file]" not in seen, f"渠道内部的类型名摆到了她眼前。拿到：\n{seen}"


@pytest.mark.integration
@pytest.mark.parametrize("content_text", ["看看这个[file]", "看看这个"])
async def test_a_note_that_comes_with_a_file_shows_both(
    living_db, in_a_moment, content_text
):
    """一条消息同时带文字和附件，两样都得在，顺序跟 items 一致。

    先信 ``content_text`` 的话，附件在她眼里**整个不存在**：她照着那段文字回，
    完全不知道对方还发了个东西过来。

    两种 ``content_text`` 都摆一遍 —— 投影层今天写的是 ``看看这个[file]``，库里
    也见过只剩那段文字的。**哪一种都不该改变她看到什么**：正文以 items 为准，
    这一列只是兜底。
    """
    await _seed_world()
    await _incoming(
        _DM,
        at=_at(22, 27),
        items=[{"kind": "text", "text": "看看这个"}, _FILE_ITEM],
        content_text=content_text,
    )

    async with in_a_moment("akao", now=_at(22, 30)):
        seen = await look_at_phone.invoke({"channel_id": str(_DM)})

    assert "看看这个[文件: 三体.epub]" in seen, (
        f"文字和附件不是二选一，她两样都收到了。拿到：\n{seen}"
    )


@pytest.mark.integration
async def test_a_picture_and_a_sticker_read_as_themselves(living_db, in_a_moment):
    """图片和表情包在她眼里是「图片」「表情包」，不是 ``[image]``、``[sticker]``。

    这两类占了她手机上绝大多数的非文本消息（prod 近两天 image 1081、sticker 725）。
    摆一个渠道内部的类型名给她，她要多绕一道才认得出那是什么东西。
    """
    await _seed_world()
    await _incoming(
        _DM,
        at=_at(22, 20),
        items=[{"kind": "image", "key": "img_v3_aa"}],
        content_text="[image]",
    )
    await _incoming(
        _DM,
        at=_at(22, 21),
        items=[{"kind": "sticker", "key": "stk_bb"}],
        content_text="[sticker]",
    )

    async with in_a_moment("akao", now=_at(22, 30)):
        seen = await look_at_phone.invoke({"channel_id": str(_DM)})

    assert "[图片]" in seen and "[表情包]" in seen, f"拿到：\n{seen}"
    assert "[image]" not in seen and "[sticker]" not in seen, f"拿到：\n{seen}"


@pytest.mark.integration
async def test_a_kind_this_channel_does_not_render_keeps_its_placeholder(
    living_db, in_a_moment
):
    """``unsupported`` 项带的是给人看的中文占位串，不能被换成类型名。

    这是改成"以 items 为准"最容易顺手弄坏的一处：``unsupported`` 要是落进"不认识
    的 kind"那一档，她看到的就从「[合并转发]」退成「[unsupported]」—— 比改之前
    还糟（原来这一档正是 ``content_text`` 兜住的）。占位串由投影层写死
    （lark-service ``parse-message.ts``），是线上历史的一部分。
    """
    await _seed_world()
    await _incoming(
        _DM,
        at=_at(22, 27),
        items=[
            {
                "kind": "unsupported",
                "text": "[合并转发]",
                "meta": {"original_type": "merge_forward"},
            }
        ],
        content_text="[合并转发]",
    )

    async with in_a_moment("akao", now=_at(22, 30)):
        seen = await look_at_phone.invoke({"channel_id": str(_DM)})

    assert "[合并转发]" in seen and "[unsupported]" not in seen, f"拿到：\n{seen}"


@pytest.mark.integration
async def test_the_older_type_value_shape_still_reads(living_db, in_a_moment):
    """少数历史行用 ``type``/``value`` 而不是 ``kind`` —— 两套都得继续认。

    prod 近两天：``kind`` 那套 text 13528 / image 1081 / sticker 725 /
    unsupported 94 / file 16，``type`` 那套 image 17 / text 5。后者条数少，但她读到
    的是同一条会话，漏认就是中间凭空少一句。
    """
    await _seed_world()
    await _incoming(
        _DM,
        at=_at(22, 27),
        items=[
            {"type": "text", "value": "旧消息"},
            {"type": "image", "value": "img_old"},
        ],
        content_text="旧消息[image]",
    )

    async with in_a_moment("akao", now=_at(22, 30)):
        seen = await look_at_phone.invoke({"channel_id": str(_DM)})

    assert "旧消息[图片]" in seen, f"拿到：\n{seen}"


@pytest.mark.integration
async def test_the_tail_she_speaks_from_carries_the_file_name_too(
    living_db, in_a_moment
):
    """她开口前读的那段会话尾巴同样要带文件名。

    看手机和渲染措辞是两个读取方、同一份正文。只修一边的话，她在手机上看见
    「[文件: 三体.epub]」、转头开口时上下文里又变回「[file]」—— 同一个东西在
    一缝之内长了两副样子，她会当成两件事。
    """
    await _seed_world()
    await _incoming(_DM, at=_at(22, 27), items=[_FILE_ITEM], content_text="[file]")

    async with in_a_moment("akao", now=_at(22, 30)):
        await look_at_phone.invoke({"channel_id": str(_DM)})  # 读过了才进她已知范围

    known = await conversation_as_she_knows_it(
        lane=LANE, persona_id="akao", channel_id=str(_DM), now=_at(22, 30)
    )

    assert "[文件: 三体.epub]" in known, f"拿到：\n{known}"


# --------------------------------------------------------------------------
# 十一 · 撤掉的那条不在会话里了
# --------------------------------------------------------------------------
#
# 撤回不删 ``common_message`` 那一行（公共层是消息记录，删行会打断历史），撤成功
# 只在 ``recalled_at`` 上留个时刻。所以**读的一侧不管，她就会原样看见一条自己明明
# 撤掉了的话** —— 然后接着它往下说，而对面早就看不到那句了。
#
# 判据写在**这一列的含义**上（这一行在渠道上已经不在了），不写在谁撤的它上面：她自己
# 撤的、同群姐姐撤的都是同一件事。今天填这一列的只有投递侧（撤的是 bot 自己发的），
# 但这条规则不依赖那个事实。
#
# **只有"打开会话"那一处例外，而且只对她自己撤掉的那条。** 她撤完之后不知道自己撤了
# 什么（coe-living 实证：撤完 8 分钟还在问主人撤了啥），原样显示会让她接着一句对面看
# 不到的话往下说，留白洞等于没修 —— 所以留痕迹并带原话，那正是真实的信息状态：她自己
# 知道撤了什么（真人能点开重新编辑），对面不知道内容但知道有这么回事。
#
# 措辞只说得出口的那件事：**这条消息已经撤回了**。不说"你撤回了" —— 群主和管理员也
# 撤得掉她的消息，而撤回这件事在库里只有一个时刻、没有操作者。
#
# 别人撤掉的仍然一处都不显示：真人那侧看到的是"XX 撤回了一条消息"，内容确实没了。


@pytest.mark.integration
async def test_a_message_she_took_back_is_gone_from_the_tail_she_speaks_from(
    living_db,
):
    """她自己撤掉的那句，不在她开口前读的那段会话里。

    这是最要命的一处：``conversation_as_she_knows_it`` 是嘴渲染措辞前读的最后一样
    东西（``app.living.mouth.send_message``）。撤掉的那句留在里面，她就会接着一句
    对面根本看不到的话往下说。
    """
    await _seed_world()
    took_back = await _her_own(_DM, text_body="那家店周一不开", at=_at(21, 30))
    await _her_own(_DM, text_body="明天见", at=_at(21, 32))
    await _recalled_on_the_channel(took_back, at=_at(21, 31))

    known = await conversation_as_she_knows_it(
        lane=LANE, persona_id="akao", channel_id=str(_DM), now=_at(21, 35)
    )

    assert "那家店周一不开" not in known, (
        f"她撤掉的那句还在她开口前的上下文里 —— 她会接着一句对面看不到的话说下去。"
        f"拿到：\n{known}"
    )
    assert "你：明天见" in known, (
        f"撤一句把她别的话也一起拿掉了。拿到：\n{known}"
    )


@pytest.mark.integration
async def test_a_message_she_took_back_leaves_a_trace_carrying_what_it_said(
    living_db, in_a_moment
):
    """她自己撤掉的那条，在她打开的会话里留下痕迹**并带着原话**。

    实证（coe-living，2026-09-04）：她撤完 8 分钟后还在问主人"你刚才到底发了啥"——
    她的记忆里只有"我去撤了那句"这个行为，会话里那句话已经消失，于是她把撤回这件事
    安在了主人身上。原话在这里是必要的：她本人确实知道自己撤了什么（真人能点开重新
    编辑），对面不知道内容但知道有这么回事。
    """
    await _seed_world()
    await _incoming(_DM, text_body="你现在能撤回飞书消息没", at=_at(14, 50))
    took_back = await _her_own(
        _DM, text_body="所以主人是发了什么见不得人的东西想撤回吗", at=_at(14, 50, 30)
    )
    await _recalled_on_the_channel(took_back, at=_at(14, 50, 54))
    await _incoming(_DM, text_body="还真的能撤回啊", at=_at(14, 58))

    async with in_a_moment("akao", now=_at(14, 59)):
        seen = await look_at_phone.invoke({"channel_id": str(_DM)})

    assert "所以主人是发了什么见不得人的东西想撤回吗" in seen, (
        f"她撤掉的那句在她眼前是个白洞 —— 她不知道自己撤了什么。拿到：\n{seen}"
    )
    assert "这条消息已经撤回了" in seen, (
        f"原样显示的话，她会接着一句对面根本看不到的话往下说。拿到：\n{seen}"
    )
    assert "你撤回" not in seen, (
        f"库里只有撤回的时刻、没有操作者：群主和管理员也撤得掉她的消息，"
        f"「你撤回了」是句证明不了的话。拿到：\n{seen}"
    )


@pytest.mark.integration
async def test_a_message_already_taken_back_does_not_offer_a_handle(
    living_db, in_a_moment
):
    """已经撤掉的那条不再带编号 —— 它已经不是"能撤的"了。

    留着编号等于同时告诉她"这条撤回了"和"拿这串去撤它"，而她照着再撤一次只会撤了个空。
    """
    await _seed_world()
    took_back, handle = await _her_own_proactive(
        _DM, text_body="那家店周一不开", at=_at(21, 30)
    )
    await _recalled_on_the_channel(took_back, at=_at(21, 31))

    async with in_a_moment("akao", now=_at(21, 35)):
        seen = await look_at_phone.invoke({"channel_id": str(_DM)})

    assert "这条消息已经撤回了" in seen, f"拿到：\n{seen}"
    assert handle not in seen, (
        f"撤掉的那条还挂着可撤的编号 —— 她照它再撤一次只会撤了个空。拿到：\n{seen}"
    )


@pytest.mark.integration
async def test_what_someone_else_took_back_is_not_in_the_window_either(
    living_db, in_a_moment
):
    """别人（真人、姐姐）撤掉的消息，她打开会话时一条都看不到。

    真人那侧看到的是"XX 撤回了一条消息"，内容确实没了。留一条带原话的痕迹给她，就是
    让她看到的会话跟对面看到的不是同一个。
    """
    await _seed_world()
    his = await _incoming(
        _GROUP,
        text_body="这个别说出去",
        at=_at(21, 30),
        sender=_SOMEONE,
        sender_name="路人",
    )
    hers = await _sister_said(_GROUP, text_body="我也撤一条", at=_at(21, 31))
    await _incoming(
        _GROUP,
        text_body="刚才那条你们看到了吗",
        at=_at(21, 32),
        sender=_SOMEONE,
        sender_name="路人",
    )
    await _recalled_on_the_channel(his, at=_at(21, 33))
    await _recalled_on_the_channel(hers, at=_at(21, 33))

    async with in_a_moment("akao", now=_at(21, 35)):
        seen = await look_at_phone.invoke({"channel_id": str(_GROUP)})

    assert "这个别说出去" not in seen and "我也撤一条" not in seen, (
        f"别人撤掉的消息还在她眼前 —— 她看到的会话跟对面看到的不是同一个。拿到：\n{seen}"
    )
    assert "刚才那条你们看到了吗" in seen, f"没撤的那条也一起没了。拿到：\n{seen}"


@pytest.mark.integration
async def test_a_sister_word_taken_back_is_gone_from_what_she_knows_too(
    living_db, in_a_moment
):
    """姐姐撤掉的那句同样不在 —— 哪怕她之前已经读过。

    判据是"这一行在渠道上还在不在"，不是"谁撤的"。她读过之后那句本来会绕过游标
    进入她已知的那段（前一节那批用例），撤掉之后不该再进。
    """
    await _seed_world()
    took_back = await _sister_said(_GROUP, text_body="今晚吃火锅", at=_at(21, 30))
    await _sister_said(_GROUP, text_body="七点楼下集合", at=_at(21, 31))

    async with in_a_moment("akao"):
        await look_at_phone.invoke({"channel_id": str(_GROUP)})  # 两条都读过了
    await _recalled_on_the_channel(took_back, at=_at(21, 32))

    known = await conversation_as_she_knows_it(
        lane=LANE, persona_id="akao", channel_id=str(_GROUP), now=_at(21, 35)
    )

    assert "今晚吃火锅" not in known, (
        f"姐姐撤掉的那句还在她已知的那段里。拿到：\n{known}"
    )
    assert "绫奈：七点楼下集合" in known, f"没撤的那句也一起没了。拿到：\n{known}"


@pytest.mark.integration
async def test_a_message_taken_back_before_she_looked_is_not_there_to_open(
    living_db, in_a_moment
):
    """撤在她看之前 —— 信封上不算动静，拿起手机也没有它。

    信封那一处和看手机那一眼必须同一条判据：信封说有一条、翻开却什么都没有，
    她只会以为自己漏看了。
    """
    await _seed_world()
    took_back = await _sister_said(_GROUP, text_body="今晚吃火锅", at=_at(21, 30))

    before = await envelopes_for(lane=LANE, persona_id="akao")
    assert [(e.channel_id, e.unread) for e in before] == [(str(_GROUP), 1)], (
        f"用例前提就没成立：撤回之前这条本该是一条未读。拿到：{before}"
    )

    await _recalled_on_the_channel(took_back, at=_at(21, 31))

    assert await envelopes_for(lane=LANE, persona_id="akao") == [], (
        "撤掉的那条还在信封上算一条动静 —— 她会为一条不存在的消息拿起手机"
    )
    async with in_a_moment("akao"):
        seen = await look_at_phone.invoke({"channel_id": str(_GROUP)})
    assert "今晚吃火锅" not in seen, f"翻开会话还能看见撤掉的那条。拿到：\n{seen}"


@pytest.mark.integration
async def test_the_envelope_does_not_name_someone_whose_only_word_was_taken_back(
    living_db,
):
    """信封上点的名字里，没有"只说过一句、而且撤掉了"的那个人。

    信封上那几个名字是她判断"这条会话值不值得翻开"的依据。摆一个撤掉了的人在那儿，
    她翻开会话根本找不到那个人说了什么。
    """
    await _seed_world()
    took_back = await _sister_said(_GROUP, text_body="今晚吃火锅", at=_at(21, 30))
    await _incoming(
        _GROUP,
        text_body="我也去",
        at=_at(21, 31),
        sender=_SOMEONE,
        sender_name="路人",
    )
    await _recalled_on_the_channel(took_back, at=_at(21, 32))

    envelopes = await envelopes_for(lane=LANE, persona_id="akao")

    assert [(e.unread, e.senders) for e in envelopes] == [(1, ("路人",))], (
        f"信封上还点着一个只说过一句、而且已经撤掉了的人。拿到：{envelopes}"
    )


@pytest.mark.integration
async def test_the_skipped_count_does_not_count_a_message_taken_back(
    living_db, in_a_moment
):
    """"前面还有 N 条你没往回翻"里不算撤掉的那些。

    未读的口径只有一处才对：信封、看手机那一眼、跳过多少条，三处一分家，她看到的
    数就跟她读到的东西对不上。
    """
    await _seed_world()
    for i in range(PHONE_GLANCE_LIMIT + 3):
        mid = await _sister_said(_GROUP, text_body=f"姐姐第{i}句", at=_at(20, i))
        if i == 0:
            await _recalled_on_the_channel(mid, at=_at(21, 0))

    async with in_a_moment("akao"):
        seen = await look_at_phone.invoke({"channel_id": str(_GROUP)})

    assert "还有 2 条" in seen, (
        f"跳过多少条把撤掉的那条也算进去了 —— 这个数跟她真能翻到的东西对不上。"
        f"拿到：\n{seen}"
    )


@pytest.mark.integration
async def test_a_message_taken_back_stops_calling_her(living_db):
    """撤掉的那条不再叫她。

    真人在私聊里发一句又撤回，对面就不该再被这句话叫过去 —— 那句话已经不在会话里
    了。判据同样只看这一列：``recalled_at`` 非空 = 渠道上它不在了。
    """
    await _seed_world()
    mid = await _incoming(_DM, text_body="在吗", at=_at(21, 30))

    before = await newest_unread_summons(lane=LANE, persona_id="akao")
    assert before is not None and before.message_id == mid, (
        f"用例前提就没成立：撤回之前这条私聊本该在叫她。拿到：{before}"
    )

    await _recalled_on_the_channel(mid, at=_at(21, 31))

    assert await newest_unread_summons(lane=LANE, persona_id="akao") is None, (
        "撤掉的那条还在叫她 —— 她会被提前带到一刻，为一句已经不在的话"
    )


@pytest.mark.integration
async def test_a_name_only_seen_in_a_taken_back_message_is_not_found(
    living_db, in_a_moment
):
    """按名字找回会话时，撤掉的那条不算"这个人在里面说过话"。

    这只手是拿 ``sender_display_name`` 在她见过的消息里搜的。撤掉的那条在渠道上已经
    不在了，拿它把一条会话搜出来，等于让她按一句不存在的话去找人。
    """
    await _seed_world()
    took_back = await _sister_said(_GROUP, text_body="今晚吃火锅", at=_at(21, 30))

    async with in_a_moment("akao"):
        before = await look_up_contact.invoke({"name": "绫奈"})
    assert str(_GROUP) in before, (
        f"用例前提就没成立：撤回之前该按姐姐的名字搜得到这个群。拿到：\n{before}"
    )

    await _recalled_on_the_channel(took_back, at=_at(21, 31))

    async with in_a_moment("akao"):
        after = await look_up_contact.invoke({"name": "绫奈"})
    assert str(_GROUP) not in after, (
        f"撤掉的那条还把这个群摆进了搜索结果。拿到：\n{after}"
    )
