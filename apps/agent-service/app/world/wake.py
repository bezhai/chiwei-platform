"""world 什么时候醒：只因两种原因——收件箱来了消息，或者它自己定的时刻到了。

没有心跳，也没有默认间隔。每一轮最后由主 agent 定下次醒来的时刻（:mod:`app.world.main_agent`），
这里负责把它变成现实。

**私有状态**（``$WORLD_DATA_DIR/<泳道>/next_wake.json``）只记一样：**最新唤醒**——哪一条自定
消息（id）、什么时刻、为什么。只有 world 自己读写这个文件；人工读写接口只够得到 ``records/``。

**先发后记**（:func:`set_next_wake`）：先用通信机制排一条那个时刻送达自己的消息（``send_at``，
消息 id 就是新唤醒的 id，正文就是那段说明），broker 确认之后才把它记成最新唤醒。所以状态里
记的唤醒，一定已经排出去了，或者就是正在处理、正在重试的那一条。

* 排的时候失败（包括 broker 其实收下了却没确认、一轮超时取消落在等确认时）：状态不动，这一轮
  算失败，叫醒它的那条消息重投时照常跑，重跑时再排一个新的。
* 排出去了、记的时候失败：同样算这一轮失败、重跑；重跑排的新唤醒记进状态，先排出去的那条
  到点时是旧消息。
* 进程死在"排出去"和"记下来"之间：重启时（:func:`wake_on_start`）状态里还是叫醒那一轮的那条，
  时刻已经过了，立刻排一个新的；先前那两条都成了旧消息。

**旧的自定唤醒跳过**（:func:`is_stale_wake`）：一条自己发给自己的普通消息，id 跟状态里的最新
唤醒对不上，就是被后来定的时刻取代了，跳过、不跑一轮。不然每来一条别人的消息，就多出一条
唤醒链。别人发来的消息、机制发回来的"没有送达"告知不受这条影响。

**最新唤醒永不进死信**（:func:`retry_latest_wake_without_limit`，收件箱的 ``retry_without_limit``）：
它那一轮失败了，通信机制按封顶的指数退避一直重试（上限走 Dynamic Config
:data:`WAKE_RETRY_CAP_MINUTES_KEY`），每一次失败都在记录者里记一行、日志里一条 warning。
它进了死信就再没有东西会叫醒 world。别人的消息照常有限次重试、然后进死信——那时状态里原定
的下次唤醒还在，world 本来就会按时醒。

**进程启动时**（:func:`wake_on_start`，收件箱开设时、拿到卷的写锁之后）：状态为空、读不出来、
或者记的时刻已经过了，立刻排一个新的自定唤醒（同样先发后记）；记的时刻还没到，按原 id、原
时刻再排一次，防 broker 把它弄丢，接收方按 id 去重，不会醒两次。
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from inner_shared.dynamic_config import dynamic_config

from app.infra.cst_time import now_cst, to_cst_full
from app.messaging.message import Kind, Message
from app.messaging.sending import send_at
from app.world.volume import lane_dir, require_writer_lock, write_atomically

logger = logging.getLogger(__name__)

# world 在通信机制里的名字：它的收件箱，也是它给自己排消息时的发送方和接收方。
WORLD = "world"

_STATE_FILE = "next_wake.json"

# Dynamic Config：最新唤醒那一轮一直失败时，两次重试之间最长隔多久。改它不用重新部署。
WAKE_RETRY_CAP_MINUTES_KEY = "world_wake_retry_cap_minutes"
DEFAULT_WAKE_RETRY_CAP_MINUTES = 60


@dataclass(frozen=True)
class NextWake:
    """定下的一次醒来：哪一条自定消息、什么时刻、为什么（这段话也是那条消息的正文）。"""

    message_id: str
    at: datetime
    reason: str


def read_next_wake() -> NextWake | None:
    """状态里的最新唤醒；没有、或者读不出来都是 ``None``（启动时按"没有"补醒）。"""
    path = lane_dir() / _STATE_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))["latest"]
        return NextWake(
            message_id=str(raw["message_id"]),
            at=datetime.fromisoformat(raw["at"]),
            reason=str(raw["reason"]),
        )
    except FileNotFoundError:
        return None
    except (ValueError, KeyError, TypeError):
        logger.error("world: %s is unreadable; treated as no wake set", path, exc_info=True)
        return None


def _record_next_wake(wake: NextWake) -> None:
    directory = lane_dir()
    directory.mkdir(parents=True, exist_ok=True)
    write_atomically(
        directory / _STATE_FILE,
        json.dumps(
            {
                "latest": {
                    "message_id": wake.message_id,
                    "at": wake.at.isoformat(),
                    "reason": wake.reason,
                },
                "written_at": now_cst().isoformat(),
            },
            ensure_ascii=False,
        ),
    )


async def _schedule(wake: NextWake) -> None:
    await send_at(
        sender=WORLD,
        recipient=WORLD,
        body=wake.reason,
        at=wake.at,
        message_id=wake.message_id,
    )


async def set_next_wake(at: datetime, reason: str) -> NextWake:
    """定下次醒来：先排出去，broker 确认之后才记成最新唤醒。``at`` 必须带时区。

    ``reason`` 是那条消息的正文，醒来时原样摆到主 agent 眼前。排失败（``SendFailed``）或者记
    失败都往外抛；排失败时状态没动。
    """
    require_writer_lock()  # 记不下来的唤醒不排：没拿着卷的写锁就别往外发
    wake = NextWake(message_id=uuid.uuid4().hex, at=at, reason=reason)
    await _schedule(wake)
    _record_next_wake(wake)
    logger.info("world: next wake %s at %s", wake.message_id, at.isoformat())
    return wake


def is_own_wake(message: Message) -> bool:
    """这是 world 给自己排的一次醒来吗：自己发给自己的普通消息。

    "没有送达"告知的发送方也是 world（通信机制把退回的告知记成原发送方发给自己），但它的
    类型不是普通消息。
    """
    return message.kind is Kind.MESSAGE and message.sender == WORLD


def is_stale_wake(message: Message) -> bool:
    """这是一条被后来定的时刻取代了的自定消息吗。只有自己发给自己的普通消息才可能是。"""
    if not is_own_wake(message):
        return False
    latest = read_next_wake()
    return latest is None or latest.message_id != message.message_id


async def _retry_cap_minutes() -> int:
    minutes = await asyncio.to_thread(
        dynamic_config.get_int,
        WAKE_RETRY_CAP_MINUTES_KEY,
        default=DEFAULT_WAKE_RETRY_CAP_MINUTES,
    )
    if minutes <= 0:
        logger.warning(
            "dynamic config %s = %r is not a positive integer; using %d",
            WAKE_RETRY_CAP_MINUTES_KEY,
            minutes,
            DEFAULT_WAKE_RETRY_CAP_MINUTES,
        )
        return DEFAULT_WAKE_RETRY_CAP_MINUTES
    return minutes


async def retry_latest_wake_without_limit(message: Message) -> timedelta | None:
    """收件箱里一条消息处理失败时：它是状态里的最新唤醒，就不限次数重试（交回退避上限）。

    别的消息交回 ``None``，照常有限次重试、然后进死信。
    """
    if not is_own_wake(message) or is_stale_wake(message):
        return None
    return timedelta(minutes=await _retry_cap_minutes())


async def wake_on_start() -> None:
    """进程启动、收件箱开设时（已经拿到卷的写锁）：按私有状态决定要不要立刻醒一次。"""
    latest = read_next_wake()
    now = now_cst()
    if latest is None:
        logger.info("world: no wake set; waking now")
        await set_next_wake(now, "进程刚启动，没有找到你定下的下次醒来时刻。")
    elif latest.at <= now:
        logger.info("world: wake %s at %s has passed; waking now", latest.message_id, latest.at)
        await set_next_wake(
            now,
            f"进程刚启动。你上一次定下的醒来时刻（{to_cst_full(latest.at.isoformat())}）"
            f"已经过了，那一次的说明是：{latest.reason}",
        )
    else:
        logger.info(
            "world: wake %s at %s is still ahead; scheduling it again",
            latest.message_id,
            latest.at,
        )
        await _schedule(latest)
