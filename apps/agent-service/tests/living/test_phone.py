"""手机 —— 信封可感，内容要她去看。

四条硬边界，各有对应的用例：

  * **每一缝拿到的只有信封。** 有没有动静、谁、多密多快、跟她刚才干的事有没有牵连。
    正文一个字都不在信封里 —— 不然"看手机"就成了摆设，她躺着就把消息读完了。
  * **看手机是她的动作，成功返回之后才推游标。** 中途炸掉 = 一条都不算已读。
  * **跳过的永久丢失。** 她只看最后几条，中间的不会补看：真人"未读 47 条"就是这样。
  * **睡觉时消息照堆、不算已读。** 她没做这个动作，游标就不动。
"""
from __future__ import annotations

import datetime as dt
import json
import uuid

import pytest
from sqlalchemy import text

from app.data import session as session_mod
from app.living.phone import (
    NEVER_LOOKED,
    PHONE_GLANCE_LIMIT,
    conversation_as_she_knows_it,
    envelopes_for,
    look_at_phone,
    newest_unread_summons,
    phone_envelope,
    reachable_conversations,
    read_through,
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
    text_body: str,
    at: dt.datetime,
    sender: uuid.UUID = _BEZHAI,
    sender_name: str = "bezhai",
    names_bot: uuid.UUID | None = None,
    scope: str | None = None,
    bot_name: str = "chiwei",
    message_id: uuid.UUID | None = None,
) -> str:
    """真人发来的一条消息。``names_bot`` 不为空 = 这条 @ 了那个 bot。"""
    items: list[dict] = []
    if names_bot is not None:
        items.append(
            {
                "type": "mention",
                "value": "赤尾",
                "meta": {"bot_common_user_id": str(names_bot)},
            }
        )
    items.append({"type": "text", "value": text_body})
    mid = message_id or uuid.uuid4()
    resolved_scope = scope or ("direct" if conv in (_DM, _OTHERS_DM) else "group")
    async with session_mod.get_session() as s:
        await s.execute(
            text(
                "INSERT INTO common_message "
                "(common_message_id, channel, common_conversation_id, common_user_id,"
                " sender_display_name, role, content, content_text, scope, bot_name,"
                " event_time) "
                "VALUES (CAST(:m AS uuid), 'lark', CAST(:c AS uuid), CAST(:u AS uuid),"
                " :sn, 'user', CAST(:body AS jsonb), :txt, :sc, :bn, :et)"
            ),
            {
                "m": str(mid),
                "c": str(conv),
                "u": str(sender),
                "sn": sender_name,
                "body": json.dumps(items, ensure_ascii=False),
                "txt": text_body,
                "sc": resolved_scope,
                "bn": bot_name,
                "et": _ms(at),
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
) -> str:
    """某个 bot 在这条会话里说过的一句（``role='assistant'``）。

    形状照真的出站那一处写（``apps/lark-service/src/lark/outbound/deliver.ts``）：
    content item 是 ``kind``/``text``、``common_user_id`` 和 ``sender_display_name``
    都是那个 bot 的、``bot_name`` 是发这句话的 bot。

    **``role`` 只说明"这是某个 bot 发的"，说不出是哪个。** 三姐妹在同一个群里，
    她们的出站落在这张表里长得一模一样 —— 分得开的只有 ``bot_name``。
    """
    mid = uuid.uuid4()
    resolved_scope = "direct" if conv in (_DM, _OTHERS_DM) else "group"
    async with session_mod.get_session() as s:
        await s.execute(
            text(
                "INSERT INTO common_message "
                "(common_message_id, channel, common_conversation_id, common_user_id,"
                " sender_display_name, role, content, content_text, scope, bot_name,"
                " event_time) "
                "VALUES (CAST(:m AS uuid), 'lark', CAST(:c AS uuid), CAST(:u AS uuid),"
                " :sn, 'assistant', CAST(:body AS jsonb), :txt, :sc, :bn, :et)"
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
            },
        )
    return str(mid)


async def _her_own(conv: uuid.UUID, *, text_body: str, at: dt.datetime) -> str:
    """她自己在这条会话里说过的一句（她的 bot 是 ``chiwei``）。"""
    return await _bot_said(
        conv,
        text_body=text_body,
        at=at,
        bot_name="chiwei",
        bot_uid=_AKAO_BOT_UID,
        display_name="赤尾",
    )


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
# 三 · 游标只在看手机成功之后推进
# --------------------------------------------------------------------------


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
async def test_looking_twice_in_one_moment_does_not_show_the_same_batch_again(
    living_db, in_a_moment
):
    """一缝里看两次，第二次不该把刚看过的再摆一遍。

    游标延到缝末落库之后，这一条就是它唯一的代价：本缝内的"已经看过"必须自己记着。
    """
    await _seed_world()
    await _incoming(_DM, text_body="在吗", at=_at(21, 30))

    async with in_a_moment("akao"):
        first = await look_at_phone.invoke({"channel_id": str(_DM)})
        second = await look_at_phone.invoke({"channel_id": str(_DM)})

    assert "在吗" in first
    assert "在吗" not in second, f"同一批消息在一缝里被摆了两遍：{second}"


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
# 四 · 跳过的永久丢失
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_she_reads_the_last_few_and_the_middle_is_gone_for_good(
    living_db, in_a_moment
):
    await _seed_world()
    total = PHONE_GLANCE_LIMIT + 5
    for i in range(total):
        await _incoming(_DM, text_body=f"第{i}条", at=_at(20, i))

    async with in_a_moment("akao"):
        seen = await look_at_phone.invoke({"channel_id": str(_DM)})

    assert f"第{total - 1}条" in seen
    assert "第0条" not in seen, "她不该一次把 15 条全读完"

    # 中间那些不会补看：游标已经推到最新那条。
    async with in_a_moment("akao"):
        again = await look_at_phone.invoke({"channel_id": str(_DM)})
    assert "第0条" not in again and f"第{total - 1}条" not in again
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
