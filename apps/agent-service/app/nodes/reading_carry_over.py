"""一次性的搬运：旧的读书队列里的消息搬进读书收件箱（``POST /admin/reading/carry-over``）。

读书原来走持久边，队列是 ``durable_file_picked_up_read_a_round``（泳道里带 ``_<泳道>`` 后缀）；现在
走 living 插件开设的读书收件箱（:data:`app.living.services.READING`），新版本不再消费旧队列。
旧队列里剩下的消息由这条路由处理：先只看（``dry_run``，默认），再搬，最后删掉本泳道的空队列。
prod 切换完成、旧队列删掉之后，这条路由连同 monitor-dashboard 的转发一起删除。

**看哪几条队列。** 本进程泳道的那一条；在泳道里还有不带后缀的同名队列：旧的泳道队列里 10 秒没人
消费的、被拒收的消息，都按 prod 的 routing key 进了这一条。prod 上两者是同一条。共用的
``dead_letters`` 不碰：prod 被拒收的旧读书消息留在那里，``/admin/dlq/*`` 照样能看。队列都用
passive declare 打开，不存在就报不存在，不会建出来。

**每条消息归哪一类**，按这个顺序判断：

* 解不开（不是那几个字段的 JSON）的：``unreadable``，原样放回；
* 正文里的 ``lane`` 不是本进程泳道的：``foreign``，回答里按泳道报条数（``foreign_lanes``）；
* broker 加的 ``x-death`` 头最近一项是 ``rejected`` 的：``dead``，旧的持久边处理失败、拒收过的；
* 其余：``unprocessed``，从没被处理过的一程。

**搬**（``dry_run=false``）：``unprocessed`` 的按读书收件箱的格式发过去
（:func:`app.living.reading.send_round`：消息 id 是 ``round_id``，发送方是那个人在世界里的名字），
发送成功（记录写好、broker 确认）之后才确认旧消息；那个人不在三姐妹里、或者没送到，就放回，计入
``failed``。``discard_dead=true`` 时 ``dead`` 的逐条把消息体写进日志再确认掉，否则放回。
``discard_foreign=true`` 时 ``foreign`` 的也这样丢掉，否则放回：prod 不带后缀的那条旧队列里可能有 ppe
泳道 10 秒没人消费、转进来的消息，那些泳道已经没有消费者，不丢掉它们，这条队列永远删不掉；要不要丢，
由人看过 ``dry_run`` 报的泳道之后决定。``unreadable`` 的永远放回。同一程不会读两遍：收件箱按消息 id
去重，读书那一侧按 ``FileRead.round_id`` 跳过已经提交过的那一程。

**删**（``delete_empty=true``，``dry_run=false`` 时才做）：只删本泳道的那一条，而且只在它空的时候删
（``if_empty``）。泳道里不删不带后缀的那一条：别的泳道可能还在跑旧版本、往里送消息。

**一次调用扫完每一条队列。** 先用 passive declare 拿到深度，然后取这么多条：取到的在这一次结束之前
都不放回，所以 ``basic.get`` 每次拿到的是下一条，不会把放回的那一条再取一遍——放回的消息回到队首，
要是一次只扫前面一截，排在后面的永远扫不到。搬走的在发送确认之后确认，丢掉的在写进日志之后确认，其余
在这一次全部处理完之后一起放回；放回和关通道每一步各自做，一步失败只记日志，不耽误其余的
（:func:`_put_back_and_close`）。取出来的消息都挂在通道上没确认，所以深度超过 :data:`SCAN_LIMIT` 的
队列一条都不取，只报深度和 ``too_many``（看的时候也一样）。回答里每条队列报操作前后的深度、消费者数、
各类计数。操作后的深度（``depth_after``）是放回、关通道之后马上量的，broker 要过一会儿才把放回的消息
全部算进就绪，放回得多时它偏小；要准确的数就再看一次。
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Annotated, Any

import aiormq
from pydantic import Field, ValidationError

from app.api.middleware import get_header_var
from app.data.schema import Data, Key
from app.infra.lane_policy import current_deployment_lane
from app.infra.rabbitmq import lane_queue, mq
from app.living.common import living_lane
from app.living.reading import FilePickedUp, send_round
from app.living.services import Residents, WhoLivesHere
from app.messaging.message import SendFailed

logger = logging.getLogger(__name__)

# 旧持久边的队列名（``durable_<数据>_<消费者>``）。
OLD_QUEUE = "durable_file_picked_up_read_a_round"

# 一次调用最多从一条队列里取多少条。整条队列在一次调用里取出来、处理完才放回，取出来的都挂在通道上
# 没确认；比它深的队列一条都不取。
SCAN_LIMIT = 5000


def _new_request_id() -> str:
    return uuid.uuid4().hex


class ReadingCarryOverRequest(Data):
    request_id: Annotated[str, Key] = Field(default_factory=_new_request_id)
    dry_run: bool = True
    discard_dead: bool = False
    discard_foreign: bool = False
    delete_empty: bool = False

    class Meta:
        transient = True


class ReadingCarryOverResponse(Data):
    lane: Annotated[str, Key]
    dry_run: bool
    queues: list[dict[str, Any]]

    class Meta:
        transient = True


@dataclass
class _Queue:
    """一条候选队列这一次的情况；``report()`` 是回答里的那一项。"""

    name: str
    exists: bool = False
    depth_before: int | None = None
    consumers: int | None = None
    depth_after: int | None = None
    # 深度超过 SCAN_LIMIT，这一次一条都没取。
    too_many: bool = False
    scanned: int = 0
    unprocessed: int = 0
    dead: int = 0
    foreign: int = 0
    foreign_lanes: dict[str, int] = field(default_factory=dict)
    unreadable: int = 0
    carried: int = 0
    failed: int = 0
    discarded_dead: int = 0
    discarded_foreign: int = 0
    deleted: bool = False
    dead_letters: list[dict[str, Any]] = field(default_factory=list)

    def report(self) -> dict[str, Any]:
        return {
            "queue": self.name,
            "exists": self.exists,
            "depth_before": self.depth_before,
            "consumers": self.consumers,
            "too_many": self.too_many,
            "scanned": self.scanned,
            "unprocessed": self.unprocessed,
            "dead": self.dead,
            "foreign": self.foreign,
            "foreign_lanes": self.foreign_lanes,
            "unreadable": self.unreadable,
            "carried": self.carried,
            "failed": self.failed,
            "discarded_dead": self.discarded_dead,
            "discarded_foreign": self.discarded_foreign,
            "depth_after": self.depth_after,
            "deleted": self.deleted,
            "dead_letters": self.dead_letters,
        }


def _last_death(headers: dict[str, Any] | None) -> dict[str, Any]:
    deaths = (headers or {}).get("x-death")
    if isinstance(deaths, list) and deaths and isinstance(deaths[0], dict):
        return deaths[0]
    return {}


def _text(value: Any) -> str | None:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value if isinstance(value, str) else None


def _sort(incoming, here: str) -> tuple[str, FilePickedUp | None]:
    """这条消息归哪一类（见模块说明），和解出来的那一程。"""
    try:
        picked = FilePickedUp.model_validate_json(incoming.body)
    except ValidationError:
        return "unreadable", None
    if picked.lane != here:
        return "foreign", picked
    if _text(_last_death(incoming.headers).get("reason")) == "rejected":
        return "dead", picked
    return "unprocessed", picked


async def _carry(picked: FilePickedUp, queue: str, residents: Residents) -> bool:
    """把这一程发进读书收件箱；送到了交回 ``True``。"""
    sender = residents.by_persona.get(picked.persona_id)
    if sender is None:
        logger.warning(
            "reading carry-over: round %s in %s belongs to %r, who is not one of the sisters; "
            "put back",
            picked.round_id,
            queue,
            picked.persona_id,
        )
        return False
    try:
        delivery = await send_round(picked, sender=sender)
    except SendFailed as exc:
        logger.warning(
            "reading carry-over: round %s in %s could not be sent: %s; put back",
            picked.round_id,
            queue,
            exc,
        )
        return False
    if not delivery.delivered:
        logger.warning(
            "reading carry-over: round %s in %s was not delivered (%s); put back",
            picked.round_id,
            queue,
            delivery.reason,
        )
        return False
    return True


async def _discard(incoming, queue: str, what: str) -> None:
    """丢掉一条：完整的消息体先写进日志，再确认掉。"""
    logger.warning(
        "reading carry-over: discarding %s from %s (operator %s): %s",
        what,
        queue,
        get_header_var("operator"),
        incoming.body.decode("utf-8", "replace"),
    )
    await incoming.ack()


async def _scan(channel, state: _Queue, req, held: list, residents: Residents) -> None:
    """打开这条队列，把开始时在里面的每一条分类、处理；要放回的放进 ``held``。"""
    try:
        queue = await channel.declare_queue(state.name, passive=True)
    except aiormq.exceptions.ChannelNotFoundEntity:
        return
    state.exists = True
    state.depth_before = int(queue.declaration_result.message_count)
    state.consumers = int(queue.declaration_result.consumer_count)
    if state.depth_before > SCAN_LIMIT:
        state.too_many = True
        logger.warning(
            "reading carry-over: %s holds %d messages, more than the %d one call takes; "
            "nothing taken",
            state.name,
            state.depth_before,
            SCAN_LIMIT,
        )
        return
    here = living_lane()
    # 取到的这一次结束之前都不放回，所以每次拿到的是下一条。
    for _ in range(state.depth_before):
        incoming = await queue.get(no_ack=False, fail=False)
        if incoming is None:
            break
        state.scanned += 1
        kind, picked = _sort(incoming, here)
        if kind == "unreadable":
            state.unreadable += 1
            held.append(incoming)
            continue
        if kind == "foreign":
            state.foreign += 1
            state.foreign_lanes[picked.lane] = state.foreign_lanes.get(picked.lane, 0) + 1
            if req.dry_run or not req.discard_foreign:
                held.append(incoming)
                continue
            await _discard(
                incoming, state.name, f"round {picked.round_id} of lane {picked.lane}"
            )
            state.discarded_foreign += 1
            continue
        if kind == "dead":
            state.dead += 1
            death = _last_death(incoming.headers)
            state.dead_letters.append(
                {
                    "round_id": picked.round_id,
                    "persona_id": picked.persona_id,
                    "title": picked.title,
                    "reason": _text(death.get("reason")),
                    "dead_lettered_from": _text(death.get("queue")),
                }
            )
            if req.dry_run or not req.discard_dead:
                held.append(incoming)
                continue
            await _discard(incoming, state.name, f"the old dead letter of round {picked.round_id}")
            state.discarded_dead += 1
            continue
        state.unprocessed += 1
        if req.dry_run or not await _carry(picked, state.name, residents):
            if not req.dry_run:
                state.failed += 1
            held.append(incoming)
            continue
        await incoming.ack()
        state.carried += 1


async def _put_back_and_close(channel, queue: str, held: list) -> None:
    """放回这条通道上留下的消息，再关掉通道。每一条放回、关通道各自做，失败只记日志、接着做下一步：
    放回失败的那一条还没确认，通道关上时 broker 照样把它放回原队列。"""
    for incoming in held:
        try:
            await incoming.nack(requeue=True)
        except Exception:
            logger.warning(
                "reading carry-over: putting a message back on %s failed; it goes back when "
                "its channel closes",
                queue,
                exc_info=True,
            )
    if channel.is_closed:
        return
    try:
        await channel.close()
    except Exception:
        logger.warning(
            "reading carry-over: closing the channel of %s failed; what it holds goes back "
            "when the connection closes",
            queue,
            exc_info=True,
        )


async def _depth(name: str) -> int | None:
    """队列现在的深度；不存在是 ``None``。不存在时 passive declare 会关掉通道，所以用一条一次性的。"""
    channel = await mq.open_channel()
    try:
        queue = await channel.declare_queue(name, passive=True)
        return int(queue.declaration_result.message_count)
    except aiormq.exceptions.ChannelNotFoundEntity:
        return None
    finally:
        if not channel.is_closed:
            await channel.close()


async def _delete_if_empty(name: str) -> bool:
    channel = await mq.open_channel()
    try:
        await channel.queue_delete(name, if_empty=True)
        return True
    except aiormq.exceptions.ChannelPreconditionFailed:
        return False
    finally:
        if not channel.is_closed:
            await channel.close()


async def carry_over_reading(
    req: ReadingCarryOverRequest, *, residents: WhoLivesHere
) -> ReadingCarryOverResponse:
    """看、搬、删旧的读书队列，见模块说明（发件人按 ``residents`` 服务读好的名字认）。"""
    known = residents.residents()
    lane = current_deployment_lane()
    own = lane_queue(OLD_QUEUE, lane)
    states = [_Queue(own)] if lane is None else [_Queue(own), _Queue(OLD_QUEUE)]
    # 每条队列：它的通道，和这一次结束时要放回的消息。
    scans: list[tuple[Any, _Queue, list]] = []
    try:
        for state in states:
            # 一条队列一条通道：队列不存在时 passive declare 会关掉它所在的通道。
            channel = await mq.open_channel()
            held: list = []
            scans.append((channel, state, held))
            await _scan(channel, state, req, held, known)
    finally:
        for channel, state, held in scans:
            await _put_back_and_close(channel, state.name, held)
    for state in states:
        if state.exists:
            state.depth_after = await _depth(state.name)
    own_state = states[0]
    if req.delete_empty and not req.dry_run and own_state.depth_after is not None:
        own_state.deleted = await _delete_if_empty(own)
    logger.info(
        "reading carry-over in lane=%s dry_run=%s (operator %s): %s",
        lane or "prod",
        req.dry_run,
        get_header_var("operator"),
        [
            {k: v for k, v in s.report().items() if k != "dead_letters"}
            for s in states
        ],
    )
    return ReadingCarryOverResponse(
        lane=lane or "prod", dry_run=req.dry_run, queues=[s.report() for s in states]
    )
