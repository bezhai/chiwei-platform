"""她那次开口，落地成了公共层的哪一行 —— 事后对账。

:mod:`app.living.mouth` 把一条消息交给投递之后就断线了：出站走 MQ，写库的是渠道
服务，两边隔着一个 broker。渠道服务落 assistant 行时会把"这是她哪一次开口"写进
``common_message.agent_outbound_id``（值就是 :class:`~app.living.mouth.SpokenOutbound`
的 ``outbound_id``）。这个模块负责把这条线**反向接回来**：把认领表上还没对上账的
那些，按 id 查一遍公共层，查到就把那一行的 id 补回去。

**为什么是事后对账，不是投递回执。** 让嘴等一个回执 = 把"她说话"和"渠道写库"绑成
一次同步往返：broker 慢一点她就卡在工具调用里，而她那一缝是有节奏的。事后对账把这
两件事解开——她说完就走，账晚几分钟对上没有任何影响。

**没有重试计数、没有退避、没有"失败 N 次就放弃"。** 对账是**幂等的读-补**：对不上
就等下一拍，CAS 输了也等下一拍。这里没有任何东西需要"最终放弃"——一条永远对不上的
记录本身就是要给人看的事实（那次 ``emit`` 的结果未知，或者投递方压根没写成），
而不是一个要被计数器消化掉的失败。

**补一版走 append，不是 UPDATE。** 照
:func:`app.living.upcoming.mark_upcoming_consumed` 那个模板：读最新一版 → 走构造函数
造新版（**不是** ``model_copy(update=...)``，那个在 pydantic v2 上完全跳过校验，naive
datetime 会从缝里溜进 TIMESTAMPTZ 列）→ ``insert_append(expected_current_ver=...)``
→ **看返回值**。

**这条链上有两个写者，两边输了的待遇不一样。** 另一个是
:func:`app.living.mouth.send_message` 的收口。这一侧输了直接放掉：下一拍它还在待办
里，等着就行。收口那一侧没有下一拍 —— 放掉的话那条记录就永久停在"已落地、未收口"，
所以它是重读最新一版、把状态那一轴合上去再写（:func:`app.living.mouth._settle`）。
两侧写的是**正交的字段**，所以合并是安全的：谁都只往最新一版上添自己那根轴。

**落地不碰 ``state``。** 认领状态和渠道落地是两根正交的轴，理由写在
:class:`~app.living.mouth.SpokenOutbound` 的 docstring 里。

**这不是入站口。** 一条 ``Source.interval`` 的钟 + 两条 SELECT，没有队列、没有
HTTP，外面的任何东西都没法通过它进来（``tests/living/test_no_inbound.py``）。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Annotated

from sqlalchemy import text

from app.data.session import get_session
from app.infra.cst_time import CST
from app.living.clock import living_lane
from app.living.mouth import SpokenOutbound
from app.runtime.data import Data, Key
from app.runtime.migrator import _table_name
from app.runtime.node import node
from app.runtime.persist import insert_append

logger = logging.getLogger(__name__)

# 五分钟一拍。对账不赶时间：没有任何东西等着它（撤回不依赖它，她那一缝也不读它），
# 而它要追的那次写入在消息发出去的几秒内就发生了——所以间隔只要远大于那几秒，快慢
# 对"能不能对上"没有区别。选五分钟而不是更密，是因为每一拍的真实成本是把**永远
# 对不上**的那些（emit 结果未知的、投递方没写成的）重新扫一遍，那部分不会随时间
# 减少；选五分钟而不是更疏，是让一条落地的记录在她下一个常规缝（十分钟）之前一定
# 已经对上账。
LANDING_TICK_SECONDS = 300

_OUTBOUND_TABLE = _table_name(SpokenOutbound)

# 这条泳道上还没对上账的每一条（每个 outbound_id 只看版本链最新那一版）。
#
# 刻意**不加时间窗**：加了就等于替永远对不上的那些做了"放弃"的决定，而那正是该留着
# 给人看的东西。这张表一次开口一条，量级由她说话的频率决定，扫得起。
_UNLANDED = (
    f"SELECT * FROM ("
    f"  SELECT DISTINCT ON (lane, outbound_id) * FROM {_OUTBOUND_TABLE} "
    f"  WHERE lane = :lane ORDER BY lane, outbound_id, ver DESC"
    f") latest WHERE landed_common_message_id IS NULL "
    f"ORDER BY claimed_at ASC"
)

# 公共层里认领了这些 id 的行。
#
# 参数形态跟 ``app.domain.recipient_directory`` 那处一致：**列侧裸用、参数侧给
# ``uuid.UUID`` 对象**（PG 从 ``= ANY($n)`` 推出 uuid[]，asyncpg 直接编码 UUID）。
# 列侧 CAST 成 text 会绕开 ``idx_common_message_agent_outbound_id`` 走全表扫。
_LANDED_IN = (
    "SELECT agent_outbound_id, common_message_id, event_time "
    "FROM common_message WHERE agent_outbound_id = ANY(:oids) "
    "ORDER BY event_time ASC, common_message_id ASC"
)


class LandingTick(Data):
    """对账那一拍。

    单字段 ``ts``——框架源循环固定按 ``data_type(ts=<iso>)`` 造 payload，多一个必填
    字段就是每一拍 ValidationError **直接杀 Pod**。这条约定顺带也是"chat 没有入口"
    的物理保证：钟装不下内容，就当不了信箱。
    """

    ts: Annotated[str, Key]

    class Meta:
        transient = True


async def _unlanded(*, lane: str) -> list[SpokenOutbound]:
    """这条泳道上还没对上账的那些，按认领先后升序。"""
    async with get_session() as s:
        rows = (await s.execute(text(_UNLANDED), {"lane": lane})).mappings().all()
    return [
        SpokenOutbound(**{k: r[k] for k in SpokenOutbound.model_fields}) for r in rows
    ]


async def _landed_in(oids: list[uuid.UUID]) -> dict[uuid.UUID, tuple[uuid.UUID, int]]:
    """``outbound_id -> (公共层那一行的 id, 它的 event_time 毫秒)``。"""
    async with get_session() as s:
        rows = (await s.execute(text(_LANDED_IN), {"oids": oids})).mappings().all()
    found: dict[uuid.UUID, tuple[uuid.UUID, int]] = {}
    for r in rows:
        oid = r["agent_outbound_id"]
        if oid in found:
            # 一次开口只该落一行。真出现两行说明投递方那边重复写了，取最早那条
            # （查询已经按 event_time 排过序），并且说出来 —— 悄悄取一条会让"库里
            # 有两条她的同一句话"这件事没人知道。
            logger.warning(
                "living landing outbound=%s 在公共层有多行（取最早那条 %s），"
                "投递方可能重复写了",
                oid,
                found[oid][0],
            )
            continue
        found[oid] = (r["common_message_id"], r["event_time"])
    return found


async def reconcile_landings(*, lane: str) -> int:
    """把这条泳道上对得上的那些补齐；返回**这次调用**补上了几条。

    幂等：补过的行下一拍不再出现在待办里（``landed_common_message_id IS NOT NULL``）。
    对不上、或者 CAS 输了，这一拍就跳过它，下一拍照样来——不计数、不退避。
    """
    pending = await _unlanded(lane=lane)
    if not pending:
        return 0

    # ``outbound_id`` 是 uuid 的 **hex**（无短横，见 ``mouth.send_message``），而
    # ``common_message.agent_outbound_id`` 是 uuid 列。转换错了不会报错，只会永远
    # 对不上账，所以这一步单独走一遍、形状不对的那条跳过并说出来。
    wanted: dict[uuid.UUID, SpokenOutbound] = {}
    for row in pending:
        try:
            wanted[uuid.UUID(hex=row.outbound_id)] = row
        except ValueError:
            logger.warning(
                "living landing lane=%s outbound=%r 不是 uuid，对不了账",
                lane,
                row.outbound_id,
            )
    if not wanted:
        return 0

    filled = 0
    for oid, (message_id, event_time_ms) in (await _landed_in(list(wanted))).items():
        row = wanted[oid]
        written = await insert_append(
            SpokenOutbound(
                **{
                    **row.model_dump(),
                    "landed_common_message_id": str(message_id),
                    "landed_at": datetime.fromtimestamp(event_time_ms / 1000, tz=CST),
                }
            ),
            expected_current_ver=row.ver,
        )
        if written == 1:
            filled += 1
        else:
            # 有人在我们读完之后又给这条追了一版（收口、或者另一个进程在对同一批
            # 账）。这一拍不补，下一拍它还在待办里 —— 不重试、不计数。
            logger.info(
                "living landing lane=%s outbound=%s 补账时版本被人抢先了，等下一拍",
                lane,
                row.outbound_id,
            )
    return filled


@node
async def landing_tick(tick: LandingTick) -> None:
    """把这条泳道上还没对上账的开口补一遍。

    泳道由节点自己从进程环境读（钟不携带内容），跟另外四条钟同一个分工。
    """
    lane = living_lane()
    filled = await reconcile_landings(lane=lane)
    if filled:
        logger.info("living landing lane=%s 对上了 %d 次开口", lane, filled)
