"""渠道那边关于她这次开口的事实，事后按 id 取回来。

:mod:`app.living.mouth` 把一条消息交给投递之后就断线了：出站走 MQ，写库的是渠道
服务，两边隔着一个 broker。渠道服务落 assistant 行时会把"这是她哪一次开口"写进
``common_message.agent_outbound_id``（值就是 :class:`~app.living.mouth.SpokenOutbound`
的 ``outbound_id``）。这个模块负责把这条线**反向接回来**。

**这条钟把两件事对回台账，两件都是渠道那一行上的事实：**

  * **落地**（:func:`reconcile_landings`）：还没对上账的那些，按 id 查一遍公共层，
    查到就把那一行的 id 和它的 ``event_time`` 补回去。
  * **撤回**（:func:`reconcile_recalls`）：她按下撤回、还没确认撤掉的那些，按同一
    列 id 查 ``common_message.recalled_at``（投递侧撤成功才填这一列，撤失败不填）。
    **这次开口对应的每一行都有撤回时刻**才算撤完，补的是最后那一段消失的时刻；
    只撤掉一部分就等下一拍 —— 那条消息确实还有一段在真人眼前。

两件事各查各的待办、各写各的那根轴，共用一条钟和一份 id 转换 —— 追的是同一列
``agent_outbound_id``、同一批记录，节奏也没有第二套理由，所以不另起一条钟。

**为什么是事后对账，不是投递回执/撤回回调。** 让嘴等一个回执 = 把"她说话"和"渠道
写库"绑成一次同步往返：broker 慢一点她就卡在工具调用里，而她那一缝是有节奏的。撤回
那一侧更彻底：生活引擎这里**没有、也不会有任何入站边**（``app/wiring/living.py``
写死，``tests/living/test_no_inbound.py`` 守着），渠道撤成功了没有回调也没有队列能
告诉我们，只能自己按节奏去查。事后对账把这两件事解开——她说完就走，账晚几分钟对上
没有任何影响。

**没有重试计数、没有退避、没有"失败 N 次就放弃"。** 对账是**幂等的读-补**：对不上
就等下一拍，CAS 输了也等下一拍。这里没有任何东西需要"最终放弃"——一条永远对不上的
记录本身就是要给人看的事实（那次 ``emit`` 的结果未知，或者投递方压根没写成），
而不是一个要被计数器消化掉的失败。

**补一版走 append，不是 UPDATE。** 照
:func:`app.living.upcoming.mark_upcoming_consumed` 那个模板：读最新一版 → 走构造函数
造新版（**不是** ``model_copy(update=...)``，那个在 pydantic v2 上完全跳过校验，naive
datetime 会从缝里溜进 TIMESTAMPTZ 列）→ ``insert_append(expected_current_ver=...)``
→ **看返回值**。

**这条链上有三个写者，输了的待遇不一样。** 另外两个是
:func:`app.living.mouth.send_message` 的收口，和她按下撤回那只手。对账这两侧输了都
直接放掉：下一拍它还在待办里，等着就行。收口那一侧没有下一拍 —— 放掉的话那条记录
就永久停在"已落地、未收口"，所以它是重读最新一版、把状态那一轴合上去再写
（:func:`app.living.mouth._settle`）。三边写的是**正交的字段**，所以合并是安全的：
谁都只往最新一版上添自己那根轴，绝不用手里那份过期的把别人已经写下的事实抹回 NULL。

**这两件事都不碰 ``state``。** 认领状态、渠道落地、撤回是三根正交的轴，理由写在
:class:`~app.living.mouth.SpokenOutbound` 的 docstring 里。

**这不是入站口。** 一条 ``Source.interval`` 的钟 + 几条 SELECT，没有队列、没有
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

# 五分钟一拍。对账不赶时间：两件事都没有人等着它。她那一缝不读这张台账；撤回那只手
# 也不读（投递侧按 ``agent_outbound_id`` 直接反查公共层去撤），而"她还看不看得见那条
# 撤掉的消息"是直接看 ``common_message.recalled_at`` 的（:mod:`app.living.phone`），
# 跟这条钟对没对上账无关。它要追的那两次写入都发生在动作之后的几秒到几十秒内——所以
# 间隔只要远大于那几秒，快慢对"能不能对上"没有区别。选五分钟而不是更密，是因为每一拍
# 的真实成本是把**永远对不上**的那些（emit 结果未知的、投递方没写成的、撤回始终没成功
# 的）重新扫一遍，那部分不会随时间减少；选五分钟而不是更疏，是让一条落地的记录在她下
# 一个常规缝（十分钟）之前一定已经对上账。
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

# 她按下撤回了、但还没确认撤掉的每一条（同样只看版本链最新那一版）。
#
# 待办由**她的动作**定义（``took_back_at`` 非空），不是由渠道那一列定义：渠道说撤掉
# 了而台账上她没撤过，那是一件该被看见的怪事，不是一个可以顺手补上的空。
#
# 跟落地那条一样不加时间窗：投递侧退避重投三次才进死信，而"她去撤了、始终没撤掉"
# 本身就是要留着给人看的事实。
_UNCONFIRMED_RECALLS = (
    f"SELECT * FROM ("
    f"  SELECT DISTINCT ON (lane, outbound_id) * FROM {_OUTBOUND_TABLE} "
    f"  WHERE lane = :lane ORDER BY lane, outbound_id, ver DESC"
    f") latest WHERE took_back_at IS NOT NULL AND recalled_at IS NULL "
    f"ORDER BY took_back_at ASC"
)

# 公共层里认领了这些 id 的行。
#
# 参数形态：**列侧裸用、参数侧给 ``uuid.UUID`` 对象**（PG 从 ``= ANY($n)`` 推出
# uuid[]，asyncpg 直接编码 UUID）。
# 列侧 CAST 成 text 会绕开 ``ix_common_message_agent_outbound_id`` 走全表扫。
_LANDED_IN = (
    "SELECT agent_outbound_id, common_message_id, event_time "
    "FROM common_message WHERE agent_outbound_id = ANY(:oids) "
    "ORDER BY event_time ASC, common_message_id ASC"
)

# 这些 id 各自在公共层落了几行、其中几行渠道那边真撤掉了、最后一行是什么时候没的。
#
# ``recalled_at IS NOT NULL`` 是"**这一行**撤成功了"的全部判据 —— 投递侧撤失败不填
# 这一列，所以空着就是还没撤掉（**不是撤失败**）。
#
# 但"**这次开口**撤完了"要的是每一行都撤掉：一次开口被切成几段发出去时每段各一行，
# 撤掉一段、另一段还挂在真人眼前，跟整条撤完不是同一件事。所以这里不按行取，按 id
# 聚合出「几段 / 撤掉几段 / 最后一段什么时候没的」，判据留给调用方。
#
# ``count(recalled_at)`` 只数非空的那些，跟 ``count(*)`` 相等就是全撤掉了。
_RECALL_STATE_IN = (
    "SELECT agent_outbound_id, "
    "count(*) AS parts, "
    "count(recalled_at) AS parts_recalled, "
    "max(recalled_at) AS last_recalled_at "
    "FROM common_message WHERE agent_outbound_id = ANY(:oids) "
    "GROUP BY agent_outbound_id"
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


async def _pending(sql: str, *, lane: str) -> list[SpokenOutbound]:
    """这条泳道上某一份待办（两条 SELECT 的形状一样，只有 WHERE 不同）。"""
    async with get_session() as s:
        rows = (await s.execute(text(sql), {"lane": lane})).mappings().all()
    return [
        SpokenOutbound(**{k: r[k] for k in SpokenOutbound.model_fields}) for r in rows
    ]


def _by_outbound_uuid(
    rows: list[SpokenOutbound], *, lane: str
) -> dict[uuid.UUID, SpokenOutbound]:
    """把待办按 ``outbound_id`` 的 **uuid 写法**索引起来。

    ``outbound_id`` 是 uuid 的 **hex**（无短横，见 ``mouth.send_message``），而
    ``common_message.agent_outbound_id`` 是 uuid 列。转换错了不会报错，只会永远
    对不上账，所以这一步单独走一遍、形状不对的那条跳过并说出来。
    """
    wanted: dict[uuid.UUID, SpokenOutbound] = {}
    for row in rows:
        try:
            wanted[uuid.UUID(hex=row.outbound_id)] = row
        except ValueError:
            logger.warning(
                "living landing lane=%s outbound=%r 不是 uuid，对不了账",
                lane,
                row.outbound_id,
            )
    return wanted


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


async def _recalled_in(oids: list[uuid.UUID]) -> dict[uuid.UUID, datetime]:
    """``outbound_id -> 这次开口最后一段被撤掉的时刻``。**没撤完的不在结果里。**

    判据是"这次开口在公共层的每一行都有撤回时刻"，不是"有任意一行有"。一次开口被
    切成几段发出去时每段各一行，撤掉一段、另一段还挂在真人眼前 —— 那跟整条撤完不
    是同一件事，按"有一行就算"记的话，这条从此退出待办，另一段再也没人去查。

    时刻取**最后**那一段消失的那一刻。这次开口从第一段被删开始变残缺、到最后一段
    被删才彻底不在，"撤掉了"这一刻说的是后者；取最早那个会把中间那段还看得见的
    时间抹掉。

    一段都没撤掉是**常态**（她按下撤回到渠道真撤掉之间隔着一趟队列），不说话；
    撤了一部分才是要给人看的事实，每一拍都说一次 —— 它不会自己好起来。
    """
    async with get_session() as s:
        rows = (
            (await s.execute(text(_RECALL_STATE_IN), {"oids": oids})).mappings().all()
        )
    found: dict[uuid.UUID, datetime] = {}
    for r in rows:
        parts, recalled = r["parts"], r["parts_recalled"]
        if recalled == parts:
            found[r["agent_outbound_id"]] = r["last_recalled_at"]
        elif recalled:
            logger.warning(
                "living landing outbound=%s 只撤掉了 %d/%d 段，还有一段在别人眼前，"
                "这一拍不确认",
                r["agent_outbound_id"],
                recalled,
                parts,
            )
    return found


async def reconcile_landings(*, lane: str) -> int:
    """把这条泳道上对得上的那些补齐；返回**这次调用**补上了几条。

    幂等：补过的行下一拍不再出现在待办里（``landed_common_message_id IS NOT NULL``）。
    对不上、或者 CAS 输了，这一拍就跳过它，下一拍照样来——不计数、不退避。
    """
    wanted = _by_outbound_uuid(await _pending(_UNLANDED, lane=lane), lane=lane)
    if not wanted:
        return 0

    filled = 0
    for oid, (message_id, event_time_ms) in (await _landed_in(list(wanted))).items():
        row = wanted[oid]
        # 走构造函数而不是 ``model_copy(update=...)``：后者在 pydantic v2 上完全跳过
        # 校验，naive datetime 会从这条缝里溜进 TIMESTAMPTZ 列。
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
            # 有人在我们读完之后又给这条追了一版（收口、撤回那只手、或者另一个进程
            # 在对同一批账）。这一拍不补，下一拍它还在待办里 —— 不重试、不计数。
            logger.info(
                "living landing lane=%s outbound=%s 补账时版本被人抢先了，等下一拍",
                lane,
                row.outbound_id,
            )
    return filled


async def reconcile_recalls(*, lane: str) -> int:
    """把渠道那边真撤掉了的那些确认回台账；返回**这次调用**确认了几条。

    待办是"她按下撤回了、还没确认撤掉的"（``took_back_at`` 非空、``recalled_at``
    为空）。查不到 = 渠道那边还没撤掉，**不是撤失败了**：投递侧退避重投三次才进
    死信，中间这段时间的事实就是"不知道"。撤掉一部分跟一部分都没撤同一个待遇（见
    :func:`_recalled_in`）—— 那条消息还有一段在真人眼前，说它撤完了就是假的。
    所以这里跟落地那一半同一个待遇 —— 对不上就等下一拍，CAS 输了也等下一拍，
    不计数、不退避。

    只写 ``recalled_at`` 这一根轴，其余几根照最新读到的那一版原样带走：认领状态、
    落地那两列、她按下撤回的时刻都是别人写的事实，用手里这份把它们抹回 NULL 就是
    静默的数据丢失。
    """
    wanted = _by_outbound_uuid(
        await _pending(_UNCONFIRMED_RECALLS, lane=lane), lane=lane
    )
    if not wanted:
        return 0

    confirmed = 0
    for oid, at in (await _recalled_in(list(wanted))).items():
        row = wanted[oid]
        # ``at`` 是渠道那一行自己的时刻，从 TIMESTAMPTZ 列读回来就是 aware 的，不像
        # 落地那半要从 epoch 毫秒现造一个。同样走构造函数而不是 ``model_copy``。
        written = await insert_append(
            SpokenOutbound(**{**row.model_dump(), "recalled_at": at}),
            expected_current_ver=row.ver,
        )
        if written == 1:
            confirmed += 1
        else:
            logger.info(
                "living landing lane=%s outbound=%s 确认撤回时版本被人抢先了，"
                "等下一拍",
                lane,
                row.outbound_id,
            )
    return confirmed


@node
async def landing_tick(tick: LandingTick) -> None:
    """把渠道那边关于她这次开口的事实取回来：落地一遍，撤回一遍。

    两件事都可能这一拍一条都没有（她可能根本没撤过任何东西），那是正常的 —— 记
    info，不是错误。

    泳道由节点自己从进程环境读（钟不携带内容），跟另外四条钟同一个分工。
    """
    lane = living_lane()
    filled = await reconcile_landings(lane=lane)
    if filled:
        logger.info("living landing lane=%s 对上了 %d 次开口", lane, filled)
    confirmed = await reconcile_recalls(lane=lane)
    if confirmed:
        logger.info("living landing lane=%s 确认撤掉了 %d 条", lane, confirmed)
