"""她惦记着没了结的事 —— 状态快照里唯一由她自己维护的那一层。

快照的另外三层都是**从事实读出来的当前状态**（她在哪、她刚做过什么、这段时间发生
了什么），读多少遍都一样，而且天然有界。只有这一层是她自己写的，因为它回答的是一
个只有她能回答的问题：**那些已经滚出窗口的事情里，哪几件我还惦记着。**

**开、关、保持是同一个动作。** 她调 :func:`app.living.moment.keep_in_mind` 重写一遍
整份清单（:func:`rewrite_loose_ends`）：写进去的就挂着，这次没写的就是关掉了。关不是
代码判断"这条过期了"，是**她的省略**在生效——判准是"它是在让她的决定生效，还是替她做
决定"，省略即关掉属于前者。所以这里没有任何过期阈值、没有条数上限、没有优先级排序。

**哪一缝都能调，不绑在"换事情"上。** 「是否换事」不等于「是否记住」：有人跟她说了
句要紧的话，她手上的书没放下（那一缝答「继续」），但她记住了。绑在换事情上的话，这
条感知在游标推进之后就永久消失——她自己最近那十二条里只有她**自己**说做的，别人说的
话不在里面。

**已知限制（不修，coe 上观察）：线头的身份是那句话的原文。** 所以

  * 她换个说法重述同一件事 → 算成一条**新**线头，旧的那条因为没被列出来而关掉；
  * 她某一缝漏抄了一条 → 那条就此关掉，跟她真的了结了它分不出来；
  * 关掉之后原句一字不差地再出现 → 复活，出处停在第一次那一缝，中间那段断档在数据
    上看不出来（读起来像"从头到尾一直挂着"）。

给线头做精确身份（编号、模糊匹配、相似度合并）是工程脑，而且真人的记忆本来就是这样
模糊、会漏、会自己接上的。这里选择照实记录她说了什么，把失真留在数据里让人看得见，
而不是用一套匹配规则把它藏起来。

**``thread_id`` 从内容派生**，所以同一句话重写多少遍都是同一条线头，
``opened_moment_id`` 永远停在第一次写下的那一缝。这是"跨多缝没被遗忘、而且指得出
是从哪一缝带过来的"这条验收的全部依据：清单上每一条都自带出处。

**关掉的又被列出来 = 复活，出处不变。** 她重新惦记起同一件事，说明它从最早那一缝
起就一直在她心上；给它换一个新出处等于把这段延续抹掉。

有版本链（``ver``），因为线头有一个真实的状态变化：写下 → 了结（→ 也可能重新挂
起）。用 framework 的 ``Version`` + ``insert_append`` CAS，不另起一张影子表——
"这条了结了没有"是这条线头的状态，不是另一件事。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Annotated

from pydantic import field_validator
from sqlalchemy import text

from app.data.session import get_session
from app.living.records import _require_aware
from app.runtime.data import Data, Key, Version
from app.runtime.migrator import _table_name
from app.runtime.persist import insert_append, select_latest

# 派生 thread_id 的命名空间。换掉它 == 历史线头全部对不上、每条的出处一起重置。
_THREAD_ID_NS = uuid.UUID("2b7f9c14-6d8a-4f31-9e05-7c3a1d4b8e62")


class LooseEnd(Data):
    """她心上挂着的一件事：什么事、从哪一缝起挂着、了结了没有。

    自然键 ``(lane, persona_id, thread_id)``；``thread_id`` 从 ``what`` 派生
    （:func:`derive_thread_id`），所以重写清单不会堆出重复行。

    ``opened_moment_id`` 是**第一次**把它写进心上的那一缝，之后再怎么重写都不变。
    ``closed_moment_id`` 是关掉它的那一缝。这两列不是日志：验收要对每一条"跨缝
    带过来的事"指出具体是哪两条记录接上的，只给一个比例不算数。

    ``what`` 存的是归一化（去首尾空白）之后的原话，不做任何改写——她怎么说的就怎么
    存，下一缝原样还给她看。
    """

    lane: Annotated[str, Key]
    persona_id: Annotated[str, Key]
    thread_id: Annotated[str, Key]
    ver: Annotated[int, Version]  # framework 维护：v1 = 写下，之后 = 关掉 / 复活
    what: str
    opened_at: datetime
    opened_moment_id: str
    closed_at: datetime | None = None
    closed_moment_id: str | None = None

    # 不声明 Meta.indexes：读取形状是"这个人每条线头的最新一版"，先
    # DISTINCT ON (lane, persona_id, thread_id) ORDER BY ver DESC 再筛 closed_at
    # —— 走的是 migrator 给 Version 类自动建的 ix_key_ver。

    @field_validator("opened_at", "closed_at")
    @classmethod
    def _aware_instant(cls, v: datetime | None) -> datetime | None:
        return _require_aware("opened_at / closed_at", v)


_TABLE = _table_name(LooseEnd)


def derive_thread_id(what: str) -> str:
    """一件挂心事的 id —— 从它那句话派生，所以重写清单落回同一条。

    只做去首尾空白这一步归一化：再多的归一化（去标点、压空格、大小写）会把两件
    她自己觉得不一样的事悄悄合并成一条。宁可让措辞变了的那句当成新的一条——那至少
    是她真的换了说法。
    """
    return "end:" + uuid.uuid5(_THREAD_ID_NS, what.strip()).hex


async def list_open_loose_ends(*, lane: str, persona_id: str) -> list[LooseEnd]:
    """她此刻还挂着没了结的事，按挂上的先后升序。

    每条只看版本链上最新的一版：``closed_at`` 一旦被填上，这条就不再出现（除非
    后来又被她列出来、append 了一版把它清空）。
    """
    sql = (
        f"SELECT * FROM ("
        f"  SELECT DISTINCT ON (lane, persona_id, thread_id) * FROM {_TABLE} "
        f"  WHERE lane = :lane AND persona_id = :persona_id "
        f"  ORDER BY lane, persona_id, thread_id, ver DESC"
        f") latest "
        f"WHERE closed_at IS NULL "
        f"ORDER BY opened_at ASC, thread_id ASC"
    )
    async with get_session() as s:
        result = await s.execute(
            text(sql), {"lane": lane, "persona_id": persona_id}
        )
        rows = result.mappings().all()
    return [LooseEnd(**{k: row[k] for k in LooseEnd.model_fields}) for row in rows]


async def rewrite_loose_ends(
    *,
    lane: str,
    persona_id: str,
    moment_id: str,
    now: datetime,
    still_on_my_mind: Sequence[str],
) -> list[LooseEnd]:
    """把她这一缝报的整份清单落下来；返回落完之后还挂着的那些。

    ``still_on_my_mind`` 是**全量**，不是增量：里面有的挂着，里面没有的关掉。空
    清单就是"心里空了"，代码不许替她留着任何一条。漏抄跟真的了结在这里分不出来，
    这是已知限制（见模块 docstring），不加任何补偿逻辑。

    空白项直接忽略（她列了个空行不是一件心事）；同一句话在一份清单里出现两次算
    一件（派生 id 一样）。

    返回值让调用方（一缝）直接报得出"缝末还挂着几件"，不用再查一次库。
    """
    wanted: dict[str, str] = {}
    for raw in still_on_my_mind:
        what = (raw or "").strip()
        if not what:
            continue
        wanted.setdefault(derive_thread_id(what), what)

    for thread_id, what in wanted.items():
        await _keep_open(
            lane=lane,
            persona_id=persona_id,
            thread_id=thread_id,
            what=what,
            moment_id=moment_id,
            now=now,
        )

    for end in await list_open_loose_ends(lane=lane, persona_id=persona_id):
        if end.thread_id in wanted:
            continue
        await insert_append(
            LooseEnd(
                **{
                    **end.model_dump(),
                    "closed_at": now,
                    "closed_moment_id": moment_id,
                }
            ),
            expected_current_ver=end.ver,
        )

    return await list_open_loose_ends(lane=lane, persona_id=persona_id)


async def _keep_open(
    *,
    lane: str,
    persona_id: str,
    thread_id: str,
    what: str,
    moment_id: str,
    now: datetime,
) -> None:
    """让这条线头处于"挂着"的状态：没有就写下，关着的就复活，开着的什么都不做。

    复活时 ``opened_at`` / ``opened_moment_id`` **原样保留**：她重新惦记起同一件
    事，说明它从最早那一缝起就一直在她心上，换一个新出处等于把这段延续抹掉。

    走构造函数而不是 ``model_copy(update=...)``：后者在 pydantic v2 上完全跳过
    校验，naive 的时刻会从这条缝里溜进 TIMESTAMPTZ 列。
    """
    keys = {"lane": lane, "persona_id": persona_id, "thread_id": thread_id}
    latest = await select_latest(LooseEnd, keys)
    if latest is None:
        await insert_append(
            LooseEnd(
                **keys,
                ver=0,  # 由 insert_append 按 expected_current_ver 赋成 1
                what=what,
                opened_at=now,
                opened_moment_id=moment_id,
            ),
            expected_current_ver=0,
        )
        return
    assert isinstance(latest, LooseEnd)
    if latest.closed_at is None:
        return
    await insert_append(
        LooseEnd(
            **{
                **latest.model_dump(),
                "closed_at": None,
                "closed_moment_id": None,
            }
        ),
        expected_current_ver=latest.ver,
    )
