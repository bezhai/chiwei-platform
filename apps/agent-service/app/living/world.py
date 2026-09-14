"""world 的轮次 —— 这个世界本身是怎么往前走的。它有两件事可做，一轮什么都不做也正常。

**一件是让一件事发生**（:func:`expect`）：收「什么事、多久之后、在哪、持续多久」，
写进 :class:`~app.living.records.Upcoming`，到点由交付那一步变成真的发生
（:mod:`app.living.calendar`）。**另一件是维护那棵文档树**
（:mod:`app.living.documents`）：地方长什么样、人是谁、设定是什么，都是文件。

**它不写世界叙述。** 上一代每轮被要求描述此刻的世界，14 天 prod 实测烧掉总消耗约一半
去写「水槽水珠成浅印」这类静物记账，那些字没有任何一个变成她能遇上的事。设定写进文档
只写一次、之后一直在；这一轮该不该冒出点新东西是另一个问题。所以一轮只回一句「没有」
是**正常且期望的**结果。

**它不挑收件人。** :func:`expect` 没有 recipient 参数，签名里也不会有。谁感知得
到由位置说了算（:func:`app.living.place.reach_between`），那是客观事实，不是 world
的判断——旧 world 亲手挑收件人，信息差就没有归属人了。

**它看得见姐妹做了什么，但那不是给它编剧情用的。** 喂进去的是客观发生过的事
（:func:`app.living.happening.read_all_after`，带游标不重发），因为世界该不该有反应取
决于世界上发生了什么 —— 有人出门了，外面才谈得上下不下雨。它仍然不替她们决定说什么、
想什么：它手里没有任何一只能让某个人开口或者改变状态的工具。

**什么时候跑由三条管**（:class:`WorldPace`，两个 Dynamic Config key）：离上一轮不到
**硬下限**不跑；过了下限而且**有 world 之外的谁做过事**就跑；都没有但过了**心跳**也跑。
硬下限那条不是省钱，是防正反馈 —— world 让一件事发生、姐妹对它有反应、又把 world 叫醒，
上一代就是这个形状，一天跑两百多轮。轮次落库（:class:`WorldRound`）有三个用处，缺一个
都会疼：节奏判断要读上一轮什么时候跑的（不落库的话每次发版都白跑一轮）、重放要有幂等
的依据、验收要能逐条列出「哪几轮说了没有、哪几轮产出了什么」。

**它也有自己的连续上下文**（:mod:`app.living.continuity`，跟三姐妹同一个命名空间、
``actor='world'``）：每轮送到它眼前的只有新发生的事，账本和文档目录读一百遍字字一样，
只在界桩上重铺（:func:`world_state`）。

prompt 在 Langfuse（:data:`WORLD_ROUND_PROMPT_ID`），不硬编码进代码；账本和这一轮的时刻
走 USER 那条消息而不是 prompt 变量——它们每轮都变，而 prompt 变量改名会**静默**渲染成
字面量，能少一个变量就少一个。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
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
from app.agent.trace import collect_usage
from app.capabilities.agent import AgentRunner
from app.data.session import get_session
from app.domain.thinking_cost import record_round_cost
from app.infra.cst_time import dated_clock, to_cst_full
from app.living.anchor import anchor_on_grid
from app.living.continuity import (
    TranscriptConflict,
    commit_moment_transcript,
    load_moment_transcript,
    load_trim_policy,
    next_transcript,
    transcript_key,
    trim_for_round,
)
from app.living.documents import DOCUMENT_TOOLS, documents_root, list_tree
from app.living.happening import (
    anyone_acted_since,
    read_all_after,
    seq_before,
)
from app.living.records import WORLD_ACTOR, Happening, _require_aware, esc
from app.living.serial import hold
from app.living.upcoming import list_upcoming_between, schedule_upcoming
from app.runtime.data import Data, Key
from app.runtime.migrator import _table_name
from app.runtime.persist import insert_idempotent

logger = logging.getLogger(__name__)

# Langfuse prompt id（新 id，只发泳道 label，不碰 production）。正文**一个变量都不
# 引用**：时刻和账本都走 USER 消息，用的是这一轮自己的锚。Agent 仍会无条件注入
# currDate / currTime，但那取的是模型调用那一刻现取的钟、不是锚，两者跨午夜会打架
# ——正文不引用它们，注入的就只是没人要的 kwargs。
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

# 节奏三条，两个 Dynamic Config key。改它们不用重新部署。
#
#   * **心跳** 最长这么久必醒一次。世界自己也会有事发生（该下雨了、该有人来敲门），
#     不能只在姐妹动了的时候才动。
#   * **硬下限** 两轮之间绝不短于这个数。**这条不是省钱，是防正反馈**：world 让一件
#     事发生 → 姐妹对它有反应 → 又唤醒 world。上一代就是这个形状，一天跑两百多轮。
#     没有它，"有事就提前"会退化成"有多少事就跑多少轮"。
#
# 单一间隔（旧的 ``living_world_round_minutes``，默认 60）已经删掉：那个数一调大世界
# 就迟钝，一调小就一直在空转，而这两件事本来就该由两个数各管一头。
LIVING_WORLD_HEARTBEAT_MINUTES_KEY = "living_world_heartbeat_minutes"
DEFAULT_WORLD_HEARTBEAT_MINUTES = 30
LIVING_WORLD_FLOOR_MINUTES_KEY = "living_world_floor_minutes"
DEFAULT_WORLD_FLOOR_MINUTES = 10

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

# **没有"这一轮改了几份文档"这个计数。** 想要过，砍了：数它只有两条路，一条是数工具
# 调用次数（失败的也算进去，于是看板上比实际做的多），另一条是让文档那几只手绑上轮次
# context 好往里记（给一个本来不需要 context 的层加一个失败面，只为填一个计数器）。
# 文档改了什么的权威记录是那棵树本身 —— 目录可以直接打开看，旁路那条 CronJob 的 commit
# 历史就是世界的演化史，每一次写入还各有一行日志。

# 派生 item_id 的命名空间，随手换会让历史 item 全部对不上。
_ITEM_ID_NS = uuid.UUID("6f5d1c2e-9a3b-4d7e-8c10-5b2f4a6e9d31")


class WorldRound(Data):
    """world 跑过的一轮：什么时候跑的、产出了几件事、最后说了什么。

    自然键 ``(lane, round_id)``，纯 append 无版本链——一轮跑完就是跑完了，没有
    "改一条旧轮次"的语义。``round_id`` 取本轮时间锚（精确到分），所以同一分钟里
    的重放落回同一行。

    ``next_seq`` 是**它读到哪了**：这一轮喂进去的是 ``seq > 上一轮的 next_seq`` 那些事。
    住在这一行上而不是另开一张表，是因为它跟这一轮同生共死 —— 游标推了而上下文没写成
    的话，那一批发生过的事就此对它永久消失，所以两者在同一个事务里落地。

    **NULL 是"这一列还不存在时写的"，不是"读到 0"**（:func:`_resume_from`）。两者在
    ``or 0`` 底下长得一模一样，而后果差一整部历史。

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
    # 它读到哪了。可空是因为这是后加的列：``ALTER TABLE ADD COLUMN`` 给已有行留的是
    # NULL，声明成 ``int`` 会让那些行一读出来就 ValidationError。NULL 的含义是
    # **"没有游标可接"**，由 :func:`_resume_from` 翻译成"只读最近这一段"。
    next_seq: int | None = None

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
        str,
        Field(
            description="发生在哪，层级路径如「家/门口」；"
            "写一整片（「学校」）就是那一片里的人都碰得上；"
            "不属于任何地方（天黑、台风）就留空"
        ),
    ] = "",
    lasts_minutes: Annotated[
        int,
        Field(
            description="这件事会持续多久（分钟）。一瞬间的事（快递到了、有人敲门）"
            "填 0；会持续一段的（下雨、停电、街上在办庙会）填它大概持续多久"
        ),
    ] = 0,
) -> str:
    """让一件事在世界上发生。

    只说**是什么、在哪、多久之后、持续多久**。谁会碰上它不用你管——到点了它自己会
    发生，在场的人自然感知得到。

    **地点就是这件事的范围**：写一个具体位置（「家/厨房」）就是那儿的人碰得上；写一
    整片（「学校」）就是这片里的人都碰得上；不属于任何地方的（天黑了、台风来了）
    **留空**，那样在哪的人都碰得上。别为了填而编一个地点。

    **持续多久决定后到的人知不知道。** 填 0 的事是一瞬间的：它发生的那一刻在场的人
    知道，之后走进来的人不知道，这对「快递送到门口」是对的。会持续一段的事
    （下雨、停电、庙会）必须填上它大概持续多久 —— 不填的话，下雨开始时不在场的人
    走进来会以为天是晴的，而且再也没有别的途径知道。

    in_minutes 必须在 1～4320 之间（最近 1 分钟后、最远 3 天后）。超出范围会
    报错，请改填一个范围内的值重调。

    Args:
        what: 将要发生的这件客观事，一句自然语言。
        in_minutes: 多少分钟之后发生（1 ≤ in_minutes ≤ 4320）。
        place: 发生在哪 / 多大一片；留空 = 到处。
        lasts_minutes: 持续多久；0 = 一瞬间的事。

    Returns:
        一句确认文本。
    """
    if not (EXPECT_MIN_MINUTES <= in_minutes <= EXPECT_MAX_MINUTES):
        raise ValueError(
            f"expect 的 in_minutes={in_minutes} 不在 {EXPECT_MIN_MINUTES}～"
            f"{EXPECT_MAX_MINUTES} 之间。请改填一个范围内的值重调。"
        )
    if lasts_minutes < 0:
        raise ValueError(
            f"expect 的 lasts_minutes={lasts_minutes} 是负数。"
            "一瞬间的事填 0，会持续一段的填它大概持续多少分钟。"
        )
    lane, now = _round_scope()
    due_at = now + timedelta(minutes=in_minutes)
    where = place.strip() or None
    # 持续到什么时候在**排它的这一刻**就定死，跟到期交付晚了几十秒无关 —— 跟
    # ``occurred_at`` 取 ``due_at`` 而不是取交付时的 ``now`` 是同一条。
    lasts_until = due_at + timedelta(minutes=lasts_minutes) if lasts_minutes else None
    item_id = derive_upcoming_id(what=what, place=where, due_at=due_at)
    if not await schedule_upcoming(
        lane=lane,
        item_id=item_id,
        what=what,
        due_at=due_at,
        place=where,
        lasts_until=lasts_until,
    ):
        return f"「{what}」已经在账上了，没有重复写"
    _written_slot().append(item_id)
    return f"记下了：{due_at.strftime('%m-%d %H:%M')} 「{what}」"


# 它这一轮手里的东西：一棵文档树（世界的设定集）加一只"让一件事发生"。
#
# ``update_outline`` / ``describe_place`` / ``npc_visit`` 这些都不需要 —— 全是文档的
# 写入。NPC 登场就是让一件事发生（「林小满来敲门」），她是谁写在 ``人/林小满`` 里。
WORLD_ROUND_TOOLS = [*DOCUMENT_TOOLS, expect]

# 哪些返回是素材（过期了再读一次就有），哪些要留着。裁剪层不给默认值，所以这两张表
# 必须在这儿写全；用例 ``test_every_hand_it_has_is_classified`` 钉住两份正好覆盖
# ``WORLD_ROUND_TOOLS``。
#
# 读回来的设定是素材：它当时读过、想过，结论已经变成它自己的话留在上下文里；原文过期
# 了再读一次就有。**它自己改过什么则要留着** —— 那是"这件事到底做成了没有"的唯一记录，
# 裁掉它会照着一个不知道成没成的动作再来一遍。
WORLD_MATERIAL_TOOLS = frozenset({"list_documents", "read_document"})
WORLD_KEPT_TOOLS = frozenset(
    {"write_document", "edit_document", "delete_document", "expect"}
)


@dataclass(frozen=True)
class WorldPace:
    """world 这一轮的节奏：最长多久必醒一次，两轮之间最少隔多久。"""

    heartbeat_minutes: int
    floor_minutes: int


async def world_pace() -> WorldPace:
    """这一轮按哪套节奏；配脏了**整套**退回默认值并记一行。

    退回是整套不是逐项：两个数之间有约束（下限不能大于心跳，否则心跳永远够不着），
    逐项修补会拼出一套谁也没设计过的节奏，而它的表现是"world 要么不醒要么一直醒"。

    Dynamic Config 的拉取是同步 httpx（10s 缓存），走 ``asyncio.to_thread`` 避免缓存
    刷新那一次阻塞事件循环（与 :mod:`app.living.calendar` 同口径）。
    """

    def read() -> WorldPace:
        return WorldPace(
            heartbeat_minutes=dynamic_config.get_int(
                LIVING_WORLD_HEARTBEAT_MINUTES_KEY,
                default=DEFAULT_WORLD_HEARTBEAT_MINUTES,
            ),
            floor_minutes=dynamic_config.get_int(
                LIVING_WORLD_FLOOR_MINUTES_KEY,
                default=DEFAULT_WORLD_FLOOR_MINUTES,
            ),
        )

    pace = await asyncio.to_thread(read)
    if pace.floor_minutes <= 0 or pace.heartbeat_minutes < pace.floor_minutes:
        logger.warning(
            "world 的节奏配置不成立（下限得是正数、而且不能大于心跳）：%r；"
            "本次整套退回默认值",
            pace,
        )
        return WorldPace(
            heartbeat_minutes=DEFAULT_WORLD_HEARTBEAT_MINUTES,
            floor_minutes=DEFAULT_WORLD_FLOOR_MINUTES,
        )
    return pace


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


async def _resume_from(
    last: WorldRound | None, *, lane: str, now: datetime
) -> int:
    """这一轮从哪个 seq 往后读。**没有游标可接就只读最近这一段，不是从世界的开头。**

    两种情形走到这儿，共同点是"不知道该从哪儿接"：这条泳道 world 一轮都没跑过
    （``last is None``），和 ``next_seq`` 这一列还不存在时写的那些轮次（值是 NULL）。
    它们**都不是**"读到 0"。

    从 0 起算的后果实测过（coe-living，2026-09-14 加列那天）：第一轮开始逐轮补读两周前
    的事，一次 200 条、5508 条积压要 28 轮，每轮约 0.15 美元 —— 而且中间每一轮
    ``anyone_acted_since`` 都为真，于是它一直按硬下限跑，要补将近五个小时。它要判断的是
    "这个点该不该冒出点新东西"，两周前谁说了什么对这个判断没有用，只会让它照着过时的剧情
    排事。跳过的那一段它本来也从没拿到过 —— 旧 world 根本不读 happening。

    **边界用 :data:`LEDGER_LOOK_BACK`，不是"从此刻起算"。** 一条崭新的泳道第一轮该看得见
    刚刚发生的那几件事；"从此刻起算"会让每条新泳道的第一轮凭空瞎一次。这个窗口跟账本
    往回看的那一头是同一个数，因为问的是同一件事：**多久以前的事还值得它现在过问**。
    """
    if last is not None and last.next_seq is not None:
        return last.next_seq
    return await seq_before(lane=lane, at=now - LEDGER_LOOK_BACK)


async def world_ledger(*, lane: str, now: datetime) -> str:
    """账本：这段时间已经发生过的、和还没到的，一件一行。

    它答的是"这件事是不是已经排过了"，所以**只有安排，没有世界的样子**：没有上一版
    世界叙述、没有对此刻的描写。多喂那些会把它推回去写叙述，而写叙述正是上一代烧掉
    一半消耗的地方。

    它跟设定集一起铺在界桩上（:func:`world_state`），不是每轮重发 —— 账上有什么读一
    百遍字字一样。每轮送到它眼前的是新发生的事，那一份在 :func:`_render_happened`。
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


def world_transcript_key(lane: str) -> str:
    """world 的连续上下文键。跟三姐妹在同一个命名空间里，它也是这个世界里的一个存在。"""
    return transcript_key(lane=lane, actor=WORLD_ACTOR)


def _line(h: Happening, *, now: datetime) -> str:
    """一件事在 world 眼里的一行。

    **它不站在任何地方**，所以这里不走三档裁剪（:func:`app.living.happening.perceive`）
    —— 那一套答的是"在场的人听见了什么"，而 world 要知道的是世界上客观发生了什么。

    ``content`` 过 :func:`app.living.records.esc`：这一条上有逐字通道 ——
    :func:`app.living.outside.look_outside` 把天气、番名从外部数据源逐字拼进 content。
    ``actor`` / ``place`` 不过，它们是 persona id 和世界的地点路径，取值由代码定死。
    """
    where = h.place if h.place else "（没记下地点）"
    who = "" if h.actor == WORLD_ACTOR else f"{h.actor} "
    return f"{dated_clock(h.occurred_at, now=now)} {where} {who}{esc(h.content)}"


def _render_happened(rows: list[Happening], *, now: datetime) -> str:
    if not rows:
        return "这段时间世界上没什么动静。"
    lines = "\n".join(_line(h, now=now) for h in rows)
    return f"这段时间发生了这些：\n{lines}"


async def world_state(*, lane: str, now: datetime) -> str:
    """界桩上重铺的那一份：账上有什么 + 设定集里有哪些文件。

    **这两样读一百遍字字一样**，所以跟她那边的状态快照放在同一个位置 —— 只在清理
    那一下作为新起点重铺一次，不是每轮重发。每轮送到它眼前的只有新发生的事。

    文档只给**目录不给正文**：正文按需 read，那正是这棵树能不随运行时间增长的原因。
    """
    ledger = await world_ledger(lane=lane, now=now)
    tree = await asyncio.to_thread(list_tree, documents_root())
    return f"账上现在是这样：\n{ledger}\n\n{tree}"


def build_world_runner() -> AgentRunner:
    """本轮的 agent。模块级函数，测试替身从这里换掉，不碰真模型。"""
    return AgentRunner(_WORLD_ROUND_CFG, tools=WORLD_ROUND_TOOLS)


async def run_world_round(*, lane: str, now: datetime) -> WorldRound | None:
    """跑一轮 world；这会儿不该跑就一句模型都不调，返回 ``None``。

    **什么时候跑，三条一起管**（:class:`WorldPace`）：

      * 离上一轮不到**硬下限** —— 不跑。这条先于一切。
      * 过了硬下限，而且**有 world 之外的谁做过事** —— 跑（提前那一轮）。
      * 都没有，但过了**心跳** —— 跑。世界自己也会有事发生。

    "有谁做过事"排除 ``actor = 'world'``（:func:`app.living.happening.anyone_acted_since`），
    否则它会被自己让发生的那件事叫醒，一天跑两百多轮。

    整段在排他占用里：两条拍打到同一个 lane 时后到的排队，等前一轮跑完再读上一轮的
    时间——不然两拍会各自读到"还没跑过"、双双跑一轮。

    **``now`` 先落到以硬下限为步长的网格上**（:func:`app.living.anchor.anchor_on_grid`）。
    两件事同时要：

      * *幂等*。这一轮先写 ``Upcoming``、后写 ``WorldRound``，中间崩掉下一拍会重跑；
        而 ``item_id`` 是从 ``what|place|due_at`` 派生的、``due_at = 锚 + in_minutes``
        —— 锚一动派生 id 就动，账上会出现两件只差几分钟的「快递送到门口」，事后根本
        看不出是重复。落在格上，重跑算出同一个 id，CAS 把第二次挡成 no-op。
      * *轮次身份唯一*。``round_id`` 取这个锚，而两轮之间至少隔一个硬下限、网格步长
        正好是那个数，所以两轮**必然**落在不同格上。这条不是讲究：成本记账是
        ``ON CONFLICT DO NOTHING``，撞了的话第二轮那笔 token 一行日志都不留地消失，
        而"这一天花了多少"只能从那张表数。

    **``WorldRound`` 和连续上下文在同一个事务里落地。** 游标（``next_seq``）在那一行
    上，上下文里是它读到的那些事 —— 分两次写的话，游标推了而上下文没写成时，那一批
    发生过的事就此对它永久消失，而且一句报错都没有。

    ``max_retries=1``：core 的 ``run`` 会把整轮 ReAct 包在 ``@retry`` 里，一次模型
    瞬时失败会整轮重放、重放已经执行过的 durable 写。派生 id 让重放无害，但重放
    仍然是白花的一次钱，而且这一轮本来就低频、下一拍再来就行。
    """
    pace = await world_pace()
    anchor = anchor_on_grid(now, minutes=pace.floor_minutes)
    async with hold(world_round_lock_key(lane)):
        last = await latest_world_round(lane=lane)
        after_seq = await _resume_from(last, lane=lane, now=anchor)
        if last is not None:
            since = anchor - last.ran_at
            if since < timedelta(minutes=pace.floor_minutes):
                return None
            if since < timedelta(minutes=pace.heartbeat_minutes) and not (
                await anyone_acted_since(lane=lane, after_seq=after_seq)
            ):
                return None

        rows, next_seq = await read_all_after(lane=lane, after_seq=after_seq)
        round_id = anchor.isoformat(timespec="minutes")

        key = world_transcript_key(lane)
        history, history_ver = await load_moment_transcript(key)
        trim_policy = await load_trim_policy()
        # 账本和文档目录读一百遍字字一样，所以只在界桩上重铺一次；每轮送到它眼前的
        # 只有新发生的事。跟她那边的状态快照是同一个位置。
        history = trim_for_round(
            history,
            material_tools=WORLD_MATERIAL_TOOLS,
            now=anchor,
            state=await world_state(lane=lane, now=anchor),
            policy=trim_policy,
        )
        stimulus = Message(
            role=Role.USER,
            content=(
                # 时刻必须自己给：一轮什么动静都没有时，它一个时间线索都没有 ——
                # 而那正是它最该判断"这个点该不该冒出点什么"的时候。用这一轮的锚不
                # 现取钟，跟派生 id、跟账本三者同源。
                f"现在 {to_cst_full(anchor.isoformat())}。\n\n"
                f"{_render_happened(rows, now=anchor)}"
            ),
        )
        context = AgentContext(
            # 它这一条线在 langfuse 里读成一条流，逐轮翻起来才不用大海捞针。
            session_id=f"living-world:{lane}",
            features={
                FEATURE_LANE: lane,
                FEATURE_NOW: anchor.isoformat(),
                FEATURE_WRITTEN: [],
            },
        )
        produced: list[Message] = []
        # 用量落 durable PG，理由跟 moment 一样（见 app.living.moment）：langfuse 会系统性
        # 丢 trace，"这一天花了多少"只能从 PG 数。
        with collect_usage() as usage:
            reply = await build_world_runner().run(
                [*history, stimulus],
                context=context,
                max_retries=1,
                transcript_sink=produced,
            )

        await record_round_cost(
            lane=lane,
            actor=WORLD_ACTOR,
            round_id=round_id,
            usage=usage,
            observed_at=anchor.isoformat(),
        )

        round_ = WorldRound(
            lane=lane,
            round_id=round_id,
            ran_at=anchor,
            produced=len(set(context.features[FEATURE_WRITTEN])),
            said=reply.text().strip(),
            next_seq=next_seq,
        )
        async with get_session() as session:
            await insert_idempotent(round_, session=session)
            try:
                await commit_moment_transcript(
                    key,
                    next_transcript(history, [stimulus, *produced], policy=trim_policy),
                    expected_ver=history_ver,
                    session=session,
                )
            except TranscriptConflict:
                # 同一个 lane 上两轮 world 在并发跑 —— 进程内排他占用的前提破了。
                # 整个事务一起回滚：游标和上下文本来就该同生共死。
                logger.exception("world 的上下文在这一轮跑的时候被别人写过了：%s", key)
                raise
        return round_
