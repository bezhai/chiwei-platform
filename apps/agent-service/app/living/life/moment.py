"""moment —— 每十分钟她回到自己身上一次，回答同一个问题。

**问题只有一个：有没有什么把我从现在这件事里带走？** 默认答「继续」，一个词，零工
具、零写库，是最便宜的那种轮次。「默认继续」靠 prompt 让她自己选，**不是**代码里那
句"没有新事件就跳过这一轮"——那是替她做决定，而且它恰好是前六次范式失败的形状：
一旦由代码判断她该不该醒，她就不再是一直活着的，而是某个组件每轮算出来的产物。

**被带走时她换的是一件事，不是一个时长。** 所以这里的工具签名里**没有任何分钟数**
（``tests/living/test_moment.py`` 把这条钉死）。真人对"多久"没有内感受：他不会想
"我要花四十分钟洗澡"，他只是去洗澡了。问她要一个数字就是把生活切成日程表。

**她的上下文跨 moment 连续。** 一个 moment 结束时，这一轮喂进去的那条 USER 消息、她说的
每一句、每一次工具调用和工具返回原样存下来，下一个 moment 接在输入前面
（存取和裁剪在 :mod:`app.agent.continuity`；键是 ``lane:persona``，**不分天、一直连着**
（:data:`HISTORY_KEY`），在这条记录和手机已读提交之后单独写，写失败只记一行 ERROR、
这一轮照样算数（:func:`remember_this_round`））。

**所以醒来只送新发生的事**：几点了、离上一次隔了多久（:mod:`app.living.life.now`）、她在哪在做什么
（her_records 插件那一段）、有什么到点了（loose_ends 插件那一段）、传到她这里的消息里还没看过的
（household 插件那一段，:mod:`app.living.household`；别人做了什么，只有传到她这里的这一段）、手机上刚来了什么
（phone 插件那一段，:mod:`app.living.phone`）。她此刻的样子（在哪、在做什么、上一次写下的
那天、心里挂着什么、手机上还有什么没看）读一百遍字字一样，上一轮读过的还在上下文里，
所以它只在清理那一下作为新起点重铺一次（:func:`app.agent.continuity.trim_for_round`），
而且上下文里永远只留最新那一份。"心里挂着没了结的事"那份清单由 ``keep_in_mind``
重写（loose_ends 插件，:mod:`app.living.loose_ends`）。

**她自己说过的话，在她眼前只出现一次。** 上下文连着的时候，那就是她自己那次
``say`` / ``send_message`` 调用：回执不抄原话（发出去的只给撤回编号），状态也不再附
"你刚做过、说过"。那一段只在上下文接不住她最近说过的话时才给 —— 上下文是空的（第一次
跑、清过库）、硬顶把标记消息全裁掉了、上一轮没存下来
（:func:`app.agent.continuity.continues_unbroken`）。上下文不按天清零，所以正常跑着的
时候它不出现。2026-09-13 上下文连起来之后，同一批话在一轮输入里一度出现了几十次，她的
说话方式跟着一天天收窄成同一个样子。

**位置和手上的事是例外，每轮都给**（:meth:`app.living.her_records.book.OwnRecords.whereabouts_section`）：那两样她自己就能改
（:meth:`InPerson.switch_to`、:meth:`InPerson.move_to`），铺在界桩上的那份到下一个清理点之前一直是旧的，
而她在哪是她这一轮里最不能过期的一样（world 按她报的位置判断谁看得见她）。

**挂线头是独立的一件事，不绑在 ``switch_to`` 上。** 「是否换事」不等于「是否记住」：
绫奈跟她说"周末陪我去祭典"，她手上的书没放下（这个 moment 答「继续」），但她记住了——这是
真人每天都在做的事。把清单绑在换事情上，这句话在她看过之后就永久消失了：传到她这里的消息
只摆一次，上下文里那一段 4 小时就裁掉，状态里「你刚做过、说过」那一段只有她**自己**说做
的，别人说的话不在里面，谁也救不回来。而"跨 moment 因果延续"恰好是整个实验最想验证的东西。

**每个 moment 串行。** 一个人不能同时想两件事——这是物理事实，不是给她加冷却。她的每一轮都在
她自己的一条轮次队列上跑，一次一轮，后到的排队等前一次做完（两条路：钟点上该来的，和
:mod:`app.living.life.nudge` 那条被叫醒提前来的；队列由宿主建，登记在 :mod:`app.living.life`）。

**moment 的身份是格子，不是钟表上那一瞬**（:func:`regular_moment_due`）。
副作用先落库、``LifeMoment`` 后落库，中间崩掉的话下一拍会重跑；身份落在格上，重试拿到
的是同一个 moment、所有派生 id 原样对上，重放退化成无害的 no-op。残余缺口（重放产出了**不
一样**的动作时两边都会留下）写在那里。

**提前来的那个 moment 不落在格子上**，它的身份是**把她叫来的那条消息**（``nudged_by``）。
理由见 :class:`DueMoment`：撞不上常规 moment 的 id，而且同一条消息只能把她叫来一次。

**身份是格子，她这个 moment 的『现在』却是拿到占用那一刻的钟**（:class:`DueMoment`）。排队是
正常的（队列让后到的等，不丢），前一轮挂住时后面那一拍要过一阵才轮到她；她这一轮看到的、
做的、发出去的都在轮到她之后，钟点也就是那一刻。

**"她这个 moment 的钟点"和"这个 moment 第几个落地"是两个问题，各有各的列**（见
:class:`LifeMoment` 的 ``seq``）。

工具（``keep_in_mind`` 夹在这几只中间给她，但它是 loose_ends 插件的，见 :mod:`app.living.loose_ends`）：

  * :meth:`InPerson.switch_to`    我现在改去做 X、在哪、什么把我带走的
  * :meth:`InPerson.move_to`      人挪了地方，手上的事没变
  * :meth:`InPerson.say` / :meth:`InPerson.act`  当面说一句话 / 做一个别人看得见的动作。这一轮结束时，她在
    世界里做的事一条发给 world，只对姐妹说的话直接给那位姐妹（household 插件，
    :mod:`app.living.household.outgoing`）
  * :meth:`InPerson.look_around`  问 world 她这里现在什么样、有谁在；没人回答就如实告诉她
  * :meth:`InPerson.stop_for_now` 这一轮我到此为止（零参数，调完这一轮就结束）
  * ``look_at_phone``    拿起手机打开某条会话，看说了什么（phone 插件，:mod:`app.living.phone`）
  * ``look_through_your_phone`` 翻一翻手机上都有哪些会话（同上，三层里的第二层：
    通知只给刚来的那几条动静，这只手给整张名单）
  * ``look_up_contact``  按名字找回一条会话（同上）
  * ``send_message``     给手机上某条会话发一条（:mod:`app.living.speaking.mouth`）
  * ``search_online`` / ``browse_online``  带着问题上网查 / 没事逛一圈刷一批
    （:mod:`app.living.web`，跟 ``look_at_phone`` 是两件事：那只手看别人发给她的
    消息，这两只手看网上的东西）
  * ``look_for_something_to_read`` 有人发过什么能读的、她读到哪了
    （:mod:`app.living.reading`）
  * ``read_a_bit``       拿起其中一个往下读一程（:mod:`app.living.reading`）
  * ``draw_a_picture`` / ``find_a_picture_online``  自己画一张 / 上网找现成的一张
    （:mod:`app.living.pictures`）
  * ``look_through_your_pictures`` / ``look_at_a_picture``  翻一翻她手上有哪些图 /
    拿出其中一张看（:mod:`app.living.pictures`，跟读东西那两只手同一个形状：先列出
    来、再点一张。**"看"这只手拆出来是必须的**——句柄只出现在画图那一刻的话，
    她能不能找回自己画过的东西就取决于那次返回还在不在上下文里）
  * ``read_a_guide`` / ``run_a_script``  读一份写好的说明 / 照说明把它教她跑的那条
    命令真的跑一遍（guides 插件，:mod:`app.living.guides`。她自己长什么样就写在其中一份里；四份
    说明里三份通篇在教她跑脚本，只接读取的话读完手上没有能跑它的东西）

prompt 在 Langfuse，按顶层小节分成五段（:data:`LIFE_MOMENT_AGENT` 列出顺序，id 是
``living_life_moment/<段名>``），变量三个——``persona_name``、``persona_core`` 在「她是谁」那一段
（persona 插件登记，:mod:`app.living.persona`），``guides_you_can_read`` 在「手边能读的说明」那一段
（guides 插件登记，:mod:`app.living.guides`）。**每个 moment 都变的东西一律
走 USER 消息**：prompt 变量没有编译期校验，改名会静默渲染成字面量，能少一个就少一个。
第三个是例外，而且只能是变量：她手边有哪些说明可读会变（注册表启动时填、每 30 秒热
加载），工具 schema 在 import 时就定死了，装不下一份会变的清单。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Annotated

from inner_shared.dynamic_config import dynamic_config
from pydantic import Field, field_validator

from app.agent.contracts import AgentSpec, Segment, TrimPolicy, VarsFn
from app.agent.round import (
    AssembledProfile,
    Round,
    current_round,
    run_round,
    store_history,
)
from app.agent.tools._common import get_or_create_counter, tool_error
from app.agent.tools.outcome import BAD_ARGUMENTS
from app.data.ordered import insert_in_commit_order
from app.data.persist import fetch_one, from_row
from app.data.schema import CommitOrder, Data, Key, table_name
from app.infra.cst_time import CST
from app.living.common import SEND_SECONDS, esc, legacy_null_is, require_aware
from app.living.services import (
    KIND_ACT,
    KIND_SPEECH,
    MEDIUM_IN_PERSON,
    WORLD,
    HerRecords,
    LooseEnds,
    Place,
    WhoLivesHere,
)
from app.messaging.sending import ask

logger = logging.getLogger(__name__)

# Langfuse prompt id（新 id，只发泳道 label，不碰 production）。
LIFE_MOMENT_PROMPT_ID = "living_life_moment"

# life-model：这个 moment 要的是对自己处境的判断（有没有什么把我带走），不是对话能力。
# 用 life-model 而不是 offline-model，是因为**量级不一样**：life 一天 432 个 moment × 三个
# 人。life-model 这个别名本来就是给 life 这种高频线选的，跟低频的离线调用（日记、每周
# 回看，走 offline-model）档位不该互相牵动。
# recursion_limit 12 = **她一口气能做几件事**，不是"一次思考多深"。上下文连续之后这两个
# 问题分开了：一件完整的事要三四次模型调用（搜一下拿到结果 → 读进去 → 说出来 / 做出
# 来），12 给的是三件的余量。8 只够两件，而 2026-09-11 实测 5 小时 66 个 moment 里，那
# 12 只需要先拿中间结果才用得上的工具一次都没被调用过。
#
# **这个数不是节奏，是兜底。** 什么时候收口由她自己调 :meth:`InPerson.stop_for_now` 决定；撞上
# 这个数说明她一直没停，那一下不静默截断，而是再调一次模型、这次不给任何工具，让她把
# 话说完（见 :func:`app.agent.core._run_loop`）。
#
# **往上调之前先看它乘在哪里**：每轮新增的 token 跟着它走（实测每轮 5k–10k），而上下文
# 的硬顶是 200k（:data:`MOMENT_TRIM_POLICY`）。翻倍这个数等于把撞硬顶的时间减半。
#
# 一个人的一整天在 langfuse 里读成一条流（session），逐个 moment 翻起来才不用大海捞针。
# 模型的临时失败不整轮重来（``max_retries`` 1）：重放会把已经执行过的 durable 写再执行一遍，
# 派生 id 让它无害，但仍是白花的一次钱，下一拍再来就行。
def _segment(
    name: str, *, variables: tuple[str, ...] = (), values: VarsFn | None = None
) -> Segment:
    """她这一轮 prompt 的一段：段名和 Langfuse id 都是 ``living_life_moment/<name>``。"""
    prompt_id = f"{LIFE_MOMENT_PROMPT_ID}/{name}"
    return Segment(prompt_id, prompt_id, variables=variables, values=values)


# 她这一轮 prompt 里由这里登记的段。每段声明的变量就是这一段给的那几个（模板多一个、少一个都是
# 配置错误，见 :class:`app.agent.prompts.PromptConfigError`）。``<rules>`` 一条规则同时说到好几只手，
# 整段留在一起。第一段「她是谁」由 persona 插件登记（:mod:`app.living.persona`），最后一段「手边能读的
# 说明」由 guides 插件登记（:mod:`app.living.guides`）。
LIFE_MOMENT_SEGMENTS = (
    _segment("this-moment"),
    _segment("rules"),
    _segment("voice"),
)

# 她这一轮的 prompt：按顶层小节分的五段，按这个顺序用空行拼成一条 SYSTEM，和分段之前的整份逐字节
# 相同。
LIFE_MOMENT_AGENT = AgentSpec(
    segments=tuple(
        f"{LIFE_MOMENT_PROMPT_ID}/{name}"
        for name in ("identity", "this-moment", "rules", "voice", "guides")
    ),
    model="life-model",
    trace_name="living-life-moment",
    recursion_limit=12,
    session="living-life:{lane}:{participant}",
    bad_arguments=BAD_ARGUMENTS,
)

@dataclass
class Switched:
    """这一轮她换过的事（每次 :meth:`InPerson.switch_to` 追加一条 ``{"doing", "because"}``）。数它而不是
    数"有没有调过工具"：换事情和说话是两回事，而"换的理由"要能逐次查出来。这一轮的状态。"""

    changes: list[dict[str, str]] = field(default_factory=list)


# Dynamic Config key：两个 moment 之间至少隔多少分钟。改它不用重新部署。
LIVING_LIFE_MOMENT_MINUTES_KEY = "living_life_moment_minutes"
DEFAULT_LIFE_MOMENT_MINUTES = 10

# 派生 id 的命名空间，随手换会让历史记录全部对不上。
_ID_NS = uuid.UUID("9c1e4f70-3b28-4a56-8d0f-1e7a2c5b6934")


class LifeMoment(Data):
    """她跑过的一个 moment：换没换事情、为什么换、最后说了什么。

    自然键 ``(lane, persona_id, moment_id)``，纯 append 无版本链——一个 moment 过完就是过完
    了。``moment_id`` 是这个 moment 的**身份**：常规 moment 取时间锚（精确到分，同一格的重放落回
    同一行），提前来的 moment 取把她叫来的那条消息。

    **``began_at`` 和 ``seq`` 回答的是两个问题，别互相顶替。**

      * ``began_at`` = **她这个 moment 的『现在』**：喂给快照、手机信封和这一轮的 ``now``
        的那个时刻，也是她这一轮记下的事、发出去的消息上的时刻。两种 moment 都取拿到占用
        那一刻的钟（:class:`DueMoment`）；常规 moment 的身份是它落在的那一格，不是这个时刻。
      * ``seq`` = **这个 moment 在这条轴上第几个落地**，(lane, persona) 各一条轴，在落地那次
        提交里取号（:func:`app.data.ordered.insert_in_commit_order`），所以它的先后就是提交的
        先后。

    每一轮都在拿到占用时读钟，所以新写下的几轮两者先后一致；常规 moment 的 ``began_at`` 还记着
    格子的那些旧行没有这个保证（排在提前来的 moment 后面落地的常规 moment，钟点反而更早）。
    所以：

      * "上一个落地的是谁"问 ``seq``（:func:`latest_moment`）：她的上下文是一版接一版
        写下去的，``context_ver`` 要跟最后落地的那一轮比；
      * 常规节奏问的是"最近跑过的**哪一格**"，那是 ``began_at``
        （:func:`latest_regular_moment`）：它落在哪一格，那一格就跑过了。

    ``switched`` / ``pulled_by`` / ``said`` 三列是验收口径，不是日志："逐个 moment 看得出
    哪些是继续、哪些换了事情、换的理由是什么"只能从这张表查。光看 langfuse trace
    算不准（会丢 trace），光看 ``Whereabouts`` 看不见"跑了但没换"的那些 moment。

    ``nudged`` 分开的是**两种 moment**：钟点上该来的那种，和被人叫来提前的那种。这一列
    有真实的机制后果，不是标签——常规间隔只跟**上一个常规 moment**比
    （:func:`latest_regular_moment`）。不分开的话，每来一条私聊就把她的固定节奏往后
    推一次：一天被搭话十次，她的十分钟就成了不定期。
    而"上一个落地的是谁"仍然跨两种 moment 共用一条轴（:func:`latest_moment` 不筛这一列）：
    她的上下文只有一条，两种 moment 都接在它后面写。

    ``context_ver`` 是**这一轮的上下文该写成第几版**（读到的那一版加一），它让下一个
    moment 判得出"上一轮到底写进去没有"：这条记录先提交、上下文后写，所以下一轮读到的
    版本比这个数小就是那次写没落地。写失败留得下一行 ERROR，而进程崩在两次提交之间时
    连 ERROR 都没有 —— 两种在下一轮眼里是同一个形状，判据因此只能落在这一列上
    （:func:`lost_last_round`）。自愈：补上那一轮写完之后两个数就追平了。
    """

    lane: Annotated[str, Key]
    persona_id: Annotated[str, Key]
    moment_id: Annotated[str, Key]
    # 这个 moment 第几个落地（本 lane + 本人一条轴）
    seq: Annotated[int, CommitOrder(within=("lane", "persona_id"))] = 0
    began_at: datetime   # 她这个 moment 的『现在』：拿到占用那一刻的钟
    switched: bool       # 换事情了吗（False = 「继续」）
    pulled_by: str       # 什么把她带走的（她自己那句）；没换 = ""
    recorded: int        # 这个 moment 她说 / 做了几件事（当面的、手机上的都算）
    doing: str           # moment 末她手上是什么事
    open_ends: int       # moment 末她心里还挂着几件
    said: str            # 她这个 moment 最后那句话
    context_ver: int     # 这个 moment 的上下文该写成第几版
    nudged: bool = False  # 被叫来提前的那个 moment 吗（False = 钟点上该来的）

    class Meta:
        # 两条读侧形状，因为上面那两个问题各问各的：
        #   * (lane, persona_id, seq)      最后落地的那个 moment + 取下一个号的 MAX(seq)
        #   * (lane, persona_id, began_at) 最近跑过的那一格
        indexes = (
            ("lane", "persona_id", "seq"),
            ("lane", "persona_id", "began_at"),
        )

    # ``nudged`` / ``seq`` 都是后加的列，已经有数据的泳道上旧行是 NULL —— 不接住就是
    # 读一行炸一行，整条 life 循环推不动。理由和另一半防线（SQL 侧的 COALESCE）见
    # :func:`app.living.common.legacy_null_is`。
    _legacy_nudged = field_validator("nudged", mode="before")(
        classmethod(legacy_null_is(False))
    )
    # 加列之前的行统一当成 0 号：新写的 moment 从 1 起，所以任何一条新 moment 都排在它们后面，
    # 而旧行之间的先后退回加列之前唯一有过的那个口径（``began_at``，见
    # :func:`latest_moment` 的第二排序键）。新写的 moment 不带号，号由落地那次提交给。
    _legacy_seq = field_validator("seq", mode="before")(
        classmethod(legacy_null_is(0))
    )
    # 加列之前的行没有这个数，读出来当 0 —— 而 0 是"这一行说不出上下文该是第几版"，
    # :func:`lost_last_round` 据此不判。判了就是拿一个没人写过的数去说"上下文丢了"，
    # 于是她每一轮都在重铺状态。**pydantic 那侧不给默认值**：新写的 moment 漏传它是
    # bug，该当场炸，不该悄悄写成 0 把这条检查关掉。
    _legacy_context_ver = field_validator("context_ver", mode="before")(
        classmethod(legacy_null_is(0))
    )

    @field_validator("began_at")
    @classmethod
    def _aware_began_at(cls, v: datetime) -> datetime:
        return require_aware("began_at", v)


_MOMENT_TABLE = table_name(LifeMoment)


# 她那条连续上下文的存储键（清单 ``resident`` 的 ``history``），按 ``lane``、``participant`` 填。
#
# **一个人一条，不分天。** lane 在键里，两条泳道天然是两行。键原来带着生活日，于是凌晨 04:00 那一轮
# 读到的是一条空上下文；一个人不会在早上四点忘掉昨晚正在想的事，所以日期去掉了，增长由裁剪管着
# （:data:`MOMENT_TRIM_POLICY` 的硬顶）。代价是清零原来是唯一一次保证前缀重建的时刻，去掉之后裁剪
# 必须长期正确，守它的是 ``tests/living/test_context_trim.py`` 跨天那一节。
#
# 跟 langfuse 的 session 不是一回事：trace 的 session 是 :data:`LIFE_MOMENT_AGENT` 的
# （``living-life:{lane}:{participant}``）。
HISTORY_KEY = "{lane}:{participant}"


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _derive(*parts: str) -> str:
    return uuid.uuid5(_ID_NS, "\x1f".join(parts)).hex


# ---------------------------------------------------------------------------
# place 这个参数交给她的字
#
# 说这几句的是她那两只落位置的手（:meth:`InPerson.switch_to` / :meth:`InPerson.move_to`）。她写下的位置是她
# 自己"手上"那一句，也是 world 判断谁会察觉到她的依据：一轮的汇总末尾写着她在哪
# （:mod:`app.living.household.outgoing`），环顾四周时她先说自己在哪（:meth:`InPerson.look_around`）。三个人
# 写的是同一个世界里的地方，同一个地方说法一致，world 才认得出是同一处。
#
# **举例就是词表。** 她不读文档，唯一见过的地名样本就是这段描述，写在这儿的地名会被
# 逐字抄走。两次线上事故都是这么来的（那时谁跟谁在一处由代码按路径字符串比对）：
#
#   * 举例写 ``家/楼上/我房间`` —— "我房间"是说话人相对的说法，而 place 是三个人共用
#     的同一套字符串。prod 实测 45 次 ``家/楼上/我房间`` 加 9 次 ``家/我房间`` 全部出
#     自绫奈和千凪两个人，2026-09-13 21:00 两人同一分钟落在这个地名上，各自在自己屋
#     里，判定却是同处一室，私下说的话被对方原话听见。
#   * 举例写 ``学校/二年三班教室`` —— 被一字不差抄走，而设定集里那间教室叫
#     ``学校/初二三班教室``。同一个地方从此分成两半。
#
# 换上一批新的写死字符串（家/浴室、学校/操场这些）只是把同一次事故往后推：那些名字是从
# 世界的初始设定里抄来的，而世界随时可以改名、删掉、重写它们，这段描述不会跟着变。
#
# 所以这里**一个样本都不给**，只说规则。占位符（``A/B``、``某地/某处``、
# ``<建筑>/<房间>``）同样不给：那仍然是一个可以照着填的模板，而且比直说更难懂。
# 也不写"去查一下设定集有哪些地名"——她不读文档是既定边界。守这条的是
# ``tests/living/conftest.py`` 里那两道检查：说明里没有具体地名，也没有贴着字的路径。
# ---------------------------------------------------------------------------

_PLACE_SHAPE = (
    "写成一条层级路径：最外面一层是这个世界里一处独立的地方，"
    "往里一层层是它内部的分隔，层与层之间用 / 隔开；"
    "就在那一处本身、底下没有更细的分隔时，只写一层也行。"
)

# 第一次事故的病根单列一句：形状写对了，"我房间"这种写法照样会把两个人写到同一个说法上。
_PLACE_IS_EVERYONES = (
    "这套地名三个人共用，同一个地方谁写都得写成同一条路径，"
    "所以别用只有站在你自己的位置上才指得明白的说法。"
)

PLACE_DESCRIPTION = f"你人在哪。{_PLACE_SHAPE}{_PLACE_IS_EVERYONES}"

# 空 place 那一句。两只手共用同一句：后果一模一样（world 判断谁会察觉到她，靠的就是她
# 报的位置），分成两份写只会让其中一份悄悄变旧。
PLACE_CANNOT_BE_EMPTY = (
    "place 不能是空的：位置是别人能不能感知到你的全部依据，"
    "空位置会让你从此谁也听不见、也没人听得见你。" + _PLACE_SHAPE
)


# ---------------------------------------------------------------------------
# 环顾四周：问 world
#
# 这里现在什么样、有谁在，由 world 回答，life 不按地名比对谁跟谁在一处：地名是三个人各自写的
# 字符串，同一个家会被写成两条不一样的路径，比对的结果就是她跟同在厨房的妹妹"不在一个地方"。
# world 读的是自然语言，依据是它的记录、各人自己报的位置和现实里的天气。
# ---------------------------------------------------------------------------

# 她环顾四周时最多等 world 回答多久。world 那边每个问题起一个应答 agent（一次模型调用，中间可能
# 查几次记录），coe 上实测 12–16 秒；45 秒是它的三倍左右，慢一些的那次也等得到。再久就不等了，
# 如实告诉她没人回答，她过一会儿可以再看一眼。这个时刻随问题带过去（通信机制的截止时刻），过了
# 它 world 那边不再答。
#
# 这个数只管一次环顾四周，管不住她这一轮。一次最多 55 秒（加上下面问出去那一步的上限），可一轮里
# 环顾几次不由它定：recursion_limit（``LIFE_MOMENT_AGENT``）数的是模型调用，一次模型调用可以连着要几只手，
# 这些手一只接一只跑，模型调用本身也要花时间，所以一轮能花多久没有一个从这里乘得出来的上限。
# 兜住一轮的是她轮次队列的占用上限（:data:`app.living.common.HOLD_LIMIT`，900 秒）：等回答时
# 撞上它，这一轮照常被掐断、队列放开、下一拍重来。环顾四周不绕过它，掐断传进来的取消原样往上走，
# 不当成"没人回答"。
# 不做成配置：这是"不让她干等"的上限，不是她生活里的一个参数。
LOOK_AROUND_ANSWER_SECONDS = 45.0

# 没等到回答时交给她的那一句。只说没人回答，不说为什么：超时、world 没在跑、出错，对她来说都
# 是同一件事——这一眼没看到。原因记在日志和通信机制的记录里。
NOBODY_ANSWERED = "没人回答：你不知道这里现在什么样。"


def _looking_around_from(me: Place) -> str:
    """她问 world 的那一句：先说她自己此刻在哪、在做什么，再问这里现在什么样、有谁在。

    **她在哪必须写在问题里。** 她这一轮做的事要等这一轮结束才汇总发给 world
    （:mod:`app.living.household.outgoing`），提问又不排在普通消息后面，所以同一轮里她刚换了地方就环顾
    四周，world 还不知道她挪了。她自己知道，就由她说；``me`` 是她此刻最新的那条位置，这一轮
    里 ``switch_to`` / ``move_to`` 刚写下的也在内。
    """
    return f"我现在在 {me.place}，正在 {me.doing}。我看一眼周围：这里现在什么样？有谁在？"


class InPerson:
    """life 插件对象：她亲身在世界里的那几只手 —— 换一件事、挪个地方、当面说一句、做一个别人看得见的动作、
    看一眼周围、这一轮到此为止。她在哪、做过什么记在 ``her_records`` 服务上，说给谁、问 world 时署的名经
    ``residents`` 服务认。对象本身不带状态：这一轮换过的事记在这一轮的状态上（:class:`Switched`）。"""

    def __init__(self, *, residents: WhoLivesHere, her_records: HerRecords) -> None:
        self.residents = residents
        self.her_records = her_records

    @tool_error("换事情失败")
    async def switch_to(
        self,
        doing: Annotated[
            str, Field(description="你现在改去做的这件事，一句话，例如「去洗澡」")
        ],
        # 这段描述里一个地名都不给，为什么见 :data:`PLACE_DESCRIPTION` 上方那段。
        place: Annotated[str, Field(description=PLACE_DESCRIPTION)],
        because: Annotated[
            str, Field(description="什么把你从刚才那件事里带走的，一句话")
        ],
    ) -> str:
        """我现在改去做别的了。

        你被带走的时候调它。说清楚你改去做什么、人在哪、什么把你带走的。

        别报时长——你不知道自己要做多久，也不用知道。

        人挪了地方但手上还是同一件事，那是 move_to。

        心里挂着什么是另一回事，走 keep_in_mind，跟换不换事没有关系。

        Args:
            doing: 你现在改去做的这件事。
            place: 你人在哪（层级路径）。
            because: 什么把你带走的。

        Returns:
            一句确认文本。
        """
        this_round = current_round()
        lane, now, persona_id = this_round.lane, this_round.now, this_round.participant
        moment_id = this_round.round_id
        what, where = doing.strip(), place.strip()
        if not what:
            raise ValueError("doing 不能是空的：说一句你改去做什么。")
        if not where:
            raise ValueError(PLACE_CANNOT_BE_EMPTY)

        # whereabouts 的自然键带上内容派生的后缀：同一个 moment 里她改两次主意要落两行
        # （最新的那条才是"当前"），而重放同样的内容仍然只落一行。
        await self.her_records.note_whereabouts(
            lane=lane,
            persona_id=persona_id,
            moment_id=f"{moment_id}:{_derive(what, where)[:8]}",
            place=where,
            doing=what,
            noted_at=now,
        )
        this_round.state(Switched).changes.append({"doing": what, "because": because.strip()})
        return f"你在 {where}，{what}。"

    @tool_error("挪个地方失败")
    async def move_to(
        self,
        # 同上，一个地名都不给：见 :data:`PLACE_DESCRIPTION` 上方那段。
        place: Annotated[str, Field(description=PLACE_DESCRIPTION)],
    ) -> str:
        """我人换地方了，手上的事没变。

        走到别处、但还在做同一件事的时候调它。

        你在哪，决定了谁看得见你、你看得见谁。人挪了而这里没挪，你在所有人眼里就还
        待在原地——你说你已经到了别处，她们看到的你还在你上一次落下的那个地方。

        被别的事带走了、手上这件事本身换了，那是 switch_to；那只手也会顺带记下位置。

        Args:
            place: 你现在人在哪（层级路径）。

        Returns:
            一句确认文本。
        """
        this_round = current_round()
        lane, now, persona_id = this_round.lane, this_round.now, this_round.participant
        moment_id = this_round.round_id
        where = place.strip()
        if not where:
            raise ValueError(PLACE_CANNOT_BE_EMPTY)

        # 手上那件事原样带走 —— 这只手改的只有位置。没有"当前"就没有可带走的事：
        # 她还没落过位置，这一步无从下脚（第一次落位走 switch_to）。
        current = await self.her_records.current_whereabouts(lane=lane, persona_id=persona_id)
        if current is None:
            raise ValueError(
                "你还没定下过自己在哪，也就没有一件'照旧做着'的事可以带到新地方去。"
            )

        # 自然键跟 switch_to 同款：同一个 moment 里挪两次要落两行（最新那条才是"当前"），
        # 而重放同样的内容仍然只落一行。
        await self.her_records.note_whereabouts(
            lane=lane,
            persona_id=persona_id,
            moment_id=f"{moment_id}:{_derive(current.doing, where)[:8]}",
            place=where,
            doing=current.doing,
            noted_at=now,
        )
        # **不进 Switched**：走一步不是"什么把你从这件事里带走了"。混进去会让
        # 逐个 moment 复盘里的换事率把单纯的走动也算成换事情。
        return f"你在 {where}，还在{current.doing}。"

    @tool_error("说话失败")
    async def say(
        self,
        what: Annotated[str, Field(description="你说出口的那句话，原话")],
        to: Annotated[
            list[str],
            # 这里**不举例**：举出来的名字就是词表，她会逐字抄走（同 switch_to / move_to
            # 的 place 举例事故）。而且一旦举的是"这个家里的三个人"，就等于把"世界上有
            # 谁"重新写回了工具描述里 —— 那正是这只手刚拆掉的那道门。姐妹的名字在人设表里
            # （:mod:`app.living.residents`），代码里一个都不写。
            Field(
                description=(
                    "说给谁，写这个人在世界里的名字，可以同时对好几个人；"
                    "不冲着谁说就传空数组"
                )
            ),
        ],
    ) -> str:
        """当面说一句话。

        写你**真的说出口的那句话**。「我和她说了几句」不是说话，那是记账——记账在
        这个家里等于什么都没发生过，别人读到的就是一句空话。

        说给你姐妹的，她一定听到原话，跟她在哪没关系。说给别人的、不冲着谁说的，谁听得见
        要看当时的情形。

        Args:
            what: 你说出口的那句话，原话。
            to: 说给谁，写名字，可以多个。

        Returns:
            一句确认文本。
        """
        return await _record(
            kind=KIND_SPEECH,
            content=what,
            audience=to,
            residents=self.residents,
            her_records=self.her_records,
        )

    @tool_error("做这个动作失败")
    async def act(
        self,
        what: Annotated[
            str, Field(description="你做的这个动作，一句自然语言，例如「把胶片摊了一茶几」")
        ],
    ) -> str:
        """做一个别人看得见的动作。

        谁会看见，要看当时的情形。

        只用来记**别人感知得到**的动作。你自己安静做的事不用调它——那是你手上的事，
        在 switch_to 里。

        **你写下的这句话，原样就是姐姐下一次感知到的客观动静，中间没有谁替你核一遍。**
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
        return await _record(
            kind=KIND_ACT,
            content=what,
            audience=[],
            residents=self.residents,
            her_records=self.her_records,
        )

    @tool_error("停下失败")
    async def stop_for_now(self) -> str:
        """我这一轮就到这儿了。

        该做的做完了、或者看了一圈没什么要做的，调它，这一轮到此为止。调完不会再有人问
        你一句，你也不用再说什么。

        **别说多久。** 下次什么时候再回到自己身上不是你定的 —— 时间自己会走，别人也会
        叫你。你只决定现在停。

        心里还挂着没了结的事，先 keep_in_mind 记下来再调它：这一轮之后你眼前的东西会换
        一批，没记下来的下次就想不起来了。

        Returns:
            一句确认文本。
        """
        return "就到这儿。"

    @tool_error("看一眼周围失败")
    async def look_around(self) -> str:
        """看一眼你这里现在什么样、有谁在。

        你在哪、在做什么，以你最后一次定下的为准，这一轮里刚换过、刚挪过的也算。

        回来的就是此刻这里的样子。回来的是"没人回答"时，你就是不知道这里现在什么样，
        别自己补上。

        Returns:
            此刻这里的样子；没人回答时如实说没人回答。
        """
        this_round = current_round()
        lane, persona_id = this_round.lane, this_round.participant
        try:
            me = await self.her_records.current_whereabouts(lane=lane, persona_id=persona_id)
            if me is None:
                # 没有"这里"可问：她自己没定下在哪，world 也不知道。
                return "你还没定下自己在哪，所以什么都够不着。先用 switch_to 落个位置。"
            asker = self.residents.residents().by_persona[persona_id]
            # 通信机制只给等回答那一段封了顶。问出去那一步（查 world 开没开收件箱、记几行记录、
            # 发给 broker 等确认）跟发一条消息是同一套动作，broker 不应答时会一直挂着，所以整个
            # 提问再按发一条消息的上限（:data:`app.living.common.SEND_SECONDS`）多给一段。
            async with asyncio.timeout(LOOK_AROUND_ANSWER_SECONDS + SEND_SECONDS):
                answer = await ask(
                    sender=asker,
                    recipient=WORLD,
                    body=_looking_around_from(me),
                    timeout_seconds=LOOK_AROUND_ANSWER_SECONDS,
                )
        except Exception:
            # 提问不重试（通信机制的约定）。什么原因没问成，都是这一眼没看到：问之前查她在哪、查她
            # 叫什么出的错也算。只接 Exception：她这一轮被取消（队列的占用上限到点掐断、进程收尾）不是
            # 没问成，取消原样往上走，这一轮该停就停。
            logger.warning(
                "living look_around lane=%s persona=%s 问 world 没问成",
                lane,
                persona_id,
                exc_info=True,
            )
            return NOBODY_ANSWERED
        if answer.text is None:
            logger.info(
                "living look_around lane=%s persona=%s world 没有回答：%s",
                lane,
                persona_id,
                answer.reason,
            )
            return NOBODY_ANSWERED
        # 不加一个字、不少一个字，过 :func:`app.living.common.esc`。回答是 world 写下的字，跟 world
        # 发来的消息是同一类，按同一条规矩交给她（:func:`app.living.household.received.render_received`）：别的
        # 参与者写下的字不归她管（esc 上"经过模型的不转义"说的是她自己的话和她自己留下的东西），它落进
        # 她这一轮的上下文、跟消息行摆在同一段文本里，不转义就能自己写出一行 ``rel="owner"``。
        return esc(answer.text)


async def _record(
    *,
    kind: str,
    content: str,
    audience: list[str],
    residents: WhoLivesHere,
    her_records: HerRecords,
) -> str:
    """``say`` / ``act`` 共用的落库：位置必须是真的，说给谁是她的事。

    位置这条 fail-loud：当面做的事这一轮结束时汇总给 world，汇总末尾那句她在哪、在做什么，
    是 world 判断谁会察觉的依据（:mod:`app.living.household.outgoing` 靠这一条断言汇总一定说得出她在
    哪）。没定下位置就做事，world 不知道这件事发生在哪，而她那边一片安静。位置本身不记进
    这件事里：它在她自己的位置记录上（:mod:`app.living.her_records.whereabouts`）。

    **收件人不校验，故意的。** 这里曾经拿一份写死的三姐妹名单
    当白名单挡下"不是这三个人"的名字。那是把"这个世界里住着谁"这条断言存在了代码
    里：world 写得出"许阿姨下午来敲门"，而她对许阿姨物理上开不了口——门一关，世界
    往前走一步，代码就离世界远一步。世界里有谁由 world 说了算。

    换成"去 world 那边查一遍有没有这个人"同样不对：life 不读 world 的存储，而且 world
    可能还没来得及记下这个人。名字原样记进这件事里；这一轮结束时，姐妹的名字直接把原话
    送到她那里，其余的名字连同这句话交给 world（:mod:`app.living.household.outgoing`）。说给一个
    没人接的名字，那句话就落在世界里没人接——这本身是真实的，不是错误。

    **只有一处不原样：姐妹的内部 id 记成她的名字**（:meth:`~app.living.services.Residents.names_for`）。
    她眼前已经不出现 id，可存下的对话历史里还留着旧的渲染文本，她照着写。id 原样记下的话
    就不算姐妹的地址，这句话绕道 world 转告，对方回话再绕一遍，每句都多花一轮 world。写错的
    id 不猜，原样记：她想叫的是谁，life 说不准。

    **这件事的 id 仍然按她写下的 ``to`` 原样算，不按换好的名字算。** 换名字之前的版本记下的
    那些，id 就是按原样算的：那一轮没跑完就赶上发版，新版本重跑同一轮、她说了同一句，id 对得上
    才认得出是同一件事。按换好的名字算，同一句话就成了另一件事，多记一条，姐妹多收一条。
    """
    this_round = current_round()
    lane, now, persona_id = this_round.lane, this_round.now, this_round.participant
    moment_id = this_round.round_id
    said = content.strip()
    if not said:
        raise ValueError("内容不能是空的：说一句真的话 / 写清楚你做了什么。")
    if await her_records.current_whereabouts(lane=lane, persona_id=persona_id) is None:
        raise ValueError(
            "你还没定下自己在哪 —— 先用 switch_to 说清楚你人在哪、在做什么，"
            "再说话或者动作。"
        )
    happening_id = "moment:" + _derive(
        persona_id, moment_id, kind, said, ",".join(audience)
    )
    await her_records.record_happening(
        lane=lane,
        happening_id=happening_id,
        actor=persona_id,
        kind=kind,
        content=said,
        occurred_at=now,
        audience=residents.residents().names_for(audience),
        medium=MEDIUM_IN_PERSON,
    )
    # 不把原话再抄一遍：它就在这次调用的参数里，而这条回执跟她的话一样在上下文里留
    # 4 小时（登记它时声明成留着的那一档），抄一遍就是同一句话在她每一轮的输入里多出现一次。
    # 当面说的话没有撤回编号，所以这里也没有编号可给。
    return "记下了。"


# 她的上下文裁多久。**写死在这儿，不走 Dynamic Config**：这五个数不随运行时变化，
# 它们是"她是什么东西"的一部分，不是可调的旋钮。裁剪那一层只给形状，填什么归这里
# （:class:`app.agent.contracts.TrimPolicy`）。
#
#   * ``material_minutes`` 60   她读到的东西，过期了再看一次就有
#   * ``own_minutes``     240   她自己说的话和动作回执 —— 那是她那段经历读得懂的骨架
#   * ``cleanup_minutes``  60   固定时刻清理。它跟 moment 间隔一起受
#     :data:`app.agent.continuity.MAX_CLEANUP_MINUTES` 约束：一张图最长活一个清理
#     周期加一个间隔，再长就比它的预签名地址活得久了（用例钉住）
#   * ``hard_cap_tokens`` 200k / ``trim_target_tokens`` 100k  兜底，不是主路
MOMENT_TRIM_POLICY = TrimPolicy(
    material_minutes=60,
    own_minutes=240,
    cleanup_minutes=60,
    hard_cap_tokens=200_000,
    trim_target_tokens=100_000,
)


# ---------------------------------------------------------------------------
# 循环
# ---------------------------------------------------------------------------


async def life_moment_minutes() -> int:
    """两个 moment 之间至少隔多少分钟；没配 / 配脏退回默认值。

    Dynamic Config 的拉取是同步 httpx（10s 缓存），走 ``asyncio.to_thread`` 避免
    缓存刷新那一次阻塞事件循环。
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


async def latest_moment(*, lane: str, persona_id: str) -> LifeMoment | None:
    """这个人**最后落地**的那个 moment，两种 moment 都算；一个 moment 都没跑过返回 ``None``。

    这一轮要问它两件事：离上一次过了多久（它的『现在』），和上一轮的上下文落地了没有（它的
    ``context_ver``，见 :func:`lost_last_round`）。她的上下文只有一条，两种 moment 都接在它
    后面写，所以不筛 ``nudged``。

    **按 ``seq`` 排，不按 ``began_at``。** 后者是她那个 moment 的钟点，问的不是落库先后：
    常规 moment 的 ``began_at`` 还记着格子的那些旧行里，排在提前来的 moment 后面落地的常规
    moment 钟点反而更早，按钟点取就会取回提前来的 moment 那一行——它的 ``context_ver`` 比后
    落地那一轮小一版，后落地那一轮的上下文没写成也判不出来。

    ``COALESCE(seq, 0)`` 是加列的另一半防线：DESC 排序下 pg 把 NULL 放**最前**，
    不接住的话已有数据的泳道会一直取回某一条旧行。旧行统一是 0 号，它们之间的先后退回
    ``began_at``——加列之前唯一有过的那个口径。
    """
    sql = (
        f"SELECT * FROM {_MOMENT_TABLE} "
        f"WHERE lane = :lane AND persona_id = :persona_id "
        f"ORDER BY COALESCE(seq, 0) DESC, began_at DESC LIMIT 1"
    )
    row = await fetch_one(sql, lane=lane, persona_id=persona_id)
    return None if row is None else from_row(LifeMoment, row)


async def latest_regular_moment(
    *, lane: str, persona_id: str
) -> LifeMoment | None:
    """这个人最近跑过的那**一格**（钟点上该来的那种 moment）；一个都没有返回 ``None``。

    间隔判断只认它。用"最近一次"判的话，每被人叫来一次就把她的固定节奏往后推一次
    ——她一天被搭话十次，那十分钟的节拍就成了不定期的。

    **这里按 ``began_at`` 排，跟 :func:`latest_moment` 不是同一个问题**：那边问"最后
    落地的是谁"，这边问"跑过的格子里最晚的是哪一格"。常规 moment 的 ``began_at`` 落在它
    那一格里（旧行就是格子本身），所以钟点最晚的那一个就落在最晚的那一格。
    """
    sql = (
        f"SELECT * FROM {_MOMENT_TABLE} "
        f"WHERE lane = :lane AND persona_id = :persona_id "
        # 旧行的 nudged 是 NULL（后加的列没有 DB 默认值）：NULL 既不等于 false
        # 也不等于 true，不 COALESCE 的话历史全被过滤掉，她每一拍都当成从没跑过。
        f"AND COALESCE(nudged, false) = false "
        f"ORDER BY began_at DESC LIMIT 1"
    )
    row = await fetch_one(sql, lane=lane, persona_id=persona_id)
    return None if row is None else from_row(LifeMoment, row)


async def moment_ran(*, lane: str, persona_id: str, moment_id: str) -> bool:
    """这个 moment 跑过了吗（按身份问，不按时间）。"""
    sql = (
        f"SELECT 1 FROM {_MOMENT_TABLE} WHERE lane = :lane "
        f"AND persona_id = :persona_id AND moment_id = :moment_id LIMIT 1"
    )
    row = await fetch_one(sql, lane=lane, persona_id=persona_id, moment_id=moment_id)
    return row is not None


# 上下文写失败的计数。没有对应的库表列：下一个 moment 冷启动本身就在 trace 和日志里看得
# 见，再加一列只是把同一件事记两遍。
CONTEXT_WRITE_FAILED = get_or_create_counter(
    "living_context_write_failed_total",
    "她这一轮的上下文没写进去的次数（这一轮仍然算数，下一轮重铺状态）",
    ["lane", "persona_id"],
)

# 下一个 moment 发现缺口的次数。**跟上面那个不是同一件事**：写失败那条只有走到
# :func:`remember_this_round` 才记得上，进程崩在 moment 记录和上下文之间时一个数都
# 不动。这个计数是那种情形唯一的痕迹。
CONTEXT_GAP = get_or_create_counter(
    "living_context_gap_total",
    "醒来发现上一轮的上下文没落地的次数（这一轮重铺一次状态）",
    ["lane", "persona_id"],
)


def lost_last_round(last: LifeMoment | None, *, loaded_ver: int) -> bool:
    """上一轮的上下文到底落地了没有。

    moment 记录先提交、上下文后写，所以记录上的 ``context_ver``（那一轮**打算**写成
    第几版）是一个下一轮读得到的期望值：读到的版本比它小，就是那次写没落地。写失败和
    "崩在两次提交之间"在这里是同一个判据 —— 后者连一行 ERROR 都留不下，所以判据不能
    挂在写失败那条补偿路径上。

    一种情形不判：**一个 moment 都没跑过**，或者那一行是加列之前写的
    （``context_ver`` 读出来是 0）—— 没有可比的期望值。

    这里原来还有第二道：上一个 moment 不在这个生活日上就不判，因为新的一天是另一条
    上下文、版本从 0 起、跟昨天那个数没有可比性。**上下文不再分天之后那道门不存在
    了** —— 同一个人从头到尾就一条，版本一路往上走，跨不跨 04:00 都直接比得了。

    所以它也不再需要知道这一轮读的是哪条键：``last`` 按 ``(lane, persona_id)`` 查出来，
    上下文也按这两样存，两者必然是同一条。
    """
    if last is None or last.context_ver <= 0:
        return False
    return loaded_ver < last.context_ver


def nudged_moment_id(nudged_by: str) -> str:
    """被叫来提前的那个 moment 的身份：``nudge:<把她叫来的那条>``（理由见 :class:`DueMoment`）。"""
    return f"nudge:{nudged_by}"


@dataclass(frozen=True)
class DueMoment:
    """判出来要跑的她的一轮：身份、她这一轮的『现在』、为什么跑。

    两条路判它：钟点上该来的（:func:`regular_moment_due`）和被人叫来提前的
    （:func:`app.living.life.nudge.nudged_moment_due`）；跑它的是同一个 :func:`run_due_moment`。
    判和跑在同一次轮到她的时候（她的轮次队列，:mod:`app.living.life`）：判在轮到她之前的话，
    判完到轮到她之间可能隔着一整轮，那一轮会把判据改掉（看过了、跑过了），判出来的就是过期的结论。

    **两种 moment，身份不一样。**

    *钟点上该来的*：身份是她那一格的起点，理由见 :func:`regular_moment_due`。

    *被人叫来提前的*：身份是 ``nudge:<把她叫来的那条消息>``，**不落格子**。这一个决定同时解掉
    两件事：

      1. 跟同一分钟的常规 moment **撞不上 id**（一个是钟点串，一个是 ``nudge:`` 开头）；
      2. **同一条消息只能把她叫来一次**——身份就是那条消息，跑过就不再跑。这不是冷却
         也不是计数器：真人手机是新消息才震，躺着的未读不会一直震。她没看手机的话
         那条一直未读，按"还有没有未读"判就是每分钟震一次。

    **她这一轮的『现在』是判它时读的钟**，两种都一样：拿到占用之后、读任何状态之前读的那一刻，
    不是钟敲响那一拍。前一轮挂住时（模型调用卡住，最长占锁 15 分钟，见
    :data:`app.living.common.HOLD_LIMIT`），排在后面的那一拍要过一阵才轮到她；她看到的是轮到她
    时的样子（收件箱、手机、她自己的经历都是那时读的），她做的事、发出去的消息带的也就必须是那一
    刻。拿那一拍的时刻，world 收到的汇总就比汇总里回应的那件事还早。

    『现在』每次重跑都不一样，幂等却不丢：一个 moment 里所有派生 id（happening_id、whereabouts
    的自然键）都从 ``moment_id`` 来，**不从『现在』来**；``moment_id`` 稳了，重跑照样是 no-op，
    第一遍已经落下的那几件留着第一遍的钟点。前提是重跑时身份不变：常规的那种靠格子；被叫来的
    那一轮没落地，重跑判出的是它记下的"开始了"的那一轮（:class:`app.living.life.nudge.NudgeBegun`），
    不会因为这期间又来了别的消息就换一个身份。
    """

    # 为什么跑这一轮，记进日志。
    reason: str
    lane: str
    persona_id: str
    # 这一轮的身份，她这一轮所有派生 id 都从它来。
    moment_id: str
    # 她这一轮的『现在』。
    now: datetime
    nudged: bool = False
    # 收件箱里叫醒她这一轮的那条消息的 id（理由见 :class:`app.living.services.HerDue`）。
    must_show: str | None = None


async def regular_moment_due(
    *, lane: str, persona_id: str, clock: Callable[[], datetime]
) -> DueMoment | None:
    """钟点上该来的那一轮：这一格还没有常规 moment 跑过，就判出它；跑过了返回 ``None``。

    先读钟（理由见 :class:`DueMoment`），再把这一刻落到间隔网格上：格子大小每次从 Dynamic
    Config 读（:func:`life_moment_minutes`），从北京时间的午夜起算、向下取整、秒一律丢掉，所以
    同一个钟点每天落在同一格上，跨天不漂。**这一格的起点（精确到分）就是这一轮的身份。**

    **为什么身份是格子，不是钟表上那一瞬。** 这一轮所有派生 id 都带着身份：``happening_id``、
    whereabouts 的自然键、说话的认领、读书那一程。副作用先落库、``LifeMoment`` 后落库，中间崩掉的
    话下一拍会重跑，而那时的钟已经不是原来那一刻了。身份跟着钟动，派生 id 就全跟着动，幂等直接被
    击穿——她把同一句话说两遍，而且两条记录长得不一样（差几分钟），事后看不出是重复。落在格上，
    一格之内的每一拍都判出同一个身份，重跑退化成无害的 no-op。

    **这不能替代真事务。** 它保证的是"重跑同样的动作不会写出重复行"，不保证"重跑产出不同的动作
    时不会两边都留下"——模型是不确定的，重跑一次未必做一样的事。真正的全有全无要把整个 moment
    包进一个数据库事务，而那意味着一条业务连接被一次几十秒的模型调用占着（:mod:`app.host.turns`
    的说明里写了为什么不能这么做）。所以这里选的是"身份稳住 + 派生 id 幂等"，残余缺口写在这儿，
    别当成事务用。

    **该不该跑只看这一格有没有常规 moment 跑过**（:func:`latest_regular_moment`）。提前来的那一轮
    不算一格：不然每被人叫来一次就把她的固定节奏往后推一次。失败了，下一拍在同一格里以同一个身份
    再跑；跨出这一格，这一格就不补了。这里什么都不记，一个刚起来的进程从库里判出的跟一直跑着的
    进程一样。

    不带时区的钟直接拒：它落进身份会被按服务器时区解释，静默偏几个小时，整条派生链跟着错位，
    而且一句报错都没有。
    """
    now = clock()
    minutes = await life_moment_minutes()
    local = require_aware("now", now).astimezone(CST)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = int((local - midnight).total_seconds() // 60)
    cell = midnight + timedelta(minutes=(elapsed // minutes) * minutes)
    # 这一格有常规 moment 跑过了：上一个常规 moment 的『现在』落在这一格里（或者更晚）。
    last_regular = await latest_regular_moment(lane=lane, persona_id=persona_id)
    if last_regular is not None and last_regular.began_at >= cell:
        return None
    moment_id = cell.isoformat(timespec="minutes")
    return DueMoment(
        reason=f"常规节奏，{moment_id} 那一格",
        lane=lane,
        persona_id=persona_id,
        moment_id=moment_id,
        now=now,
    )


async def run_due_moment(resident: AssembledProfile, due: DueMoment) -> LifeMoment:
    """跑判出来的这一轮（:class:`DueMoment`），交回落了地的那一行。它在她的轮次队列上跑，判它和
    跑它在同一次轮到她的时候（:mod:`app.living.life`）。

    这一轮按 ``resident``（宿主按清单组好的那份，:mod:`app.living.life` 交给两个唤醒来源）由固定
    循环跑（:func:`app.agent.round.run_round`）：身份、『现在』、为什么跑都是 ``due`` 里判好的。一轮
    由哪些东西组成、各自为什么在那个位置，写在下面那一节。

    **她被带到那一刻，回不回是她的输出。** 这里只负责把她带到，不看她说了什么、也没有
    任何"她该不该回"的判断——那是替她做决定。

    模型的临时失败不整轮重来（:data:`LIFE_MOMENT_AGENT` 的 ``max_retries``）。
    """
    result = await run_round(
        resident,
        participant=due.persona_id,
        round_id=due.moment_id,
        now=due.now,
        reason=due.reason,
        due=due,
    )
    moment = result.round.state(_Landed).moment
    logger.info(
        "living moment lane=%s persona=%s %s %s、说：%s",
        moment.lane,
        moment.persona_id,
        moment.moment_id,
        f"改去 {moment.doing}（{moment.pulled_by}）" if moment.switched else "继续",
        moment.said,
    )
    return moment


# ---------------------------------------------------------------------------
# 她的一轮由什么组成
#
# 下面这些由 :mod:`app.living.life` 登记，清单的 ``resident`` 定它们的位置和顺序：一轮开始前
# 把她之前做了还没发出去的发出去；读上一个 moment 和她的上下文；摆状态段（她此刻的样子）和输入段
# （这一轮新发生的）；调模型、记成本；在一个事务里落这一轮（LifeMoment → 手机已读 → 收件已读）；
# 然后存上下文、把这一轮做的事发出去。
#
# **上下文接着上一个 moment。** 开头读她那条上下文（键是 :data:`HISTORY_KEY`），结尾把"历史 +
# 这一轮的刺激 + 这一轮模型产出的每一条"写成下一版（:mod:`app.agent.continuity`）。这一轮跨过
# 清理点时先裁一遍再喂（:func:`app.agent.continuity.trim_for_round`）：素材换成短语、过了 4 小时
# 的整组删掉、她此刻的状态（下面四个状态段）作为新起点插进去。裁在模型调用之前，所以喂进去的和
# 存下去的是同一份前缀，而且一段带着过期图片地址的历史不会在模型调用那一步先炸掉、永远轮不到
# 被裁；收尾那一步只剩 token 硬顶兜底（:func:`app.agent.continuity.next_transcript`）。
# ---------------------------------------------------------------------------


@dataclass
class _LastMoment:
    """上一个落地的 moment（这一轮的状态）：:func:`previous_moment_at` 读到的那一行，
    :func:`lost_the_last_round` 判上一轮的上下文落没落地用的也是它。"""

    moment: LifeMoment | None = None


@dataclass
class _Landed:
    """这一轮落了地的那一行（这一轮的状态），:func:`run_due_moment` 交回它。"""

    moment: LifeMoment | None = None


async def previous_moment_at(round: Round) -> datetime | None:
    """ "离上一次过了多久"就摆在她眼前那一行上：她上一次回到自己身上是什么时候。读的是最后落地的
    那个 moment，两种都算，不筛 nudged（:func:`latest_moment`）；一个都没跑过是 ``None``。"""
    last = await latest_moment(lane=round.lane, persona_id=round.participant)
    round.state(_LastMoment).moment = last
    return last.began_at if last is not None else None


async def lost_the_last_round(round: Round, version: int) -> bool:
    """上一轮的上下文落地了没有（:func:`lost_last_round`；``version`` 是这一轮读到的上下文版本）。

    没落地的话这一轮眼前的历史停在更早的地方，而刺激写着"离上一次过了十分钟"，指的是她看不到
    的那一轮 —— 所以要重铺一次状态（:func:`app.agent.continuity.trim_for_round` 的
    ``lost_last_round``）。不靠写失败那条补偿路径（:func:`remember_this_round`），因为进程崩在两次
    提交之间时那条路根本跑不到。
    """
    last = round.state(_LastMoment).moment
    gap = lost_last_round(last, loaded_ver=version)
    if gap:
        CONTEXT_GAP.labels(lane=round.lane, persona_id=round.participant).inc()
        logger.error(
            "上一轮的上下文没落地：%s 读到 ver=%d，上一个 moment（%s）记的是 "
            "ver=%d。这一轮把她的状态重铺一次",
            HISTORY_KEY.format(lane=round.lane, participant=round.participant),
            version,
            last.moment_id if last is not None else "",
            last.context_ver if last is not None else 0,
        )
    return gap


# -- 摆到她眼前的 ------------------------------------------------------------
#
# **只送新发生的事**（输入段）：几点了、离上一次隔了多久、她在哪在做什么、有什么到点了、传到她
# 这里的消息、手机上刚来了什么。她此刻的样子（状态段：上一次写下的那天、心里挂着什么、手机上还有
# 什么没看）读一百遍字字一样，每轮重发就是把同一段话抄一遍，而她上一轮读过的还在上下文里。它由
# 清理那一下作为新起点重铺（默认一小时一次；上下文是空的那一轮也会立一根界桩，所以冷启动她照样
# 知道自己站在哪）。状态段每一轮都算，只有立界桩的那一轮进请求。几点了、离上一次隔了多久那一段在
# :mod:`app.living.life.now`，"离上一次"读的是 :func:`previous_moment_at`。


# -- 落地 ----------------------------------------------------------------------
#
# **这个 moment 落地和她看过的手机是同一个事务**（清单 ``resident`` 的 commit 那三步）。工具返回
# 不等于她看见了——只有这个 moment 跑完，工具结果才真的进过她的上下文。分开写的话，崩在两者之间
# 就是"已读了但内容从没到她眼前"，那几条消息永久消失且一句报错都没有。绑在一起之后崩掉的代价只是
# 她下一个 moment 原样再来一遍：宁可重看，不可漏看。传到她这里的消息同理：只把这一轮摆进去的那几条
# 记成看过，也在这个事务里。


async def land_the_moment(
    round: Round, *, loose_ends: LooseEnds, her_records: HerRecords
) -> None:
    """落这一轮的 :class:`LifeMoment`。落地号也在这个事务里取
    （:func:`app.data.ordered.insert_in_commit_order`），所以号的先后就是提交的先后。这一格已经在
    别处落了地的话（两个进程跑同一格），不再写一行，留下库里那一行。

    ``context_ver`` 是这一轮**打算**把上下文写成第几版，下一轮拿它判这次写落没落地
    （:func:`lost_last_round`）。``open_ends`` 是这时她还挂着几件（``loose_ends`` 服务）。
    """
    switches = round.state(Switched).changes
    where = await her_records.current_whereabouts(lane=round.lane, persona_id=round.participant)
    moment = LifeMoment(
        lane=round.lane,
        persona_id=round.participant,
        moment_id=round.round_id,
        began_at=round.now,
        switched=bool(switches),
        pulled_by=switches[-1]["because"] if switches else "",
        recorded=her_records.recorded_this_round(round),
        doing=where.doing if where is not None else "",
        open_ends=await loose_ends.open_count(
            lane=round.lane, persona_id=round.participant
        ),
        said=round.reply.text().strip(),
        context_ver=round.history.version + 1,
        nudged=round.due.nudged,
    )
    round.state(_Landed).moment = await insert_in_commit_order(moment)


async def remember_this_round(round: Round) -> None:
    """把这一轮写进她的上下文（:func:`app.agent.round.store_history`）；写不进去先加一个计数再抛，
    循环记一行 ERROR（登记成 ``Continue``），这一轮仍然算数，下一个 moment 会发现这个缺口并重铺
    一次状态（:func:`lost_the_last_round`）。

    **这一步在落地那个事务之后，而且不参与它。** 理由是两种代价不对称：她已经开过口了，出站的
    消息、上传的图、换过的 ``switch_to`` 都回滚不掉，让这一轮失败只会让下一拍重放，而发送去重键
    带着 moment_id 和正文，换个措辞或跨一个时间格就对不上 —— 她于是把同一句话对真人再说一遍。
    丢掉这一轮上下文的代价小得多：下一个 moment 冷启动，而她做过说过的事在状态的"你刚做过、说过"
    那段里，读的是刚才已经提交的 ``Happening``。
    """
    try:
        await store_history(round)
    except Exception:
        CONTEXT_WRITE_FAILED.labels(lane=round.lane, persona_id=round.participant).inc()
        raise
