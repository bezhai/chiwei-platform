"""她那次开口落地成了公共层的哪一行 —— 事后对账，不是投递回执。

投递方在落 assistant 行的时候把"这是她哪一次开口"写进
``common_message.agent_outbound_id``。引擎这边不在出站链路上等回执（那会把"她说话"
和"渠道写库"绑成一次同步往返，broker 一慢她就卡在那儿），而是**事后按 id 对账**。

四条硬边界，这个文件逐条钉：

  * **落地是独立的一根轴，不覆盖认领状态。** 本地认领（``claimed`` /
    ``handed_off``）和渠道落地是两件正交的事：一条已经落地、但进程崩在写记忆之前的
    消息，事实就是"交出去了、落地了、她还不记得"——用一个 ``delivered`` 盖掉
    ``claimed`` 就把后半句抹了，而后半句正是 :func:`unsettled_outbound` 要捞的那些。
  * **对不上就等下一拍。** 没有重试计数、没有退避表、没有"失败 N 次就放弃"。
  * **CAS 输了也是等下一拍。** 补一版走 append，撞上并发写就这一拍不补 —— 而且
    返回值必须真的被看。这条链上有**两个写者**（这条钟，和 ``mouth.send_message``
    的收口），谁都可能先到；对账这一侧输了有下一拍兜着，收口那一侧输了没有，所以
    收口是重读最新一版合并后重写（``mouth._settle``），这里是直接放掉。
  * **id 的两种写法要真的对得上。** 她派生的是 uuid 的 hex（无短横），库里那一列是
    uuid 类型。这一步错了的表现是永远对不上账，而且一句报错都没有。
"""
from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import text

from app.data import session as session_mod
from app.infra.cst_time import CST
from app.living.landing import LandingTick, reconcile_landings
from app.living.mouth import STATE_CLAIMED, STATE_HANDED_OFF, SpokenOutbound
from app.runtime.persist import insert_append, select_all_versions, select_latest

LANE = "coe-living"
_CONV = uuid.uuid5(uuid.NAMESPACE_OID, "conv-dm-bezhai-akao")


def _at(hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 7, 25, hour, minute, tzinfo=CST)


def _ms(moment: dt.datetime) -> int:
    return int(moment.timestamp() * 1000)


def _derived(seed: str) -> str:
    """跟 :func:`app.living.mouth.send_message` 同款：uuid 的 **hex**，没有短横。"""
    return uuid.uuid5(uuid.NAMESPACE_OID, seed).hex


async def _she_spoke(
    *,
    outbound_id: str,
    lane: str = LANE,
    state: str = STATE_HANDED_OFF,
    said: str = "你去过那家抹茶店吗？",
    moment_id: str = "2026-07-25T21:30+08:00",
) -> SpokenOutbound:
    """在认领表上放一条她开过口的记录（v1）。"""
    row = SpokenOutbound(
        lane=lane,
        outbound_id=outbound_id,
        ver=1,
        persona_id="akao",
        channel_id=str(_CONV),
        moment_id=moment_id,
        said=said,
        state=state,
        claimed_at=_at(21, 30),
        settled_at=_at(21, 30) if state == STATE_HANDED_OFF else None,
    )
    assert await insert_append(row, expected_current_ver=0) == 1
    return row


async def _the_channel_wrote(
    *,
    outbound_id: str | None,
    at: dt.datetime,
    said: str = "你去过那家抹茶店吗？",
) -> uuid.UUID:
    """投递方在公共层落下的那一行 assistant 消息；返回它的 id。

    ``outbound_id`` 给 ``None`` = 这一行不是任何一次主动开口的产物（被动回复、
    QQ 的行、加列之前的存量行都长这样）。
    """
    message_id = uuid.uuid4()
    async with session_mod.get_session() as s:
        await s.execute(
            text(
                "INSERT INTO common_message "
                "(common_message_id, channel, common_conversation_id, role,"
                " content, content_text, scope, bot_name, event_time,"
                " agent_outbound_id) "
                "VALUES (CAST(:mid AS uuid), 'lark', CAST(:conv AS uuid),"
                " 'assistant', '[]'::jsonb, :said, 'direct', 'chiwei', :et,"
                " CAST(:oid AS uuid))"
            ),
            {
                "mid": str(message_id),
                "conv": str(_CONV),
                "said": said,
                "et": _ms(at),
                # 派生 id 是 hex（无短横），uuid 列认它 —— 这里刻意**不**先转成
                # 标准写法，让"两种写法对不对得上"在 fixture 这一层就是真的。
                "oid": outbound_id,
            },
        )
    return message_id


# ---------------------------------------------------------------------------
# 一 · 对上了就把公共层那一行记回来
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_the_row_she_landed_in_is_written_back_onto_that_time_she_spoke(
    living_db,
):
    """她开口 → 渠道落了一行 → 对账把那一行的 id 补回认领表。"""
    outbound_id = _derived("she-said-it")
    await _she_spoke(outbound_id=outbound_id)
    message_id = await _the_channel_wrote(outbound_id=outbound_id, at=_at(21, 31))

    assert await reconcile_landings(lane=LANE) == 1

    row = await select_latest(
        SpokenOutbound, {"lane": LANE, "outbound_id": outbound_id}
    )
    assert row.landed_common_message_id == str(message_id), (
        "对上了却没把公共层那一行记回来 —— 这次开口在库里仍然是失联的"
    )
    assert row.landed_at == _at(21, 31), (
        "落地时刻不是渠道那一行自己的 event_time"
    )


@pytest.mark.integration
async def test_the_hex_id_she_derived_matches_the_uuid_column_it_landed_in(
    living_db,
):
    """**她派生的是 hex（无短横），库里那一列是 uuid 类型。**

    两边写法不同、指的是同一个 id。这一步转换错了不会报错，只会永远对不上账 ——
    所以两种写法都在这条用例里出现一次，谁改坏了都得红。
    """
    outbound_id = _derived("hex-vs-uuid")
    assert "-" not in outbound_id and len(outbound_id) == 32, (
        f"派生 id 不是 hex 形式了，对账那一侧的转换假设要重看：{outbound_id!r}"
    )
    await _she_spoke(outbound_id=outbound_id)
    # 渠道那一行按**标准写法**（带短横）落库，跟她手上那串 hex 是同一个 id。
    message_id = await _the_channel_wrote(
        outbound_id=str(uuid.UUID(hex=outbound_id)), at=_at(21, 31)
    )

    assert await reconcile_landings(lane=LANE) == 1

    row = await select_latest(
        SpokenOutbound, {"lane": LANE, "outbound_id": outbound_id}
    )
    assert row.landed_common_message_id == str(message_id)


@pytest.mark.integration
async def test_the_landing_instant_is_the_channel_row_own_event_time(living_db):
    """落地时刻取渠道那一行的 ``event_time``（毫秒），不是对账这一拍的时刻。

    对账什么时候跑是钟的节拍，不是关于她那句话的事实。存前者才有意义。
    """
    outbound_id = _derived("when-it-landed")
    await _she_spoke(outbound_id=outbound_id)
    await _the_channel_wrote(outbound_id=outbound_id, at=_at(3, 7))

    assert await reconcile_landings(lane=LANE) == 1

    row = await select_latest(
        SpokenOutbound, {"lane": LANE, "outbound_id": outbound_id}
    )
    assert row.landed_at == _at(3, 7)
    assert row.landed_at.tzinfo is not None, (
        "naive 时刻落进 TIMESTAMPTZ 会被按服务器时区解释，静默偏几个小时"
    )


# ---------------------------------------------------------------------------
# 二 · 落地是独立的一根轴
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_landing_does_not_disturb_the_claim_it_rides_on(living_db):
    """一条停在 ``claimed`` 的消息落地了 —— 状态还是 ``claimed``。

    "交出去了、结果未知"和"渠道上确实落地了"是两件事，而且**两件都真**：进程崩在
    写记忆之前，她就是不记得自己说过这句话。用一个投递状态盖掉 ``claimed``，
    :func:`app.living.mouth.unsettled_outbound` 就再也捞不出这一条，那个"她可能不
    记得"的事实从此没人看得见。
    """
    from app.living.mouth import unsettled_outbound

    outbound_id = _derived("claimed-but-landed")
    await _she_spoke(outbound_id=outbound_id, state=STATE_CLAIMED)
    await _the_channel_wrote(outbound_id=outbound_id, at=_at(21, 31))

    assert await reconcile_landings(lane=LANE) == 1

    row = await select_latest(
        SpokenOutbound, {"lane": LANE, "outbound_id": outbound_id}
    )
    assert row.state == STATE_CLAIMED, (
        "落地把认领状态盖掉了 —— 这条从此不再是「她可能不记得」的那一档"
    )
    assert row.settled_at is None, "收口时刻被落地顺手填上了，那是另一根轴上的事实"
    assert row.landed_common_message_id is not None
    assert [r.outbound_id for r in await unsettled_outbound(
        lane=LANE, persona_id="akao"
    )] == [outbound_id], "落地之后就捞不出来了 —— 未收口那件事被对账抹掉了"


@pytest.mark.integration
async def test_the_claim_row_is_kept_as_history_not_rewritten(living_db):
    """补一版走 append：原来那一版还在，不是被 UPDATE 覆盖掉。"""
    outbound_id = _derived("append-not-update")
    await _she_spoke(outbound_id=outbound_id)
    await _the_channel_wrote(outbound_id=outbound_id, at=_at(21, 31))

    assert await reconcile_landings(lane=LANE) == 1

    versions = await select_all_versions(
        SpokenOutbound, {"lane": LANE, "outbound_id": outbound_id}
    )
    assert [v.ver for v in versions] == [1, 2], versions
    assert versions[0].landed_common_message_id is None, (
        "原来那一版被改写了 —— 版本链上就看不出「对账之前是什么样」"
    )
    assert versions[1].landed_common_message_id is not None


# ---------------------------------------------------------------------------
# 三 · 对不上就等下一拍
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_send_that_has_not_landed_yet_waits_for_the_next_tick(living_db):
    """渠道还没写那一行 —— 这一拍什么都不补，下一拍照样来查。

    不放弃、不计数、不退避：投递方晚几秒写库、或者压根崩在半路，都是同一个待遇。
    """
    outbound_id = _derived("not-yet")
    await _she_spoke(outbound_id=outbound_id)

    assert await reconcile_landings(lane=LANE) == 0
    versions = await select_all_versions(
        SpokenOutbound, {"lane": LANE, "outbound_id": outbound_id}
    )
    assert [v.ver for v in versions] == [1], "对不上账却写了一版"

    # 下一拍：渠道那一行到了，照样补得上。
    await _the_channel_wrote(outbound_id=outbound_id, at=_at(21, 40))
    assert await reconcile_landings(lane=LANE) == 1
    row = await select_latest(
        SpokenOutbound, {"lane": LANE, "outbound_id": outbound_id}
    )
    assert row.landed_at == _at(21, 40)


@pytest.mark.integration
async def test_reconciling_again_changes_nothing(living_db):
    """幂等的读-补：补过的那些下一拍不再被碰。"""
    outbound_id = _derived("idempotent")
    await _she_spoke(outbound_id=outbound_id)
    await _the_channel_wrote(outbound_id=outbound_id, at=_at(21, 31))

    assert await reconcile_landings(lane=LANE) == 1
    assert await reconcile_landings(lane=LANE) == 0, "补过的又补了一遍"

    versions = await select_all_versions(
        SpokenOutbound, {"lane": LANE, "outbound_id": outbound_id}
    )
    assert [v.ver for v in versions] == [1, 2], (
        f"每一拍都往版本链上叠一版，表会无限长。拿到：{[v.ver for v in versions]}"
    )


@pytest.mark.integration
async def test_a_lost_race_is_left_for_the_next_tick_not_forced_through(
    living_db, monkeypatch
):
    """CAS 输了 = 这一拍不补，不重试、不计数。**而且不许硬写过去。**

    造的是**真实的版本链**：这一拍读完待办、还没来得及补账的时候，嘴那边收口了
    （v2）。手里那个版本号就此过期，CAS 是真的失败。

    以前这里是一个"无条件返回 0"的替身 —— 那样的话把生产代码里的
    ``expected_current_ver`` 整个删掉，这条用例照样绿：它验的只是"看不看返回值"，
    没验"版本号真的挡住了谁"。
    """
    from app.living import landing as landing_mod

    outbound_id = _derived("lost-race")
    await _she_spoke(outbound_id=outbound_id, state=STATE_CLAIMED)
    await _the_channel_wrote(outbound_id=outbound_id, at=_at(21, 31))

    real_landed_in = landing_mod._landed_in

    async def the_mouth_settles_in_between(oids):
        """查完公共层、还没补账的那一刻，收口那一版落了下来。"""
        found = await real_landed_in(oids)
        claimed = await select_latest(
            SpokenOutbound, {"lane": LANE, "outbound_id": outbound_id}
        )
        assert 1 == await insert_append(
            SpokenOutbound(
                **{
                    **claimed.model_dump(),
                    "state": STATE_HANDED_OFF,
                    "settled_at": _at(21, 31),
                }
            ),
            expected_current_ver=claimed.ver,
        )
        return found

    # 只换这一个符号、也只换回这一个：``monkeypatch.undo()`` 会把 ``test_db`` 那份
    # session 注入一起撤掉（两者共用同一个 monkeypatch fixture），后面的查询就打去
    # 真 DSN 了。
    monkeypatch.setattr(landing_mod, "_landed_in", the_mouth_settles_in_between)
    assert await reconcile_landings(lane=LANE) == 0, (
        "CAS 输了却报成补上了 —— 返回值没被看，或者版本号根本没挡住"
    )
    versions = await select_all_versions(
        SpokenOutbound, {"lane": LANE, "outbound_id": outbound_id}
    )
    assert [(v.ver, v.state) for v in versions] == [
        (1, STATE_CLAIMED),
        (2, STATE_HANDED_OFF),
    ], (
        f"这一拍基于过期版本硬写过去了，把收口那一版盖掉："
        f"{[(v.ver, v.state) for v in versions]}"
    )

    monkeypatch.setattr(landing_mod, "_landed_in", real_landed_in)
    assert await reconcile_landings(lane=LANE) == 1, "下一拍该照常补上"
    row = await select_latest(
        SpokenOutbound, {"lane": LANE, "outbound_id": outbound_id}
    )
    assert row.landed_common_message_id is not None
    assert row.state == STATE_HANDED_OFF, "补账把收口状态盖掉了"


# ---------------------------------------------------------------------------
# 四 · 只认自己那条 id、自己那条泳道
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_row_that_is_not_from_any_send_of_hers_matches_nothing(living_db):
    """``agent_outbound_id`` 是 NULL 的行（被动回复 / QQ / 存量）不参与配对。"""
    outbound_id = _derived("no-match")
    await _she_spoke(outbound_id=outbound_id)
    await _the_channel_wrote(outbound_id=None, at=_at(21, 31), said="随便一条回复")

    assert await reconcile_landings(lane=LANE) == 0
    row = await select_latest(
        SpokenOutbound, {"lane": LANE, "outbound_id": outbound_id}
    )
    assert row.landed_common_message_id is None, (
        "跟一条跟她那次开口无关的消息配上了 —— 库里从此指着错误的一行"
    )


@pytest.mark.integration
async def test_only_the_sends_that_landed_are_touched(living_db):
    """同一拍里两条：落地的那条补上，没落地的那条原样等着。"""
    landed_id = _derived("landed-one")
    waiting_id = _derived("waiting-one")
    await _she_spoke(outbound_id=landed_id)
    await _she_spoke(outbound_id=waiting_id, moment_id="2026-07-25T21:40+08:00")
    message_id = await _the_channel_wrote(outbound_id=landed_id, at=_at(21, 31))

    assert await reconcile_landings(lane=LANE) == 1

    landed = await select_latest(
        SpokenOutbound, {"lane": LANE, "outbound_id": landed_id}
    )
    waiting = await select_latest(
        SpokenOutbound, {"lane": LANE, "outbound_id": waiting_id}
    )
    assert landed.landed_common_message_id == str(message_id)
    assert waiting.landed_common_message_id is None


@pytest.mark.integration
async def test_another_lane_is_none_of_this_lane_business(living_db):
    """对账按泳道走 —— 别的泳道那条不归这一拍管。"""
    mine = _derived("mine")
    theirs = _derived("theirs")
    await _she_spoke(outbound_id=mine)
    await _she_spoke(outbound_id=theirs, lane="coe-otherlane")
    await _the_channel_wrote(outbound_id=mine, at=_at(21, 31))
    await _the_channel_wrote(outbound_id=theirs, at=_at(21, 32))

    assert await reconcile_landings(lane=LANE) == 1

    other = await select_latest(
        SpokenOutbound, {"lane": "coe-otherlane", "outbound_id": theirs}
    )
    assert other.landed_common_message_id is None, (
        "跨泳道补账了 —— 泳道隔离在这一张表上是 Key 的一部分"
    )


# ---------------------------------------------------------------------------
# 五 · 这条钟本身
# ---------------------------------------------------------------------------


def test_the_landing_clock_carries_nothing_but_a_timestamp():
    """单字段 ``ts``：框架源循环固定按 ``data_type(ts=<iso>)`` 造 payload，多一个
    必填字段就是每一拍 ValidationError 直接杀 Pod。顺带也是"钟当不了信箱"那条。
    """
    assert set(LandingTick.model_fields) == {"ts"}, sorted(LandingTick.model_fields)


@pytest.mark.integration
async def test_the_tick_reconciles_the_lane_this_process_is_deployed_on(
    living_db, monkeypatch
):
    """钟不携带泳道 —— 节点自己从进程环境读，跟另外四条钟同一个分工。"""
    from app.living import landing as landing_mod

    outbound_id = _derived("via-the-clock")
    await _she_spoke(outbound_id=outbound_id)
    message_id = await _the_channel_wrote(outbound_id=outbound_id, at=_at(21, 31))

    monkeypatch.setattr(landing_mod, "living_lane", lambda: LANE)
    await landing_mod.landing_tick(LandingTick(ts=_at(21, 35).isoformat()))

    row = await select_latest(
        SpokenOutbound, {"lane": LANE, "outbound_id": outbound_id}
    )
    assert row.landed_common_message_id == str(message_id)

