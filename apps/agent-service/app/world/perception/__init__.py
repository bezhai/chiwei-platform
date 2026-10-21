"""感知判断 agent：世界里发生了一个变化，谁会察觉、各自察觉到的是什么；以及把判断告知他们。

**每个变化判断一次**（:meth:`Perceiving.judge_who_notices`）。主 agent 报告一个变化
（:meth:`Perceiving.report_change`），或者一个 NPC 出场之后（他的言行原样作为变化），
就起一个感知判断 agent：它拿到清单给它列的工具（宿主组好的 ``world_perception``，``ctx.agent``）：
知识来源的查询工具，关掉的来源按这一轮开始时读的开关拿掉，跟主 agent 这一轮的一样；加上只有它有的
:func:`someone_notices`——每判断一个会察觉的人调一次，写下那个人察觉到的是什么，
以及要不要现在就叫他来看。谁会察觉、要不要现在就叫他来看，完全是它依据各来源做的判断：代码里
没有按位置、距离或者任何规则决定感知，也没有规则替它定哪些事急。

**它知道这一轮是被谁的什么消息叫醒的。** 输入里除了现在几点、这一次的变化，还有主 agent 这一轮
处理的全部消息，按到达的先后：每条谁发的、什么时候、说了什么（:func:`_perception_input`）。居民
发来的是她做了什么，做事的人已经知道自己做了什么，该告诉她的只是她不知道的那部分；这一点要有
依据，不能从变化描述里去猜是谁。一轮里可能有几个人各自发来消息，这一次的变化是谁引起的，由它
对照每条消息判断。

**模型的临时失败重试，失败那一次的判断作废。** 感知判断那次模型调用遇到 5xx、超时这类临时失败
（:data:`app.agent.RETRYABLE_EXCEPTIONS`），整次判断重来，最多 :data:`PERCEPTION_ATTEMPTS`
次。:func:`someone_notices` 只把判断记在这一次调用自己的状态里（:class:`Judgments`），不发任何
东西，所以失败那一次判断过谁、写了什么，都随那次调用丢掉；重来的那一次是一次新的调用，从空的
开始，发出去的只有成功那一次的判断。重来不交给 Agent 层（``max_retries``）：每一次尝试在这里
开一次新的调用、记一行自己的成本。试完了还是失败，交回调用方照旧处理（主 agent 看到"没有报告
出去"），日志里记一条 error 点明试了几次、最后为什么失败；每一次失败的调用在 Langfuse 里各是
一条标成 ERROR 的 trace。

**告知居民只有这一条路。** 每一条判断变成一条 :class:`~app.world.services.Notice`，消息 id 在判断完那一刻就定下；
:func:`send_notices` 把它原样按通信机制发给那个参与者（``send``，发送方是 world，用的就是这个 id）。
主 agent 没有直接给谁发消息的工具，所以"谁知道这件事"只由这一次判断决定，告知的内容也是判断
写下的那段话。

**判断和发送分开，中间先记下来。** 调用方（报告变化、让 NPC 出场）拿到判断之后，交给
:meth:`Perceiving.tell`：先把告知连同 id、要不要叫醒记进 :mod:`app.world.perception.unfinished`，再调
:func:`send_notices`。发到一半进程死了、被取消、或者发送出错，
下一轮开始时按原 id 再发一遍：投递至少一次，接收方按消息 id 去重，已经收到的人不会收到第二遍。
所以发送出错（记录写不进去、broker 没确认）不在这里收住，原样往外抛，这一轮按失败重来。

**报告变化、让 NPC 出场这两个动作不包 @tool_error。** 要记进 unfinished 的每一段话（变化原文、
NPC 的名字和情境、NPC 的言行）和每一条告知的正文，都先按通信机制的正文规则
（:func:`app.messaging.message.message_body`）检查过，记下来的东西一定写得进卷、发得出去。判断那一段
（感知判断、NPC 扮演）没成，什么都还没记、没发，失败交回给主 agent，它可以再试一次；交给
:meth:`Perceiving.tell` 之后的失败（写不进卷、发送出错、被取消）不收住，原样往外抛，这一轮按失败
重来，下一轮开始时按原 id 补发。包了 @tool_error 会把发送出错也变成一条交给模型的失败，这一轮照常
跑完、清空 unfinished，那条告知就再也发不出去了。

**没送达不另外处理。** 对方没开设收件箱时 ``send`` 不投递、记下来、当场交回"没有送达"，不会
再给 world 发一条告知，也就不会叫醒它。名字就是参与者在这个世界里的名字，通信机制校验不过的，
在判断那一刻就退回给感知判断 agent，让它改；正文也一样，通信机制收不下的（记录者存不下的字，见
:func:`app.messaging.message.message_body`）在判断那一刻退回，记下来、发出去的告知都是发得出去的。
所以 :func:`send_notices` 还会遇到的发送出错只剩基础设施的（broker、数据库不可用），按失败重来是对的。

prompt 在 Langfuse（:data:`PERCEPTION`），正文不引用任何变量；现在几点、这一轮处理的消息、这一次
的变化写在 USER 消息里。
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Annotated

from pydantic import Field

from app.agent import RETRYABLE_EXCEPTIONS
from app.agent.contracts import FailRound
from app.agent.neutral import Message as Turn
from app.agent.neutral import Role
from app.agent.round import (
    AssembledAgent,
    Round,
    current_call,
    current_round,
    open_call,
)
from app.agent.tooling import tool
from app.agent.tools.outcome import tool_error
from app.capabilities.errors import CapabilityInvalidArg
from app.capabilities.retry import retry
from app.host import Context, Plugin
from app.infra.cst_time import now_cst
from app.messaging.message import Kind, Message, message_body, participant
from app.messaging.sending import send
from app.world.agents import (
    WORLD,
    is_own_wake,
    lane,
    not_done,
    run_agent,
    when,
    world_agent,
    world_segments,
)
from app.world.perception import unfinished
from app.world.services import Notice

logger = logging.getLogger(__name__)

PERCEPTION = "world_perception"
PERCEPTION_AGENT = world_agent(PERCEPTION, "world-perception", "world_perception_model")

# 主 agent 的 prompt 里归这个插件的两段（Langfuse id）：怎么报告变化，没跑完的一轮里发生的事。
REPORT_CHANGE_SEGMENT = "world_round/report-change"
UNFINISHED_SEGMENT = "world_round/unfinished"

@dataclass
class Judgments:
    """一次感知判断调用的结果：参与者名字 → (他察觉到的那段话, 要不要现在就叫他来看)。调用自己的
    状态，调用方在调用返回之后从它持有的那次调用上读。"""

    by_name: dict[str, tuple[str, bool]] = field(default_factory=dict)


# 感知判断那次模型调用遇到临时失败（5xx、超时、连不上、限流，跟 Agent 层自己重试的是同一组：
# :data:`app.agent.RETRYABLE_EXCEPTIONS`）时最多试几次，两次之间等多久（翻倍，有上限）。
# 2026-10-06 在 coe-world 上，模型两次返回 500，那两个变化没有报告出去。一次调用最长等
# 180 秒（:data:`app.agent.client.MODEL_ANSWER_SECONDS`），三次加上等待不到十分钟，在一轮的
# 时限之内。
PERCEPTION_ATTEMPTS = 3
RETRY_BASE_SECONDS = 2.0
RETRY_MAX_SECONDS = 8.0


@tool
@tool_error("没有记下这条判断")
async def someone_notices(
    who: Annotated[
        str, Field(description="会察觉到的那个参与者，用他在这个世界里的名字")
    ],
    what: Annotated[
        str,
        Field(
            description=(
                "他察觉到的是什么：从他的角度写，什么时候、在哪、他看到听到感觉到了什么。"
                "这段话会原样发给他"
            )
        ),
    ],
    right_away: Annotated[
        bool,
        Field(
            description=(
                "要不要为这件事现在就打断他，叫他来看。true：现在就打断他，他马上来看这段话；"
                "false：不为这件事打断他，这段话照样会交给他，他下一次看的时候看到。"
                "不管哪一种，他读到的都是他在那个时刻察觉到的"
            )
        ),
    ],
) -> str:
    """判断一个参与者会察觉到这个变化，写下他察觉到的是什么，以及要不要现在就叫他来看。

    每个会察觉的人调一次；同一个人再调一次，以最后一次为准。没有人会察觉，就一次都不调。
    """
    name = who.strip()
    try:
        participant(name)
    except ValueError as exc:
        raise CapabilityInvalidArg(f"「{who}」不是通信机制收得下的参与者名字：{exc}") from exc
    if name == WORLD:
        raise CapabilityInvalidArg(f"「{WORLD}」是世界自己，不用告知")
    try:
        body = message_body(what.strip())
    except ValueError as exc:
        raise CapabilityInvalidArg(
            f"这段话通信机制发不出去，改一下再交：{exc}。写下他察觉到的是什么，它会原样发给他"
        ) from exc
    if not isinstance(right_away, bool):
        raise CapabilityInvalidArg(
            f"要不要现在就叫他来看，写 true 或 false，不是 {right_away!r}"
        )
    current_call().state(Judgments).by_name[name] = (body, right_away)
    return f"记下了：{name} 会察觉到。"


@dataclass(frozen=True)
class Told:
    """发一条告知的结果：送没送达（没送达时为什么）。"""

    notice: Notice
    delivered: bool
    reason: str | None = None


def _describe(message: Message) -> str:
    """这一轮的一条消息是谁的、什么时候的：别人发来的、世界自己定的醒来、通信机制退回的。"""
    if message.kind is Kind.NOT_DELIVERED:
        return f"通信机制退回给世界的一条没有送达的消息（{when(message.time)}）："
    if is_own_wake(message):
        # 正文是主 agent 当时写给自己的（:func:`app.world.core.main_agent.set_the_next_wake` 排醒来时
        # 那段），里面的"你"是世界；摆到感知判断眼前，"你"就成了它自己，所以说清楚。
        return (
            f"世界自己定的一次醒来（{when(message.time)}），不是谁发来的。"
            f'下面是世界当时给自己留的话，话里的"你"指世界自己：'
        )
    return f"{message.sender} 发来（{when(message.time)}）："


def _perception_input(change: str, round_messages: Sequence[Message]) -> str:
    lines = [
        f"【现在】{when(now_cst())}",
        f"【叫醒世界的消息】这一轮世界收到 {len(round_messages)} 条消息，按到达的先后：",
    ]
    for number, message in enumerate(round_messages, 1):
        lines += [f"（{number}）{_describe(message)}", message.body]
    return "\n".join([*lines, "【世界里发生的变化】", change])


class Perceiving:
    """感知判断和告知（world 的 ``perception`` 服务，:class:`~app.world.services.Perception`），以及
    主 agent 报告变化的动作（:meth:`report_change`）。``agent`` 是宿主组好的感知判断 agent
    （``ctx.agent``）：它的工具按清单排。"""

    def __init__(self, agent: AssembledAgent) -> None:
        self._agent = agent

    async def judge_who_notices(
        self, change: str, *, round_messages: Sequence[Message]
    ) -> list[Notice]:
        """为一个变化跑一次感知判断，交回判断出来的告知，每条带一个新的消息 id。一条都不发。

        ``round_messages`` 是主 agent 这一轮处理的消息，按到达的先后原样摆进感知判断的输入。它的
        工具是这一轮的：关掉的来源按这一轮开始时读的开关拿掉。模型调用的临时失败重试，最多
        :data:`PERCEPTION_ATTEMPTS` 次；每一次都从空的判断开始，失败那一次判断过谁不带进下一次
        （它也一条都没发：告知要等判断交回来之后才由调用方发）。别的失败、或者试完了还是失败，
        原样往外抛。
        """
        perception_input = _perception_input(change, round_messages)
        # 判断是在主 agent 这一轮里做的。
        world_round = current_round()
        spec = self._agent.spec
        tools = self._agent.tools_on(world_round.switched_off)

        @retry(
            attempts=PERCEPTION_ATTEMPTS,
            base_delay_s=RETRY_BASE_SECONDS,
            max_delay_s=RETRY_MAX_SECONDS,
            retry_on=RETRYABLE_EXCEPTIONS,
        )
        async def judge() -> tuple[str, dict[str, tuple[str, bool]]]:
            call_id = uuid.uuid4().hex
            call = open_call(PERCEPTION, spec, lane=lane(), round=world_round)
            await run_agent(
                call,
                spec,
                [Turn(role=Role.USER, content=perception_input)],
                prompts=world_round.prompts,
                tools=tools,
                call_id=call_id,
            )
            return call_id, call.state(Judgments).by_name

        try:
            call_id, judgments = await judge()
        except RETRYABLE_EXCEPTIONS as exc:
            logger.error(
                "world: perception failed on all %d attempts (last: %s: %s); the change is not "
                "told to anyone",
                PERCEPTION_ATTEMPTS,
                type(exc).__name__,
                exc,
            )
            raise
        logger.info(
            "world: perception %s judged %s",
            call_id,
            ", ".join(
                f"{who} ({'right away' if right_away else 'when they next look'})"
                for who, (_, right_away) in judgments.items()
            )
            or "nobody",
        )
        return [
            Notice(who=who, what=what, wakes_recipient=right_away, message_id=uuid.uuid4().hex)
            for who, (what, right_away) in judgments.items()
        ]

    async def tell(self, what: str, notices: Sequence[Notice]) -> str:
        """先把这件事（``what``，给主 agent 看的那段话）连同要发的告知记进
        :mod:`app.world.perception.unfinished`，再按各自的消息 id 发出去（:func:`send_notices`），交回给主
        agent 看的结果。记下来之后的失败原样往外抛：这一轮按失败重来，下一轮开始时按原 id 补发。"""
        happening = unfinished.note(what, list(notices))
        return render_told(await send_notices(happening.notices))

    # 主 agent 的动作，不包 @tool_error（见模块说明）。

    async def report_change(
        self,
        change: Annotated[
            str,
            Field(
                description=(
                    "世界里发生的一个变化：什么时候、在哪、什么变成了什么样。主语是世界：天气、"
                    "地方、物件、机构、其他人的处境"
                )
            ),
        ],
    ) -> str:
        """报告世界里发生的一个变化，让会察觉到它的人知道。

        由感知判断决定谁会察觉、各自察觉到的是什么，并告知他们；返回告知了谁、送没送达。
        这是让居民知道世界变化的唯一办法。一个变化报告一次。
        """
        try:
            change = message_body(change.strip())
        except ValueError as exc:
            return f"没有报告：这段话里有记不下来的东西，改一下再报告（{exc}）。"
        try:
            notices = await self.judge_who_notices(
                change, round_messages=current_round().due.messages
            )
        except Exception as exc:
            return not_done("没有报告出去，感知判断没有做成", exc)
        return await self.tell(f"你报告了一个变化：{change}", notices)


async def send_notices(notices: Iterable[Notice]) -> list[Told]:
    """把告知逐条按它自己的消息 id 发出去，交回每条的结果。

    发送出错（``SendFailed``）原样往外抛，后面的不再发：这些告知已经记下来了，这一轮按失败
    重来，下一轮开始时按原 id 全部再发一遍。
    """
    told = []
    for notice in notices:
        delivery = await send(
            sender=WORLD,
            recipient=notice.who,
            body=notice.what,
            message_id=notice.message_id,
            wakes_recipient=notice.wakes_recipient,
        )
        told.append(Told(notice, delivery.delivered, delivery.reason))
    return told


def render_told(told: list[Told]) -> str:
    """交给主 agent 看的告知结果：告知了谁、告知的什么、送没送达。"""
    if not told:
        return "感知判断：没有人会察觉到这个变化，没有告知任何人。"
    lines = ["感知判断之后，告知了这些人："]
    for t in told:
        status = "送达了" if t.delivered else f"没有送达（{t.reason}）"
        lines.append(f"- {t.notice.who}：「{t.notice.what}」——{status}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 没跑完的一轮：一轮开始前补发、摆到主 agent 眼前，上下文存下之后清空（:mod:`.unfinished`）
# ---------------------------------------------------------------------------


@dataclass
class LeftOver:
    """这一轮的状态：没跑完的那一轮里已经发生的事，每件连同这一轮开始前补发它的告知的结果。"""

    happenings: list[tuple[unfinished.Happening, list[Told]]] = field(
        default_factory=list
    )


async def resend_left_over(round: Round) -> None:
    """一轮开始前（``unfinished.resend``）：按原 id 把还没进上下文的那几件事要发的告知全部再发
    一遍，结果留给 :func:`left_over`。

    发送出错原样往外抛：这一轮在模型开始之前就算失败，记着的东西一样不动，下一轮再补。
    """
    left_over = [(h, await send_notices(h.notices)) for h in unfinished.read()]
    if left_over:
        logger.info(
            "world: resent %d notice(s) of %d happening(s) left by an unfinished round",
            sum(len(told) for _, told in left_over),
            len(left_over),
        )
    round.state(LeftOver).happenings = left_over


async def left_over(round: Round) -> str | None:
    """输入段（``unfinished.happenings``）：没跑完的那一轮里已经发生的事和补发告知的结果
    （:func:`resend_left_over`）。没有时不摆。"""
    happenings = round.state(LeftOver).happenings
    if not happenings:
        return None
    lines = [
        "【之前没跑完的一轮里已经发生的事】之前有一轮没有正常收尾。那一轮的经过你可能记得，"
        "也可能不记得；不管记不记得，下面这些在那一轮里已经发生了，记录里可能还没写。那一轮"
        "要发的告知，这一轮开始前已经按原样补发了一遍（收到过的人不会再收到一遍），下面是"
        "补发的结果："
    ]
    lines += [
        f"- {when(h.at)}\n{h.what}\n{render_told(told)}" for h, told in happenings
    ]
    return "\n".join(lines)


async def clear_unfinished(round: Round) -> None:
    """``unfinished.clear``：这一轮存进上下文了，没跑完的那一轮里发生的事不用再记着。"""
    unfinished.clear()


def setup(ctx: Context) -> None:
    perceiving = Perceiving(ctx.agent(PERCEPTION))
    ctx.provide("perception", perceiving)
    for segment in world_segments(
        PERCEPTION, REPORT_CHANGE_SEGMENT, UNFINISHED_SEGMENT
    ).values():
        ctx.segment(segment)
    for action in (tool(perceiving.report_change), someone_notices):
        ctx.tool(action, trim="kept")
    ctx.round_start("unfinished.resend", resend_left_over)
    ctx.section("unfinished.happenings", left_over)
    ctx.after_commit("unfinished.clear", clear_unfinished, on_failure=FailRound())


PLUGIN = Plugin(name="perception", setup=setup, provides=("perception",))
