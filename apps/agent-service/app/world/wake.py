"""world 什么时候醒：只因两种原因——收件箱来了消息，或者它自己定的时刻到了。

没有心跳，也没有默认间隔。每一轮最后由主 agent 定下次醒来的时刻（:mod:`app.world.main_agent`），
这里负责把它变成现实。

**私有状态**（``$WORLD_DATA_DIR/<泳道>/next_wake.json``）记两样：

* **当前**：已经排出去的那次醒来——哪一条自定消息（id）、什么时刻、为什么；
* **待定**：正在排、还不知道排没排出去的那一次。

只有 world 自己读写这个文件；人工读写接口只够得到 ``records/``。

**定下次醒来分三步**（:func:`set_next_wake`）：① 把新时刻 B 作为"待定"写进状态（"当前"不动）；
② 用通信机制排一条 B 时刻送达自己的消息（``send_at``），消息 id 就是 B 的 id，正文就是那段
说明；③ 把 B 升为"当前"，清掉"待定"。

**一条自定消息，只要是状态里的"当前"或"待定"，就不是旧消息**（:func:`is_stale_wake`）；
两者都不是，就是被后来定的时刻取代了，跳过、不跑一轮。别人发来的消息、机制发回来的
"没有送达"告知不受这条影响。这一条让三步之间任何一处失败或进程死掉都不会让它停转：

* 死在 ① 之前或 ① 失败：状态没变，叫醒这一轮的那条消息（它是"当前"）重投时照常跑。
* ② 失败（broker 没收到，或者超时取消落在等确认时）："当前"还是叫醒这一轮的那条，重投时
  照常跑，重跑那一轮会用新的"待定"把 B 顶掉。broker 其实收到了 B 的话，B 先到时它是
  "待定"，照常跑。
* ③ 失败：B 已经排出去，它是"待定"，到点照常跑；叫醒这一轮的那条仍是"当前"，重投时也照常
  跑。两边谁先跑完谁定下一个时刻，另一条随之成为旧消息。
* 进程死在 ① 之后、③ 之前：重启时（:func:`wake_on_start`）看到"待定"，按它原来的 id、原来的
  时刻再排一次并升为"当前"。它要是其实已经排出去了，两条同 id 的消息只会被处理一次（通信
  机制按 id 去重）；叫醒那一轮的旧消息重投时就是旧消息了——那一轮在定时刻之前已经把上下文
  存下，它的决定就是 B。

**进程启动时按状态补醒**（:func:`wake_on_start`，挂在收件箱开设时）：有"待定"就按上面补完；
没有"当前"（第一次启动、状态文件读不出来）或者"当前"的时刻已经过了，立刻醒一次；"当前"
还没到，按原 id、原时刻再排一次（同样靠按 id 去重，不会醒两次）。

**一轮最终进了死信**（:func:`wake_after_failure`，挂在收件箱的 ``on_final_failure`` 上）：叫醒
那一轮的是状态里还算数的自定消息（"当前"或"待定"）时，它进死信之后就再没有别的东西会叫醒
world。所以这时给自己排一次"退避时长之后再醒"（Dynamic Config :data:`WAKE_BACKOFF_MINUTES_KEY`，
默认 :data:`DEFAULT_WAKE_BACKOFF_MINUTES` 分钟），照常走上面三步。这不是心跳：只在自定唤醒
最终失败时触发一次；退避之后那一轮又失败，就再退避一次。别人发来的消息、已经被取代的自定
消息最终失败，不做任何事——状态里原定的下次醒来还在，world 本来就会按时醒。
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
from app.world.volume import lane_dir, write_atomically

logger = logging.getLogger(__name__)

# world 在通信机制里的名字：它的收件箱，也是它给自己排消息时的发送方和接收方。
WORLD = "world"

_STATE_FILE = "next_wake.json"

# Dynamic Config：自定唤醒那一轮最终失败之后，隔多久再醒一次。改它不用重新部署。
WAKE_BACKOFF_MINUTES_KEY = "world_wake_backoff_minutes"
DEFAULT_WAKE_BACKOFF_MINUTES = 60


@dataclass(frozen=True)
class NextWake:
    """定下的一次醒来：哪一条自定消息、什么时刻、为什么（这段话也是那条消息的正文）。"""

    message_id: str
    at: datetime
    reason: str


@dataclass(frozen=True)
class WakeState:
    current: NextWake | None = None
    pending: NextWake | None = None


def _wake_to_json(wake: NextWake | None) -> dict | None:
    if wake is None:
        return None
    return {"message_id": wake.message_id, "at": wake.at.isoformat(), "reason": wake.reason}


def _wake_from_json(raw: dict | None) -> NextWake | None:
    if raw is None:
        return None
    return NextWake(
        message_id=str(raw["message_id"]),
        at=datetime.fromisoformat(raw["at"]),
        reason=str(raw["reason"]),
    )


def read_state() -> WakeState:
    """私有状态；没有、或者读不出来都当作什么都没记（启动时按"没有"补醒）。"""
    path = lane_dir() / _STATE_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return WakeState(
            current=_wake_from_json(raw.get("current")),
            pending=_wake_from_json(raw.get("pending")),
        )
    except FileNotFoundError:
        return WakeState()
    except (ValueError, KeyError, TypeError, AttributeError):
        logger.error("world: %s is unreadable; treated as no wake set", path, exc_info=True)
        return WakeState()


def read_next_wake() -> NextWake | None:
    """状态里的"当前"：已经排出去的那次醒来。"""
    return read_state().current


def _write_state(state: WakeState) -> None:
    directory = lane_dir()
    directory.mkdir(parents=True, exist_ok=True)
    write_atomically(
        directory / _STATE_FILE,
        json.dumps(
            {
                "current": _wake_to_json(state.current),
                "pending": _wake_to_json(state.pending),
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


async def _schedule_and_promote(wake: NextWake) -> None:
    """② 排出去，③ 升为"当前"。"""
    await _schedule(wake)
    _write_state(WakeState(current=wake))
    logger.info("world: next wake %s at %s", wake.message_id, wake.at.isoformat())


async def set_next_wake(at: datetime, reason: str) -> NextWake:
    """定下次醒来（三步见模块说明）。``at`` 必须带时区；``reason`` 是那条消息的正文。

    哪一步失败都往外抛；已经写下的"待定"留着，重投或重启时由上面的规则接手。
    """
    wake = NextWake(message_id=uuid.uuid4().hex, at=at, reason=reason)
    _write_state(WakeState(current=read_state().current, pending=wake))
    await _schedule_and_promote(wake)
    return wake


def is_stale_wake(message: Message) -> bool:
    """这是一条被后来定的时刻取代了的自定消息吗。只有自己发给自己的普通消息才可能是。"""
    if message.kind is not Kind.MESSAGE or message.sender != WORLD:
        return False
    state = read_state()
    live = {w.message_id for w in (state.current, state.pending) if w is not None}
    return message.message_id not in live


async def wake_on_start() -> None:
    """进程启动、收件箱开设时：按私有状态补完没做完的一次定时刻，或者决定要不要立刻醒。"""
    state = read_state()
    now = now_cst()
    if state.pending is not None:
        logger.info(
            "world: wake %s was left pending; scheduling it again and making it current",
            state.pending.message_id,
        )
        await _schedule_and_promote(state.pending)
    elif state.current is None:
        logger.info("world: no wake set; waking now")
        await set_next_wake(now, "进程刚启动，没有找到你定下的下次醒来时刻。")
    elif state.current.at <= now:
        logger.info(
            "world: wake %s at %s has passed; waking now",
            state.current.message_id,
            state.current.at,
        )
        await set_next_wake(
            now,
            f"进程刚启动。你上一次定下的醒来时刻（{to_cst_full(state.current.at.isoformat())}）"
            f"已经过了，那一次的说明是：{state.current.reason}",
        )
    else:
        logger.info(
            "world: wake %s at %s is still ahead; scheduling it again",
            state.current.message_id,
            state.current.at,
        )
        await _schedule(state.current)


async def _backoff_minutes() -> int:
    minutes = await asyncio.to_thread(
        dynamic_config.get_int,
        WAKE_BACKOFF_MINUTES_KEY,
        default=DEFAULT_WAKE_BACKOFF_MINUTES,
    )
    if minutes <= 0:
        logger.warning(
            "dynamic config %s = %r is not a positive integer; using %d",
            WAKE_BACKOFF_MINUTES_KEY,
            minutes,
            DEFAULT_WAKE_BACKOFF_MINUTES,
        )
        return DEFAULT_WAKE_BACKOFF_MINUTES
    return minutes


async def wake_after_failure(message: Message, error: BaseException) -> None:
    """收件箱里一条消息最终处理失败、即将进死信时：还算数的自定唤醒失败了，就退避之后再醒。"""
    if message.kind is not Kind.MESSAGE or message.sender != WORLD:
        return
    if is_stale_wake(message):
        return
    minutes = await _backoff_minutes()
    at = now_cst() + timedelta(minutes=minutes)
    chosen = await set_next_wake(
        at,
        f"上一次醒来（排在 {to_cst_full(message.time.isoformat())}）的那一轮重试几次都没能"
        f"跑完，最后一次的错误是：{type(error).__name__}: {error}。隔了 {minutes} 分钟，"
        f"再醒一次。",
    )
    logger.warning(
        "world: wake %s failed for good; backing off to wake %s at %s",
        message.message_id,
        chosen.message_id,
        at.isoformat(),
    )
