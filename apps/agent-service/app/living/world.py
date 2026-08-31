"""world 的稀疏轮次 —— 低频问一句「有什么新东西该出现了吗」，默认答「没有」。

**这一版 world 只剩一件事可做：往账上添一件会到期的东西。** 它不写世界叙述、不
描述此刻、不推演谁在干嘛。上一代每轮被要求描述世界，14 天 prod 实测烧掉总消耗约
一半去写「水槽水珠成浅印」这类静物记账——那些字没有任何一个变成她能遇上的事。
所以这里把产出面收窄到一件：:func:`expect`，收「什么事、多久之后、在哪」，写进
:class:`~app.living.records.Upcoming`，剩下的交给到期交付（:mod:`app.living.calendar`）。
一轮什么都不调、只回一句「没有」是**正常且期望的**结果。

**它不挑收件人。** :func:`expect` 没有 recipient 参数，签名里也不会有。谁感知得
到由位置说了算（:func:`app.living.place.reach_between`），那是客观事实，不是 world
的判断——旧 world 亲手挑收件人，信息差就没有归属人了。

**它也不接收姐妹之间发生了什么。** 喂给它的只有账本：这段时间有哪些客观的事已经
发生过、还有哪些还没到。姐妹之间说了什么、谁在生谁的气，跟"外面该不该下雨"无关，
喂进去只会让它去替她们编剧情。

**低频，而且频率是业务参数。** 时间源那一拍是固定的（钟不该由配置决定跳不跳），
真正的间隔由 Dynamic Config :data:`LIVING_WORLD_ROUND_MINUTES_KEY` 说了算，判在
:func:`run_world_round` 里：离上一轮不够久就一句模型都不调。轮次落库
（:class:`WorldRound`）有三个用处，缺一个都会疼：间隔判断要读上一轮什么时候跑的
（不落库的话每次发版都白跑一轮）、重放要有幂等的依据、验收要能逐条列出「哪几轮
说了没有、哪几轮产出了什么」。

prompt 在 Langfuse（:data:`WORLD_ROUND_PROMPT_ID`），不硬编码进代码；账本走 USER
那一轮的消息而不是 prompt 变量——它每轮都变，而 prompt 变量改名会**静默**渲染成
字面量，能少一个变量就少一个。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from typing import Annotated

from inner_shared.dynamic_config import dynamic_config
from pydantic import Field, field_validator
from sqlalchemy import text

from app.agent.context import AgentContext
from app.agent.core import AgentConfig
from app.agent.neutral import Message, Role
from app.agent.runtime_context import get_context
from app.agent.tooling import tool
from app.agent.tools._common import tool_error
from app.capabilities.agent import AgentRunner
from app.data.session import get_session
from app.living.anchor import anchor_on_grid
from app.living.records import _require_aware
from app.living.serial import hold
from app.living.upcoming import list_upcoming_between, schedule_upcoming
from app.runtime.data import Data, Key
from app.runtime.migrator import _table_name
from app.runtime.persist import insert_idempotent

logger = logging.getLogger(__name__)

# Langfuse prompt id（新 id，只发泳道 label，不碰 production）。它只吃 Agent 自动
# 注入的 currDate / currTime 两个变量，账本走 USER 消息。
WORLD_ROUND_PROMPT_ID = "living_world_round"

# offline-model：这一轮要的是对客观世界的推断（这个点该有什么事冒出来），不是
# 对话能力。跟旧 world 用同一个别名，不新增 mapping。
# recursion_limit 4：一轮至多几次工具调用就该收口；世界不需要它在一轮里长篇折腾。
_WORLD_ROUND_CFG = AgentConfig(
    WORLD_ROUND_PROMPT_ID,
    "offline-model",
    "living-world-round",
    recursion_limit=4,
)

# Dynamic Config key：两轮之间至少隔多少分钟。默认 60——这是"稀疏"的量级，不是
# 10 分钟。改它不用重新部署。
LIVING_WORLD_ROUND_MINUTES_KEY = "living_world_round_minutes"
DEFAULT_WORLD_ROUND_MINUTES = 60

# expect 能排多远：最近 1 分钟、最远 3 天。下限挡"立刻发生"（那不是将要发生的事，
# 是它在替世界直接下判断）；上限挡"三个月后的祭典"这类它根本没有依据的远期承诺。
# 超范围报错喂回模型让它重填，绝不静默夹成边界值。
EXPECT_MIN_MINUTES = 1
EXPECT_MAX_MINUTES = 60 * 24 * 3

# 喂给它的账本窗口：往回半天（刚发生过的事，防它立刻再排一次）、往前**正好排得到
# 多远**（还没到的安排，防它重排同一件事）。
#
# 往前那一头必须从 EXPECT_MAX_MINUTES 派生，不能各写各的数：两个窗口一旦不一致，
# 它就能排出一件自己下一轮看不见的事，然后理直气壮地再排一遍——而重复安排恰恰是
# 这个账本存在的唯一理由。
LEDGER_LOOK_BACK = timedelta(hours=12)
LEDGER_LOOK_AHEAD = timedelta(minutes=EXPECT_MAX_MINUTES)

# 工具体从 ambient context 读这两样：lane 是泳道隔离的硬约束，now 是**本轮的时间锚**。
# now 必须来自 context 而不是工具体自己 ``datetime.now()``——派生 id 里带着 due_at，
# 整轮重放时若各次取各自的"现在"，同一件事会落成两条不同的 item。
FEATURE_LANE = "living_lane"
FEATURE_NOW = "living_now"
# 本轮真的写上账的 item_id（round-scoped，engine 每轮新建）。数它而不是数工具调用
# 次数：模型重复调同一件事只该算一件，而"调了几次 expect"跟"世界多了几件事"不是
# 一回事；也不能靠前后数账本条数——expect 能排到 3 天后，早就出了账本窗口。
FEATURE_WRITTEN = "living_written"

# 派生 item_id 的命名空间，随手换会让历史 item 全部对不上。
_ITEM_ID_NS = uuid.UUID("6f5d1c2e-9a3b-4d7e-8c10-5b2f4a6e9d31")


class WorldRound(Data):
    """world 跑过的一轮：什么时候跑的、产出了几件事、最后说了什么。

    自然键 ``(lane, round_id)``，纯 append 无版本链——一轮跑完就是跑完了，没有
    "改一条旧轮次"的语义。``round_id`` 取本轮时间锚（精确到分），所以同一分钟里
    的重放落回同一行。

    ``produced`` 是这一轮**真的写上账**的件数（重复调 expect 只算一件）；``said``
    是它最后那句话，默认就是「没有」。这两列不是日志，是验收口径：「没有」的比例
    和实际产出的新东西要能从这张表逐条查出来——只靠 langfuse trace 算不准（会丢
    trace），而只看 Upcoming 表根本看不见"跑了但什么都没产"的那些轮。
    """

    lane: Annotated[str, Key]
    round_id: Annotated[str, Key]
    ran_at: datetime
    produced: int
    said: str

    class Meta:
        # 读侧唯一形状：某 lane 上最近跑的那一轮。
        indexes = (("lane", "ran_at"),)

    @field_validator("ran_at")
    @classmethod
    def _aware_ran_at(cls, v: datetime) -> datetime:
        # 跟三张 living 表同一个把关（见 app.living.records）：naive 落进
        # TIMESTAMPTZ 会被按服务器时区解释、静默偏几小时，间隔判断跟着全错。
        return _require_aware("ran_at", v)


_ROUND_TABLE = _table_name(WorldRound)


def world_round_lock_key(lane: str) -> str:
    """本 lane 上 world 轮次的排他占用 key（一条轴，同一时刻只跑一轮）。"""
    return f"living:world-round:{lane}"


def derive_upcoming_id(*, what: str, place: str | None, due_at: datetime) -> str:
    """world 排的一件事的 item_id —— 从内容派生，所以重放落回同一条。

    同一轮里模型重复调一次、整轮被重放，都会算出同一个 id，账上只占一行。不同
    时刻的同一句话是不同的两件事（``due_at`` 进了派生），这是对的：晚上七点的
    "外面开始下雨"和第二天下午的那场不是一回事。
    """
    return "world:" + uuid.uuid5(
        _ITEM_ID_NS, f"{what}\x1f{place or ''}\x1f{due_at.isoformat()}"
    ).hex


def _round_scope() -> tuple[str, datetime]:
    """从 ambient context 取本轮的 (lane, 时间锚)。

    没绑 context 直接 ``LookupError`` 失败快，暴露漏了 ``agent_context(...)`` 的
    wiring bug——静默用一个空 lane 会开一条谁也读不到的影子轴。
    """
    ctx = get_context()
    return ctx.features[FEATURE_LANE], datetime.fromisoformat(
        ctx.features[FEATURE_NOW]
    )


def _written_slot() -> list[str]:
    """本轮已写上账的 item_id 容器（``run_world_round`` 每轮新建）。"""
    return get_context().features.setdefault(FEATURE_WRITTEN, [])


@tool
@tool_error("写下将要发生的事失败")
async def expect(
    what: Annotated[
        str, Field(description="将要发生的这件客观事，一句自然语言，例如「快递送到门口」")
    ],
    in_minutes: Annotated[
        int, Field(description=f"多少分钟之后发生，{EXPECT_MIN_MINUTES}～{EXPECT_MAX_MINUTES}")
    ],
    place: Annotated[
        str, Field(description="发生在哪，层级路径如「家/门口」；说不出就留空")
    ] = "",
) -> str:
    """写下一件将要发生的客观事（你这一轮唯一能做的事）。

    只说**是什么、在哪、多久之后**。谁会碰上它不用你管——到点了它自己会发生，
    在场的人自然感知得到。说不出发生在哪就把 place 留空，别编一个地点。

    in_minutes 必须在 1～4320 之间（最近 1 分钟后、最远 3 天后）。超出范围会
    报错，请改填一个范围内的值重调。

    大多数轮次你什么都不用调 —— 世界大部分时候没有新东西该出现。

    Args:
        what: 将要发生的这件客观事，一句自然语言。
        in_minutes: 多少分钟之后发生（1 ≤ in_minutes ≤ 4320）。
        place: 发生在哪（层级路径）；留空 = 不绑地点。

    Returns:
        一句确认文本。
    """
    if not (EXPECT_MIN_MINUTES <= in_minutes <= EXPECT_MAX_MINUTES):
        raise ValueError(
            f"expect 的 in_minutes={in_minutes} 不在 {EXPECT_MIN_MINUTES}～"
            f"{EXPECT_MAX_MINUTES} 之间。请改填一个范围内的值重调。"
        )
    lane, now = _round_scope()
    due_at = now + timedelta(minutes=in_minutes)
    where = place.strip() or None
    item_id = derive_upcoming_id(what=what, place=where, due_at=due_at)
    if not await schedule_upcoming(
        lane=lane, item_id=item_id, what=what, due_at=due_at, place=where
    ):
        return f"「{what}」已经在账上了，没有重复写"
    _written_slot().append(item_id)
    return f"记下了：{due_at.strftime('%m-%d %H:%M')} 「{what}」"


WORLD_ROUND_TOOLS = [expect]


async def world_round_minutes() -> int:
    """两轮之间至少隔多少分钟；没配 / 配脏退回默认值。

    Dynamic Config 的拉取是同步 httpx（10s 缓存），走 ``asyncio.to_thread`` 避免
    缓存刷新那一次阻塞事件循环（与 :mod:`app.living.calendar` 同口径）。
    """
    minutes = await asyncio.to_thread(
        dynamic_config.get_int,
        LIVING_WORLD_ROUND_MINUTES_KEY,
        default=DEFAULT_WORLD_ROUND_MINUTES,
    )
    if minutes <= 0:
        logger.warning(
            "dynamic config %s = %r 不是正整数；本次退回 %d 分钟",
            LIVING_WORLD_ROUND_MINUTES_KEY,
            minutes,
            DEFAULT_WORLD_ROUND_MINUTES,
        )
        return DEFAULT_WORLD_ROUND_MINUTES
    return minutes


async def latest_world_round(*, lane: str) -> WorldRound | None:
    """本 lane 上最近跑过的那一轮；一轮都没跑过返回 ``None``。"""
    sql = (
        f"SELECT * FROM {_ROUND_TABLE} WHERE lane = :lane "
        f"ORDER BY ran_at DESC LIMIT 1"
    )
    async with get_session() as s:
        result = await s.execute(text(sql), {"lane": lane})
        row = result.mappings().first()
    if row is None:
        return None
    return WorldRound(**{k: row[k] for k in WorldRound.model_fields})


async def world_ledger(*, lane: str, now: datetime) -> str:
    """账本：这段时间已经发生过的、和还没到的，一件一行。

    这是喂给它的**全部**输入。没有姐妹的状态、没有上一版世界叙述、没有它自己上
    一轮说过什么——它要判断的是"这个点该不该冒出点新东西"，多喂的每一样都会把它
    推回去写叙述。
    """
    items = await list_upcoming_between(
        lane=lane, since=now - LEDGER_LOOK_BACK, until=now + LEDGER_LOOK_AHEAD
    )
    if not items:
        return "（账上现在什么都没有）"
    lines = []
    for item in items:
        when = item.due_at.astimezone(now.tzinfo).strftime("%m-%d %H:%M")
        where = f"（{item.place}）" if item.place else ""
        mark = "已经发生" if item.due_at <= now else "还没到"
        lines.append(f"- {when} {item.what}{where} · {mark}")
    return "\n".join(lines)


def build_world_runner() -> AgentRunner:
    """本轮的 agent。模块级函数，测试替身从这里换掉，不碰真模型。"""
    return AgentRunner(_WORLD_ROUND_CFG, tools=WORLD_ROUND_TOOLS)


async def run_world_round(*, lane: str, now: datetime) -> WorldRound | None:
    """跑一轮 world；离上一轮不够久就一句模型都不调，返回 ``None``。

    整段在排他占用里：两条拍打到同一个 lane 时后到的排队，等前一轮跑完再读上一轮
    的时间——不然两拍会各自读到"还没跑过"、双双跑一轮。

    **传进来的 ``now`` 先落到轮次网格上**（:func:`app.living.anchor.anchor_on_grid`）。
    这一轮先写 ``Upcoming``、后写 ``WorldRound``，中间崩掉下一拍会重跑；而
    ``item_id`` 是从 ``what|place|due_at`` 派生的、``due_at = now + in_minutes``——
    锚一动 ``due_at`` 就动、派生 id 跟着动，幂等直接被击穿：账上出现两件「快递送到
    门口」，只差几分钟，事后根本看不出是重复。锚落在格上，重跑算出的是同一个
    ``due_at``、同一个 ``item_id``，CAS 把第二次挡成 no-op。

    ``max_retries=1``：core 的 ``run`` 会把整轮 ReAct 包在 ``@retry`` 里，一次模型
    瞬时失败会整轮重放、重放已经执行过的 durable 写。派生 id 让重放无害，但重放
    仍然是白花的一次钱，而且这一轮本来就低频、下一拍再来就行。
    """
    minutes = await world_round_minutes()
    interval = timedelta(minutes=minutes)
    anchor = anchor_on_grid(now, minutes=minutes)
    async with hold(world_round_lock_key(lane)):
        last = await latest_world_round(lane=lane)
        if last is not None and anchor - last.ran_at < interval:
            return None

        ledger = await world_ledger(lane=lane, now=anchor)
        context = AgentContext(
            features={
                FEATURE_LANE: lane,
                FEATURE_NOW: anchor.isoformat(),
                FEATURE_WRITTEN: [],
            }
        )
        reply = await build_world_runner().run(
            [Message(role=Role.USER, content=f"账上现在是这样：\n{ledger}")],
            context=context,
            max_retries=1,
        )

        round_ = WorldRound(
            lane=lane,
            round_id=anchor.isoformat(timespec="minutes"),
            ran_at=anchor,
            produced=len(set(context.features[FEATURE_WRITTEN])),
            said=reply.text().strip(),
        )
        await insert_idempotent(round_)
        return round_
