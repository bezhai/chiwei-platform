"""一缝 —— 每十分钟她回到自己身上一次，回答同一个问题。

**问题只有一个：有没有什么把我从现在这件事里带走？** 默认答「继续」，一个词，零工
具、零写库，是最便宜的那种轮次。「默认继续」靠 prompt 让她自己选，**不是**代码里那
句"没有新事件就跳过这一轮"——那是替她做决定，而且它恰好是前六次范式失败的形状：
一旦由代码判断她该不该醒，她就不再是一直活着的，而是某个组件每轮算出来的产物。

**被带走时她换的是一件事，不是一个时长。** 所以这里的工具签名里**没有任何分钟数**
（``tests/living/test_moment.py`` 把这条钉死）。真人对"多久"没有内感受：他不会想
"我要花四十分钟洗澡"，他只是去洗澡了。问她要一个数字就是把生活切成日程表。

**她记得住，靠的是状态快照而不是 transcript。** 见 :mod:`app.living.snapshot`——
四层当下事实，一处压缩都没有，所以撞不了顶也不会失真。她唯一能主动带走的是"心里
挂着没了结的事"那份清单（:mod:`app.living.loose_ends`），由 :func:`keep_in_mind`
重写。

**挂线头是独立的一件事，不绑在 ``switch_to`` 上。** 「是否换事」不等于「是否记住」：
绫奈跟她说"周末陪我去祭典"，她手上的书没放下（这一缝答「继续」），但她记住了——这是
真人每天都在做的事。把清单绑在换事情上，这条感知在游标推进之后就永久消失了：她自己
最近那十二条里只有她**自己**说做的，别人说的话不在里面，谁也救不回来。而"跨缝因果
延续"恰好是整个实验最想验证的东西。

**每缝串行。** 一个人不能同时想两件事——这是物理事实，不是给她加冷却。用 T1 的
:func:`app.living.serial.hold`，后到的排队等前一次做完（两条路：固定的钟，和
:mod:`app.living.nudge` 那条被叫醒提前来的）。

**一缝的身份是格子，不是钟表上那一瞬**（:func:`app.living.anchor.anchor_on_grid`）。
副作用先落库、``LifeMoment`` 后落库，中间崩掉的话下一拍会重跑；锚落在格上，重试拿到
的是同一缝、所有派生 id 原样对上，重放退化成无害的 no-op。残余缺口（重放产出了**不
一样**的动作时两边都会留下）写在 anchor 那边的 docstring 里。

**提前来的那一缝不落在格子上**，它的身份是**把她叫来的那条消息**（``nudged_by``）。
理由见 :func:`run_moment`：撞不上常规缝的 id，而且同一条消息只能把她叫来一次。

**"她这一缝的钟点"和"这一缝第几个落地"是两个问题，各有各的列。** 排队是正常的
（``hold`` 让后到的等，不丢），所以提前缝先跑完、常规缝后跑完时，落地顺序跟
``began_at`` 顺序**是反的**——常规缝的 ``began_at`` 是它的格子，可能比先落地那个提前
缝的真实时刻还早。游标问的是"最后落地的那一缝读到哪"，拿钟点去答就会把后落地那一缝
读过的整段丢回去（见 :class:`LifeMoment` 的 ``seq``）。

工具：

  * :func:`switch_to`    我现在改去做 X、在哪、什么把我带走的
  * :func:`keep_in_mind` 我心里挂着没了结的事，全部（哪一缝都能调）
  * :func:`say` / :func:`act`  跟姐妹说话 / 做一个她们看得见的动作
  * :func:`look_around`  够得着的地方现在怎么样
  * ``look_at_phone``    拿起手机看某条会话说了什么（:mod:`app.living.phone`）
  * ``send_message``     给手机上某条会话发一条（:mod:`app.living.mouth`）

prompt 在 Langfuse（:data:`LIFE_MOMENT_PROMPT_ID`，新 id、只发泳道 label），变量
只有两个——``persona_name`` 和 ``persona_core``。**每缝都变的东西一律走 USER 消息**：
prompt 变量没有编译期校验，改名会静默渲染成字面量，能少一个就少一个。
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
from app.data.queries.persona import find_persona
from app.data.session import get_session
from app.infra.cst_time import now_cst
from app.living.anchor import anchor_on_grid
from app.living.clock import living_lane
from app.living.happening import record_happening
from app.living.loose_ends import list_open_loose_ends, rewrite_loose_ends
from app.living.mouth import MOUTH_TOOLS
from app.living.phone import PHONE_TOOLS, commit_glances, phone_envelope
from app.living.place import Reach, reach_between_people
from app.living.records import (
    KIND_ACT,
    KIND_SPEECH,
    MEDIUM_IN_PERSON,
    _require_aware,
    legacy_null_is,
)

# 一缝里每个工具都要读的那四样 ambient 事实，定义在 scope 里（手机和嘴的工具用的
# 是同一份；放在这里会让它们反过来 import 本模块，成环）。
from app.living.scope import (
    FEATURE_GLANCES,
    FEATURE_MOMENT,
    FEATURE_PERSONA,
    FEATURE_RECORDED,
    FEATURE_SWITCHES,
    moment_scope,
    note_recorded,
)
from app.living.serial import hold
from app.living.snapshot import all_whereabouts, read_snapshot
from app.living.whereabouts import current_whereabouts, note_whereabouts

# 这两个 ambient key 是整个 living 包共用的一份定义（world 的轮次也从这里取
# lane / 时间锚），不在这里再声明一遍同名常量。
from app.living.world import FEATURE_LANE, FEATURE_NOW
from app.runtime.data import Data, Key
from app.runtime.migrator import _table_name
from app.runtime.node import node
from app.runtime.persist import insert_idempotent

logger = logging.getLogger(__name__)

# 这个家里的三姐妹。写死而不是查 ``bot_persona``：这是世界设定（谁住在这儿），不是
# 可调参数；而库里还有 ``npc:*`` 这类行，全量拉进来就是每十分钟白烧三份以上的钱。
LIVING_PERSONAS: tuple[str, ...] = ("akao", "ayana", "chinagi")

# Langfuse prompt id（新 id，只发泳道 label，不碰 production）。
LIFE_MOMENT_PROMPT_ID = "living_life_moment"

# life-model：这一缝要的是对自己处境的判断（有没有什么把我带走），不是对话能力。
# 用 life-model 而不是 offline-model，是因为**量级不一样**：life 一天 432 缝 × 三个
# 人，world 一天二十几轮。life-model 这个别名本来就是给 life 这种高频线选的，world
# 那条留在 offline-model 上，两条线的档位不该互相牵动。
# recursion_limit 8：look_around → switch_to → say/act 几步就该收口；她不该在一缝
# 里把一整段生活演完（十分钟里的一小步）。
_MOMENT_CFG = AgentConfig(
    LIFE_MOMENT_PROMPT_ID,
    "life-model",
    "living-life-moment",
    recursion_limit=8,
)

# Dynamic Config key：两缝之间至少隔多少分钟。改它不用重新部署。
LIVING_LIFE_MOMENT_MINUTES_KEY = "living_life_moment_minutes"
DEFAULT_LIFE_MOMENT_MINUTES = 10

# 钟拍得比最密的缝还密，间隔判在节点里 —— ``Source.interval`` 的秒数在 import 时
# 就固定了，想让间隔可调只能这么做（跟 :mod:`app.living.clock` 同一条理由）。
# 一分钟一拍的代价只有一次读库 + 比大小。
LIFE_MOMENT_TICK_SECONDS = 60

# 派生 id 的命名空间，随手换会让历史记录全部对不上。
_ID_NS = uuid.UUID("9c1e4f70-3b28-4a56-8d0f-1e7a2c5b6934")


class LifeMoment(Data):
    """她跑过的一缝：读到哪、感知到几条、换没换事情、为什么换、最后说了什么。

    自然键 ``(lane, persona_id, moment_id)``，纯 append 无版本链——一缝过完就是过完
    了。``moment_id`` 是这一缝的**身份**：常规缝取时间锚（精确到分，同一格的重放落回
    同一行），提前缝取把她叫来的那条消息。

    **这张表同时是游标的家。** ``next_seq`` 是她这一缝读到的最大提交序，下一缝的
    ``after_seq`` 就是它——续接不靠内存、不靠 Redis，重启后原地接上。

    **``began_at`` 和 ``seq`` 回答的是两个问题，别互相顶替。**

      * ``began_at`` = **她这一缝的『现在』**：喂给快照、手机信封和 ``FEATURE_NOW``
        的那个时刻。常规缝取格子（一缝的身份就是格子，见 :mod:`app.living.anchor`），
        提前缝没有格子可落，取真实时刻。
      * ``seq`` = **这一缝在这条轴上第几个落地**，(lane, persona) 各一条轴，在缝的
        排他占用里取号、取完到落库中间不放开占用，所以它的先后就是提交的先后。

    两者顺序**会反**，而且这是正常运转的样子：两条钟并发打到同一个人时后到的排队
    （:func:`app.living.serial.hold` 不丢），21:34 被叫来那一缝先跑完、21:35 那一拍
    的常规缝（格子 21:30）后跑完——先落地的钟点反而更晚。所以：

      * "读到哪了"必须问 ``seq``（:func:`latest_moment`）。问 ``began_at`` 会取回
        21:34 那一行的游标，把常规缝已经读过的一整段丢回去，她原样再感知一遍；
      * 常规节奏问的是"最近跑过的**哪一格**"，那是 ``began_at``
        （:func:`latest_regular_moment`）。换成 ``seq`` 就会在乱序落地后把更早的格子
        当成最近一格，已经跑过的晚格子被判成还没跑。

    ``switched`` / ``pulled_by`` / ``said`` 三列是验收口径，不是日志："逐缝看得出
    哪些是继续、哪些换了事情、换的理由是什么"只能从这张表查。光看 langfuse trace
    算不准（会丢 trace），光看 ``Whereabouts`` 看不见"跑了但没换"的那些缝。

    ``nudged`` 分开的是**两种缝**：钟点上该来的那种，和被人叫来提前的那种。这一列
    有真实的机制后果，不是标签——常规间隔只跟**上一个常规缝**比
    （:func:`latest_regular_moment`）。不分开的话，每来一条私聊就把她的固定节奏往后
    推一次：一天被搭话十次，她的十分钟就成了不定期。
    而"读到哪了"仍然跨两种缝共用一条轴（:func:`latest_moment` 不筛这一列），不然
    提前那缝读过的东西，常规缝会原样再读一遍。
    """

    lane: Annotated[str, Key]
    persona_id: Annotated[str, Key]
    moment_id: Annotated[str, Key]
    seq: int             # 这一缝第几个落地（本 lane + 本人一条轴）
    began_at: datetime   # 她这一缝的『现在』：常规缝 = 格子，提前缝 = 真实时刻
    after_seq: int       # 这一缝开始时读到哪了
    next_seq: int        # 这一缝结束时读到哪了 —— 下一缝的起点
    perceived: int       # 这一缝她感知到几条
    switched: bool       # 换事情了吗（False = 「继续」）
    pulled_by: str       # 什么把她带走的（她自己那句）；没换 = ""
    recorded: int        # 这一缝她说 / 做了几件别人感知得到的事
    doing: str           # 缝末她手上是什么事
    open_ends: int       # 缝末她心里还挂着几件
    said: str            # 她这一缝最后那句话
    nudged: bool = False  # 被叫来提前的那一缝吗（False = 钟点上该来的）

    class Meta:
        # 两条读侧形状，因为上面那两个问题各问各的：
        #   * (lane, persona_id, seq)      最后落地的那一缝 + 取下一个号的 MAX(seq)
        #   * (lane, persona_id, began_at) 最近跑过的那一格
        indexes = (
            ("lane", "persona_id", "seq"),
            ("lane", "persona_id", "began_at"),
        )

    # ``nudged`` / ``seq`` 都是后加的列，已经有数据的泳道上旧行是 NULL —— 不接住就是
    # 读一行炸一行，整条 life 循环推不动。理由和另一半防线（SQL 侧的 COALESCE）见
    # :func:`app.living.records.legacy_null_is`。
    _legacy_nudged = field_validator("nudged", mode="before")(
        classmethod(legacy_null_is(False))
    )
    # 加列之前的行统一当成 0 号：新写的缝从 1 起，所以任何一条新缝都排在它们后面，
    # 而旧行之间的先后退回加列之前唯一有过的那个口径（``began_at``，见
    # :func:`latest_moment` 的第二排序键）。``seq`` 本身**没有** pydantic 默认值：
    # 漏传一个提交序是 bug，该当场炸，不该悄悄写成 0。
    _legacy_seq = field_validator("seq", mode="before")(
        classmethod(legacy_null_is(0))
    )

    @field_validator("began_at")
    @classmethod
    def _aware_began_at(cls, v: datetime) -> datetime:
        return _require_aware("began_at", v)


class LifeMomentTick(Data):
    """life 那一拍。单字段 ``ts``——框架源循环固定按 ``data_type(ts=<iso>)`` 造
    payload，多一个必填字段就是每一拍 ValidationError **直接杀 Pod**。"""

    ts: Annotated[str, Key]

    class Meta:
        transient = True


_MOMENT_TABLE = _table_name(LifeMoment)


def life_moment_lock_key(lane: str, persona_id: str) -> str:
    """这个人在这个 lane 上的缝的排他占用 key（每人一条轴，互不阻塞）。"""
    return f"living:moment:{lane}:{persona_id}"


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _derive(*parts: str) -> str:
    return uuid.uuid5(_ID_NS, "\x1f".join(parts)).hex


@tool
@tool_error("换事情失败")
async def switch_to(
    doing: Annotated[
        str, Field(description="你现在改去做的这件事，一句话，例如「去洗澡」")
    ],
    place: Annotated[
        str, Field(description="你人在哪，层级路径如「家/浴室」「家/楼上/我房间」")
    ],
    because: Annotated[
        str, Field(description="什么把你从刚才那件事里带走的，一句话")
    ],
) -> str:
    """我现在改去做别的了。

    你被带走的时候调它。说清楚你改去做什么、人在哪、什么把你带走的。

    别报时长——你不知道自己要做多久，也不用知道。

    人挪了地方但手上还是同一件事（走廊走进教室、端着咖啡进阳台），那是 move_to。

    心里挂着什么是另一回事，走 keep_in_mind，跟换不换事没有关系。

    Args:
        doing: 你现在改去做的这件事。
        place: 你人在哪（层级路径）。
        because: 什么把你带走的。

    Returns:
        一句确认文本。
    """
    lane, now, persona_id, moment_id = moment_scope()
    what, where = doing.strip(), place.strip()
    if not what:
        raise ValueError("doing 不能是空的：说一句你改去做什么。")
    if not where:
        raise ValueError(
            "place 不能是空的：位置是别人能不能感知到你的全部依据，"
            "空位置会让你从此谁也听不见、也没人听得见你。写一个层级路径，"
            "例如「家/客厅」。"
        )

    # whereabouts 的自然键带上内容派生的后缀：同一缝里她改两次主意要落两行
    # （最新的那条才是"当前"），而重放同样的内容仍然只落一行。
    await note_whereabouts(
        lane=lane,
        persona_id=persona_id,
        moment_id=f"{moment_id}:{_derive(what, where)[:8]}",
        place=where,
        doing=what,
        noted_at=now,
    )
    get_context().features.setdefault(FEATURE_SWITCHES, []).append(
        {"doing": what, "because": because.strip()}
    )
    return f"你在 {where}，{what}。"


@tool
@tool_error("挪个地方失败")
async def move_to(
    place: Annotated[
        str, Field(description="你现在人在哪，层级路径如「学校/二年三班教室」")
    ],
) -> str:
    """我人换地方了，手上的事没变。

    走到别处、但还在做同一件事的时候调它：从走廊走进教室、端着咖啡从厨房走到阳台。

    你在哪，决定了谁看得见你、你看得见谁。人挪了而这里没挪，你在所有人眼里就还
    待在原地——你说你回教室了，她们看到的你还在走廊上。

    被别的事带走了、手上这件事本身换了，那是 switch_to；那只手也会顺带记下位置。

    Args:
        place: 你现在人在哪（层级路径）。

    Returns:
        一句确认文本。
    """
    lane, now, persona_id, moment_id = moment_scope()
    where = place.strip()
    if not where:
        raise ValueError(
            "place 不能是空的：位置是别人能不能感知到你的全部依据，"
            "空位置会让你从此谁也听不见、也没人听得见你。写一个层级路径，"
            "例如「家/客厅」。"
        )

    # 手上那件事原样带走 —— 这只手改的只有位置。没有"当前"就没有可带走的事：
    # 她还没落过位置，这一步无从下脚（第一次落位走 switch_to）。
    current = await current_whereabouts(lane=lane, persona_id=persona_id)
    if current is None:
        raise ValueError(
            "你还没定下过自己在哪，也就没有一件'照旧做着'的事可以带到新地方去。"
        )

    # 自然键跟 switch_to 同款：同一缝里挪两次要落两行（最新那条才是"当前"），
    # 而重放同样的内容仍然只落一行。
    await note_whereabouts(
        lane=lane,
        persona_id=persona_id,
        moment_id=f"{moment_id}:{_derive(current.doing, where)[:8]}",
        place=where,
        doing=current.doing,
        noted_at=now,
    )
    # **不进 FEATURE_SWITCHES**：走一步不是"什么把你从这件事里带走了"。混进去会让
    # 逐缝复盘里的换事率把单纯的走动也算成换事情。
    return f"你在 {where}，还在{current.doing}。"


@tool
@tool_error("记下心里挂着的事失败")
async def keep_in_mind(
    still_on_my_mind: Annotated[
        list[str],
        Field(
            description="你现在还挂着没了结的事，**全部**列出来（一件都没有就传空数组）"
        ),
    ],
) -> str:
    """记下我心里挂着、还没了结的事。

    **哪一缝都能调，跟你换不换手上的事没有关系。** 有人跟你说了句要紧的话、你临时
    想起一件事、你答应了谁什么——手上的书不用放下，记一笔就行。

    这是**整份清单，不是新增**：列进去的会一直跟着你，这次没列的就此了结、下一缝你
    就想不起来了。上一份清单每一缝都原样摆在你眼前，照着抄，再加上新的那件。

    你没主动记下来的东西，过几缝就真的没了——你眼前那份"刚发生过什么"只有最近一小
    段，滚出去就找不回来了。

    Args:
        still_on_my_mind: 你现在还挂着没了结的事，全部。

    Returns:
        一句确认文本。
    """
    lane, now, persona_id, moment_id = moment_scope()
    open_now = await rewrite_loose_ends(
        lane=lane,
        persona_id=persona_id,
        moment_id=moment_id,
        now=now,
        still_on_my_mind=still_on_my_mind,
    )
    carried = "、".join(e.what for e in open_now) or "没有"
    return f"心里挂着：{carried}"


@tool
@tool_error("说话失败")
async def say(
    what: Annotated[str, Field(description="你说出口的那句话，原话")],
    to: Annotated[
        list[str],
        Field(description="说给谁（可以同时对好几个人）；自言自语就传空数组"),
    ],
) -> str:
    """当面说一句话。

    写你**真的说出口的那句话**。「我和绫奈说了几句」不是说话，那是记账——记账在
    这个家里等于什么都没发生过，别人读到的就是一句空话。

    to 里的人一定听得到原话，跟她在哪没关系；同一个地方的其他人也听得见。

    Args:
        what: 你说出口的那句话，原话。
        to: 说给谁（persona_id，可以多个）。

    Returns:
        一句确认文本。
    """
    return await _record(kind=KIND_SPEECH, content=what, audience=to)


@tool
@tool_error("做这个动作失败")
async def act(
    what: Annotated[
        str, Field(description="你做的这个动作，一句自然语言，例如「把胶片摊了一茶几」")
    ],
) -> str:
    """做一个别人看得见的动作。

    同一个地方的人会看见，同一栋别处的人只知道那边有动静。

    只用来记**别人感知得到**的动作。你自己安静做的事不用调它——那是你手上的事，
    在 switch_to 里。

    **你写下的这句话，原样就是姐姐下一缝感知到的客观动静，中间没有谁替你核一遍。**
    所以这里写你做的那件事，连着它直接造成的结果一起写完整——把饭端上桌，桌上就有
    饭；推开窗，窗就开着。这些是你做出来的，照写。

    别在里面顺手替世界宣布不由这个动作产生的事：别人的身体怎么样、外面出了什么事、
    测出来是多少。「我去阳台把衣服收进来」是你做的；捎带一句「外面下过雨，衣服全湿
    了」就不是——那场雨谁也没下过，可它写进去全家就都得当真。你自己身上的感觉（冷、
    烫、手在抖）是你的，照写；但感觉不替你确立一个数字，也不替你断定别人是怎么了。

    真想让人知道你看见了什么、担心什么，就用 say 说出口——说出口的话，别人读到的是
    "你说的"，不是"事情就是这样"。

    Args:
        what: 你做的这个动作，一句自然语言。

    Returns:
        一句确认文本。
    """
    return await _record(kind=KIND_ACT, content=what, audience=[])


async def _record(*, kind: str, content: str, audience: list[str]) -> str:
    """``say`` / ``act`` 共用的落库：位置和收件人都必须是真的。

    两处 fail-loud，都是因为错了会**静默**：

      * 没定下位置 = 这条事件落在一个空地点上，谁也感知不到，而她那边一片安静；
      * 收件人写错（写了显示名、写了不存在的人）= ``audience`` 谁也匹配不上，定向
        送达那条路直接落空，只剩位置旁听——她以为自己说给了姐姐，姐姐什么都没收到。
    """
    lane, now, persona_id, moment_id = moment_scope()
    said = content.strip()
    if not said:
        raise ValueError("内容不能是空的：说一句真的话 / 写清楚你做了什么。")
    unknown = [name for name in audience if name not in LIVING_PERSONAS]
    if unknown:
        raise ValueError(
            f"{unknown} 不是这个家里的人。收件人只能是 {list(LIVING_PERSONAS)} "
            f"里的 id（不是显示名）。写错了她收不到，而且没有任何报错。"
        )
    where = await current_whereabouts(lane=lane, persona_id=persona_id)
    if where is None:
        raise ValueError(
            "你还没定下自己在哪 —— 先用 switch_to 说清楚你人在哪、在做什么，"
            "再说话或者动作。"
        )
    happening_id = "moment:" + _derive(
        persona_id, moment_id, kind, said, ",".join(audience)
    )
    await record_happening(
        lane=lane,
        happening_id=happening_id,
        actor=persona_id,
        place=where.place,
        kind=kind,
        content=said,
        occurred_at=now,
        audience=audience,
        medium=MEDIUM_IN_PERSON,
    )
    note_recorded(happening_id)
    return f"记下了：{said}"


@tool
@tool_error("看一眼周围失败")
async def look_around() -> str:
    """看一眼够得着的地方现在什么样。

    同一个地方的人，你看得见她在干嘛；同一栋别处的人，你只知道她在哪；不在这栋
    里的人，你不知道。

    Returns:
        一段自然语言描述。
    """
    lane, _now, persona_id, _moment_id = moment_scope()
    me = await current_whereabouts(lane=lane, persona_id=persona_id)
    if me is None:
        return "你还没定下自己在哪，所以什么都够不着。先用 switch_to 落个位置。"

    here: list[str] = []
    elsewhere: list[str] = []
    for other in await all_whereabouts(lane=lane):
        if other.persona_id == persona_id:
            continue
        # **人跟人比位置走 reach_between_people，不是 reach_between。** 后者有一档
        # "事情发生在一整片范围上、站在这片里的人都在场"，那是给天黑这种范围事件
        # 用的；套到人身上，一个只粗略定位到「家」的姐姐会被判成就在这屋里，她正在
        # 做什么就此泄露出去。
        reach = reach_between_people(observer=me.place, other=other.place)
        if reach is Reach.SAME_PLACE:
            here.append(f"{other.persona_id} 正在 {other.doing}")
        elif reach is Reach.SAME_BUILDING:
            # 只给位置，不给她在干嘛 —— 信息差归位置管。
            elsewhere.append(f"{other.persona_id} 在 {other.place}")

    lines = [f"你在 {me.place}。"]
    lines.append("这里还有：" + "、".join(here) + "。" if here else "这里没别人。")
    if elsewhere:
        lines.append("这栋里别处：" + "、".join(elsewhere) + "。")
    return "\n".join(lines)


# 手上的事 + 手机 + 嘴，三样合在一起才是"她这一缝能做的全部"。手机和嘴住在自己的
# 模块里（:mod:`app.living.phone` / :mod:`app.living.mouth`），只在这里汇成一份——
# 拆两份工具集就会出现"某条路进来的那一缝她没有手机"这种谁也想不到的差别。
MOMENT_TOOLS = [
    switch_to,
    move_to,
    keep_in_mind,
    say,
    act,
    look_around,
    *PHONE_TOOLS,
    *MOUTH_TOOLS,
]


# ---------------------------------------------------------------------------
# 循环
# ---------------------------------------------------------------------------


async def life_moment_minutes() -> int:
    """两缝之间至少隔多少分钟；没配 / 配脏退回默认值。

    Dynamic Config 的拉取是同步 httpx（10s 缓存），走 ``asyncio.to_thread`` 避免
    缓存刷新那一次阻塞事件循环（与 :mod:`app.living.world` 同口径）。
    """
    minutes = await asyncio.to_thread(
        dynamic_config.get_int,
        LIVING_LIFE_MOMENT_MINUTES_KEY,
        default=DEFAULT_LIFE_MOMENT_MINUTES,
    )
    if minutes <= 0:
        logger.warning(
            "dynamic config %s = %r 不是正整数；本次退回 %d 分钟",
            LIVING_LIFE_MOMENT_MINUTES_KEY,
            minutes,
            DEFAULT_LIFE_MOMENT_MINUTES,
        )
        return DEFAULT_LIFE_MOMENT_MINUTES
    return minutes


async def _one_moment(sql: str, params: dict) -> LifeMoment | None:
    async with get_session() as s:
        row = (await s.execute(text(sql), params)).mappings().first()
    if row is None:
        return None
    return LifeMoment(**{k: row[k] for k in LifeMoment.model_fields})


async def latest_moment(*, lane: str, persona_id: str) -> LifeMoment | None:
    """这个人**最后落地**的那一缝，两种缝都算；一缝都没跑过返回 ``None``。

    这是"读到哪了"的来源：感知游标跨两种缝共用一条轴，被叫来那缝读过的东西，常规缝
    不该原样再读一遍。

    **按 ``seq`` 排，不按 ``began_at``。** 后者是她那一缝的钟点，跟落库先后无关：
    提前缝（真实时刻）先跑完、常规缝（格子，钟点更早）后跑完是排队的正常结果，按钟点
    取就会取回提前缝那一行，游标退回去，常规缝读过的一整段被她原样再感知一遍。

    ``COALESCE(seq, 0)`` 是加列的另一半防线：DESC 排序下 pg 把 NULL 放**最前**，
    不接住的话已有数据的泳道会一直取回某一条旧行，游标从此钉死在那儿。旧行统一是
    0 号，它们之间的先后退回 ``began_at``——加列之前唯一有过的那个口径。
    """
    sql = (
        f"SELECT * FROM {_MOMENT_TABLE} "
        f"WHERE lane = :lane AND persona_id = :persona_id "
        f"ORDER BY COALESCE(seq, 0) DESC, began_at DESC LIMIT 1"
    )
    return await _one_moment(sql, {"lane": lane, "persona_id": persona_id})


async def latest_regular_moment(
    *, lane: str, persona_id: str
) -> LifeMoment | None:
    """这个人最近跑过的那**一格**（钟点上该来的那种缝）；一个都没有返回 ``None``。

    间隔判断只认它。用"最近一缝"判的话，每被人叫来一次就把她的固定节奏往后推一次
    ——她一天被搭话十次，那十分钟的节拍就成了不定期的。

    **这里按 ``began_at`` 排，跟 :func:`latest_moment` 不是同一个问题**：那边问"最后
    落地的是谁"，这边问"跑过的格子里最晚的是哪一格"。常规缝的 ``began_at`` 就是它的
    格子，所以这条排序问的正是后者。换成 ``seq`` 的话，乱序落地之后更早的格子会被当
    成最近一格，已经跑过的晚格子被判成还没跑，白烧一次模型而且落不了库（自然键撞上）。
    """
    sql = (
        f"SELECT * FROM {_MOMENT_TABLE} "
        f"WHERE lane = :lane AND persona_id = :persona_id "
        # 旧行的 nudged 是 NULL（后加的列没有 DB 默认值）：NULL 既不等于 false
        # 也不等于 true，不 COALESCE 的话历史全被过滤掉，她每一拍都当成从没跑过。
        f"AND COALESCE(nudged, false) = false "
        f"ORDER BY began_at DESC LIMIT 1"
    )
    return await _one_moment(sql, {"lane": lane, "persona_id": persona_id})


async def _next_moment_seq(*, lane: str, persona_id: str) -> int:
    """这一缝在 (lane, persona) 轴上的落地号。

    **只能在缝的排他占用里调**（:func:`life_moment_lock_key`）：取号和落库之间不放开
    占用，所以号的先后就是提交的先后，可见的号永远是一段连续前缀，游标推到"最后落地
    那一缝"不会把一条还在飞的记录越过去。这和
    :func:`app.living.serial.append_in_commit_order` 是同一条论证——不复用它，是因为
    它自己要 ``hold`` 一次，而这一缝已经占着同一条 key，``asyncio.Lock`` 不可重入，
    嵌套等于永久自锁死。

    旧行的 ``seq`` 是 NULL，``MAX`` 直接忽略 NULL，所以加列后的第一缝拿到 1 —— 比任何
    旧行（读出来是 0）都大。
    """
    sql = (
        f"SELECT COALESCE(MAX(seq), 0) + 1 FROM {_MOMENT_TABLE} "
        f"WHERE lane = :lane AND persona_id = :persona_id"
    )
    async with get_session() as s:
        result = await s.execute(
            text(sql), {"lane": lane, "persona_id": persona_id}
        )
        return int(result.scalar_one())


async def moment_ran(*, lane: str, persona_id: str, moment_id: str) -> bool:
    """这一缝跑过了吗（按身份问，不按时间）。"""
    sql = (
        f"SELECT 1 FROM {_MOMENT_TABLE} WHERE lane = :lane "
        f"AND persona_id = :persona_id AND moment_id = :moment_id LIMIT 1"
    )
    async with get_session() as s:
        row = (
            await s.execute(
                text(sql),
                {"lane": lane, "persona_id": persona_id, "moment_id": moment_id},
            )
        ).first()
    return row is not None


def _persona_core_var(core: str | None) -> str:
    """SYSTEM 变量 ``{{persona_core}}`` 的值：人写的那份人设正文；空白如实说。

    有就**原文直传、不裹任何措辞**。空白（列 NOT NULL，但可能是空串）绝不能渲染
    出一个空洞——她 90% 时间在 rest 的第二个独立病因就是这份东西全仓只有每周一次
    的 persona_review 读过：不是想做事做不了，是根本没想起来自己有想做的事。

    缺省措辞零剧情事实：人设内容全部从数据来。
    """
    if not core or not core.strip():
        return "（还没有为她写下这份人设正文——这一缝没有可对照的底色。）"
    return core.strip()


def build_moment_runner() -> AgentRunner:
    """本缝的 agent。模块级函数，测试替身从这里换掉，不碰真模型。"""
    return AgentRunner(_MOMENT_CFG, tools=MOMENT_TOOLS)


async def run_moment(
    *, lane: str, persona_id: str, now: datetime, nudged_by: str | None = None
) -> LifeMoment | None:
    """推进这个人的一缝；这一缝不该跑就一句模型都不调，返回 ``None``。

    整段在排他占用里：一个人不能同时想两件事。该不该跑也判在里面——不然两条路会各自
    读到"还没跑过"、双双跑一缝。

    **两种缝，身份和"该不该跑"的判据都不一样。**

    *钟点上该来的那种*（``nudged_by is None``）：``now`` 先落到间隔网格上
    （:func:`app.living.anchor.anchor_on_grid`），锚就是这一缝的身份。副作用先落库、
    这条记录后落库，中间崩掉下一拍会重跑——锚落在格上，那一拍算出的还是同一缝，所有
    派生 id 原样对上，同样的动作重放一遍写不出新行。该不该跑，只跟**上一个常规缝**
    比间隔（:func:`latest_regular_moment`）。

    *被人叫来提前的那种*（``nudged_by`` 是把她叫来那条消息的 id）：身份是
    ``nudge:<那条消息>``，**不落格子**。这一个决定同时解掉三件事：

      1. 跟同一分钟的常规缝**撞不上 id**（一个是钟点串，一个是 ``nudge:`` 开头）；
      2. **同一条消息只能把她叫来一次**——身份就是那条消息，跑过就不再跑。这不是冷却
         也不是计数器：真人手机是新消息才震，躺着的未读不会一直震。她没看手机的话
         那条一直未读，按"还有没有未读"判就是每分钟震一次；
      3. ``began_at`` 用真实时刻——她这一缝的『现在』就是被叫来的那一刻，没有格子可
         落。它**不负责**让这一缝排在上一缝后面：谁是"最后落地的那一缝"由 ``seq``
         答（:func:`latest_moment`），常规缝的格子比这个真实时刻早是正常的。

      顺带说清为什么不落格子也不丢幂等：一缝里所有派生 id（happening_id、whereabouts
      的自然键）都从 ``moment_id`` 来，**不从 ``now`` 来**；``moment_id`` 已经稳了，
      重跑照样是 no-op。

    **她被带到那一刻，回不回是她的输出。** 这里只负责把她带到，不看她说了什么、也没有
    任何"她该不该回"的判断——那是替她做决定。

    ``max_retries=1``：core 的 ``run`` 把整轮 ReAct 包在 ``@retry`` 里，一次模型
    瞬时失败会整轮重放、重放已经执行过的 durable 写。派生 id 让重放无害，但重放
    仍然是白花的一次钱，而且下一拍再来就行。
    """
    minutes = await life_moment_minutes()
    interval = timedelta(minutes=minutes)
    anchor = anchor_on_grid(now, minutes=minutes)
    nudged = nudged_by is not None
    began_at = now if nudged else anchor
    moment_id = (
        f"nudge:{nudged_by}" if nudged else anchor.isoformat(timespec="minutes")
    )

    async with hold(life_moment_lock_key(lane, persona_id)):
        if nudged:
            if await moment_ran(
                lane=lane, persona_id=persona_id, moment_id=moment_id
            ):
                return None
        else:
            last_regular = await latest_regular_moment(
                lane=lane, persona_id=persona_id
            )
            if (
                last_regular is not None
                and anchor - last_regular.began_at < interval
            ):
                return None

        # 游标跨两种缝共用一条轴：取"最近一缝"，不筛 nudged。
        last = await latest_moment(lane=lane, persona_id=persona_id)
        after_seq = last.next_seq if last is not None else 0
        snapshot = await read_snapshot(
            lane=lane, persona_id=persona_id, after_seq=after_seq, now=began_at
        )
        # 手机上只给信封（谁、多少条、多密、你上次在那儿开口是什么时候）。内容要她
        # 自己调 look_at_phone —— 白送进来的话，"她没看见"这个状态就再也不会发生。
        envelope = await phone_envelope(
            lane=lane, persona_id=persona_id, now=began_at
        )
        persona = await find_persona(persona_id)
        context = AgentContext(
            persona_id=persona_id,
            # 一个人的一整天在 langfuse 里读成一条流，逐缝翻起来才不用大海捞针。
            session_id=f"living-life:{lane}:{persona_id}",
            features={
                FEATURE_LANE: lane,
                FEATURE_NOW: began_at.isoformat(),
                FEATURE_PERSONA: persona_id,
                FEATURE_MOMENT: moment_id,
                FEATURE_SWITCHES: [],
                FEATURE_RECORDED: [],
                FEATURE_GLANCES: [],
            },
        )
        reply = await build_moment_runner().run(
            [
                Message(
                    role=Role.USER,
                    content=f"{snapshot.render()}\n\n{envelope}",
                )
            ],
            prompt_vars={
                "persona_name": getattr(persona, "display_name", "") or persona_id,
                "persona_core": _persona_core_var(
                    getattr(persona, "persona_core", None)
                ),
            },
            context=context,
            max_retries=1,
        )

        switches = context.features[FEATURE_SWITCHES]
        where = await current_whereabouts(lane=lane, persona_id=persona_id)
        # 落地号在收尾这一步才取：占用还没放开，所以取号到 commit 之间没有别人插进来，
        # 号的先后 == 提交的先后。崩在这之后的话这个号作废，在轴上留一个永远为空的洞
        # ——读侧问的是"号最大的那一行"，一个从没出现过的号不会让任何人被跳过。
        seq = await _next_moment_seq(lane=lane, persona_id=persona_id)
        moment = LifeMoment(
            lane=lane,
            persona_id=persona_id,
            moment_id=moment_id,
            seq=seq,
            began_at=began_at,
            after_seq=after_seq,
            next_seq=snapshot.perceived.next_cursor,
            perceived=len(snapshot.perceived.items),
            switched=bool(switches),
            pulled_by=switches[-1]["because"] if switches else "",
            recorded=len(set(context.features[FEATURE_RECORDED])),
            doing=where.doing if where is not None else "",
            open_ends=len(
                await list_open_loose_ends(lane=lane, persona_id=persona_id)
            ),
            said=reply.text().strip(),
            nudged=nudged,
        )
        # **这一缝落地和她看过的手机是同一个事务。** 工具返回不等于她看见了——只有这一缝
        # 跑完，工具结果才真的进过她的上下文。分开写的话，崩在两者之间就是"已读了但内容
        # 从没到她眼前"，那几条消息永久消失且一句报错都没有。绑在一起之后崩掉的代价只是
        # 她下一缝原样再看一遍：宁可重看，不可漏看。
        async with get_session() as s:
            await insert_idempotent(moment, session=s)
            await commit_glances(
                glances=context.features[FEATURE_GLANCES], session=s
            )
        return moment


@node
async def life_moment_tick(tick: LifeMomentTick) -> None:
    """把三个人各自推进一缝。

    **并发跑，一个人炸不拖累另两个。** 三条缝各有自己的占用（每人一条轴），所以
    并发没有竞争；串行的话一缝几十秒的模型调用会让第三个人永远排在拍与拍的边界上。
    异常不往上抛——源循环那一拍失败会连累另外两个人，而下一拍一分钟后就来了。
    """
    lane, now = living_lane(), now_cst()
    outcomes = await asyncio.gather(
        *(
            run_moment(lane=lane, persona_id=persona_id, now=now)
            for persona_id in LIVING_PERSONAS
        ),
        return_exceptions=True,
    )
    for persona_id, outcome in zip(LIVING_PERSONAS, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            logger.warning(
                "living moment lane=%s persona=%s 这一缝炸了：%r",
                lane,
                persona_id,
                outcome,
                exc_info=outcome,
            )
        elif outcome is not None:
            logger.info(
                "living moment lane=%s persona=%s 感知 %d 条、%s、说：%s",
                lane,
                persona_id,
                outcome.perceived,
                f"改去 {outcome.doing}（{outcome.pulled_by}）"
                if outcome.switched
                else "继续",
                outcome.said,
            )
