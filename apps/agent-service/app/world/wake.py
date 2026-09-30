"""world 什么时候醒：只因两种原因——收件箱来了消息，或者它自己定的时刻到了。

没有心跳，也没有默认间隔。每一轮最后由主 agent 定下次醒来的时刻（:mod:`app.world.round`），
这里负责把它变成现实：

1. **先写私有状态**（``$WORLD_DATA_DIR/<泳道>/next_wake.json``）：下次醒来的时刻、一个新的
   消息 id、为什么定这个时刻。只有 world 自己读写这个文件；人工读写接口只够得到
   ``records/``。
2. **再用通信机制排一条那个时刻送达自己的消息**（``send_at``），消息 id 就是状态里记的那个，
   正文就是那段说明。

**私有状态是"下次什么时候醒"的唯一依据，那条自定消息只是触发。** 所以：

* **旧的自定消息作废。** 每一轮都会定一个新时刻，而上一轮定的那条消息还在路上。它到的
  时候，id 跟状态里记的对不上，就是被后来定的时刻取代了：跳过，不跑一轮
  （:func:`is_stale_wake`）。别人发来的消息、机制发回来的"没有送达"告知不受这条影响。
* **进程启动时按状态补醒**（:func:`wake_on_start`，挂在收件箱开设时）：
    - 没有记录（第一次启动、状态文件读不出来）或者记的时刻已经过了：立刻醒一次——排一条
      "现在"送达的自定消息，同样先写状态。首次启动、重启、一轮失败到进了死信之后，都靠
      这一条保证它不会一直睡下去。
    - 记的时刻还没到：不立刻醒，把同一条消息按原 id、原时刻再排一次。进程要是死在"写好
      状态"和"排好消息"之间，这是那条消息唯一的来源；原来那条要是还在，两条同 id 的消息
      只会被处理一次（通信机制按 id 去重）。

先写状态、后排消息，是因为反过来的话，死在两步之间会留下一条状态里没有的消息（到了
也被当成作废），而状态里还是上一次的时刻；先写状态则任何时候都能从状态把消息补出来。
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from app.infra.cst_time import now_cst, to_cst_full
from app.messaging.message import Kind, Message
from app.messaging.sending import send_at
from app.world.volume import lane_dir, write_atomically

logger = logging.getLogger(__name__)

# world 在通信机制里的名字：它的收件箱，也是它给自己排消息时的发送方和接收方。
WORLD = "world"

_STATE_FILE = "next_wake.json"


@dataclass(frozen=True)
class NextWake:
    """定下的下次醒来：哪一条自定消息、什么时刻、为什么（这段话也是那条消息的正文）。"""

    message_id: str
    at: datetime
    reason: str


def read_next_wake() -> NextWake | None:
    """私有状态里记的下次醒来；没有、或者读不出来都是 ``None``（启动时按"没有"补醒）。"""
    path = lane_dir() / _STATE_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
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


def _write_next_wake(wake: NextWake) -> None:
    directory = lane_dir()
    directory.mkdir(parents=True, exist_ok=True)
    write_atomically(
        directory / _STATE_FILE,
        json.dumps(
            {
                "message_id": wake.message_id,
                "at": wake.at.isoformat(),
                "reason": wake.reason,
                "set_at": now_cst().isoformat(),
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
    """定下次醒来：先写私有状态，再排一条 ``at`` 送达自己的消息。``at`` 必须带时区。

    ``reason`` 是那条消息的正文，醒来时原样摆到主 agent 眼前。排消息失败（``SendFailed``）
    往外抛，状态已经写下。
    """
    wake = NextWake(message_id=uuid.uuid4().hex, at=at, reason=reason)
    _write_next_wake(wake)
    await _schedule(wake)
    logger.info("world: next wake %s at %s", wake.message_id, at.isoformat())
    return wake


def is_stale_wake(message: Message) -> bool:
    """这是一条被后来定的时刻取代了的自定消息吗。只有自己发给自己的普通消息才可能是。"""
    if message.kind is not Kind.MESSAGE or message.sender != WORLD:
        return False
    current = read_next_wake()
    return current is None or current.message_id != message.message_id


async def wake_on_start() -> None:
    """进程启动、收件箱开设时：按私有状态决定要不要立刻醒一次。规则见模块说明。"""
    current = read_next_wake()
    now = now_cst()
    if current is None:
        logger.info("world: no wake set; waking now")
        await set_next_wake(now, "进程刚启动，没有找到你定下的下次醒来时刻。")
    elif current.at <= now:
        logger.info("world: wake %s at %s has passed; waking now", current.message_id, current.at)
        await set_next_wake(
            now,
            f"进程刚启动。你上一次定下的醒来时刻（{to_cst_full(current.at.isoformat())}）"
            f"已经过了，那一次的说明是：{current.reason}",
        )
    else:
        logger.info(
            "world: wake %s at %s is still ahead; scheduling it again",
            current.message_id,
            current.at,
        )
        await _schedule(current)
