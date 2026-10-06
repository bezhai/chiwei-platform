"""world 收件箱里的消息走到了哪一步：还没经过一轮，还是已经处理完了。

world 一轮处理收件箱里所有还没经过一轮的消息（:mod:`app.world.rounds`）。哪些还没经过、哪些已经
处理完，记在私有卷上的 ``pending.json``，不放在进程里，因为这两件事都跨得过一次投递、一个进程：

* 不叫醒 world 的消息不单独起一轮，送到就收下了，由之后不知道哪一轮来处理，那时进程可能已经
  换过；
* 一轮失败、进程中途退出，这一轮带着的消息都要出现在之后的一轮里，不管它们各自的投递这时是在
  重试、还没重投，还是已经确认掉了；
* 一条消息已经被某一轮处理完，它的投递再来时（通信机制的重试、broker 在进程死后重投、发件方
  拿着原 id 重发）不再起一轮，换过进程也一样。

文件里两样：

* ``pending`` —— 还没经过一轮的消息，按到达的先后，每条带着它经过了几次失败的轮；
* ``handled`` —— 处理完了、但通信机制还没告诉我们它的投递成功已经记下来的消息 id，和处理完的时刻。

**放弃。** 一条消息经过的失败轮数到了上限（调用方给，见 :mod:`app.world.rounds`），就从
``pending`` 里拿掉，之后的轮不再带它：不然一条每次都让一轮跑不完的消息会留在每一轮里，world
再也跑不完一轮。放弃只是拿掉，不是结果：它之后再有投递（死信重放、通信机制还没用完的重试）
就照新的一条收下，失败次数从零数起；通信机制给一次投递的处理次数有限，所以这样来回也有尽头。
放弃时记一条 error，末尾是整条消息（id、类型、发送方、时间、要不要叫醒、全文）的 JSON，前面
写着怎么重做：叫醒 world 的，它自己的投递还在重试或者进了死信，重放死信就回来；不叫醒的送到时
就处理成功了，没有死信，这条日志是照着重做的唯一依据，而且原来的 id 在通信机制那里已经记成
处理成功，按原 id 重发会被挡掉，要换一个新的消息 id 把正文再发一遍。world 给自己排的醒来
不放弃：``pending`` 里的自定醒来总是状态里的最新唤醒（旧的在每一轮开头就挪掉了，:func:`drop`），
拿掉了它就再没有东西会叫醒 world。

**处理完的记录留到通信机制把它的投递成功记下来为止，不按时间删。** 通信机制按消息 id 去重：一次
投递处理成功、成功记下来之后（``runtime_inflight`` 里的 ``succeeded``，不会被删），同一条消息之后
再来的投递在交给处理函数之前就被挡掉。可成功是在处理函数**返回之后**才记的：返回和记下之间进程
死了，或者记的那一笔没写成，这次投递放回去，占位租约过期后同一条还会交到处理函数手里。那时
world 要认得它已经处理完了，不然整轮重跑。所以：

* 每条被一轮处理完的消息都记下（:func:`handled`），叫醒的、不叫醒的都记——不叫醒的那次投递在
  收下时就返回了，它的成功同样可能没记下来；
* 什么时候删，只看通信机制记下了没有：每一轮开始时，拿着锁，把记着的 id 交给通信机制问一次
  （:func:`app.messaging.receiving.succeeded_message_ids`），成功已经记下来的那些删掉
  （:func:`forget`）。问不到（库不通）就都留着，下一轮再问。处理函数返回不算数。

**文件有多大。** 记着的，是"处理完了、但最近一轮开始时通信机制还没记下成功"的消息：

* 最近那一轮自己的消息：等它的投递在这一轮之后才返回、才记成功，下一轮开始时删——最多一轮的量；
* 投递还在通信机制里没有定论的：处理失败在重试的，进程死了等着重投的，成功没记下来、等租约过期
  被接管的，进了死信的。前几样过一阵自己就有了定论，之后的第一轮删掉；只有一直没人重放的死信，
  记录才一直留着，一条死信一条。

world 闲着、一直没有新的一轮时，最近那一轮的记录留到下一轮开始。

只有 world 的主 agent 这一侧读写这个文件。写只有拿着卷的写锁的进程能做，每次都是读出整份、改、
再整份写回去（:func:`app.world.volume.write_atomically`），中间没有 ``await``，同一个进程里的
几次投递插不进来。**读不出来的文件挪到旁边**（:func:`app.world.volume.set_aside`），记一条点出
新名字的 error，按"什么都没有"接着跑：里面还没经过一轮的消息这时不会被带进之后的轮，由人看过
之后处理。
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from app.infra.cst_time import now_cst
from app.messaging.message import Message
from app.world.volume import lane_dir, set_aside, write_atomically
from app.world.wake import is_own_wake

logger = logging.getLogger(__name__)

_FILE = "pending.json"

def _path() -> Path:
    return lane_dir() / _FILE


def _empty() -> dict:
    return {"pending": [], "handled": {}}


def _load() -> dict:
    path = _path()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return _empty()
    try:
        state = json.loads(raw.decode("utf-8"))
        for entry in state["pending"]:
            Message.from_json(entry["message"])
            int(entry["failed_rounds"])
        for at in state["handled"].values():
            datetime.fromisoformat(at)
        return state
    except (ValueError, KeyError, TypeError, AttributeError):
        aside = set_aside(path)
        logger.error(
            "world: %s is unreadable; moved it to %s and went on as if it were empty. The "
            "messages waiting in it were not taken into a round: read it and handle them by hand",
            path,
            aside,
            exc_info=True,
        )
        return _empty()


def _save(state: dict) -> None:
    lane_dir().mkdir(parents=True, exist_ok=True)
    write_atomically(_path(), json.dumps(state, ensure_ascii=False))


def is_handled(message_id: str) -> bool:
    """这条消息已经被某一轮处理完了吗。"""
    return message_id in _load()["handled"]


def add(message: Message) -> None:
    """收下一条还没经过一轮的消息，排在最后；已经收下了就什么都不变。已经处理完的
    （:func:`is_handled`）不该再收，由调用方先问。"""
    state = _load()
    if any(e["message"]["message_id"] == message.message_id for e in state["pending"]):
        return
    state["pending"].append({"message": message.to_json(), "failed_rounds": 0})
    _save(state)


def read() -> list[Message]:
    """还没经过一轮的消息，按到达的先后。"""
    return [Message.from_json(e["message"]) for e in _load()["pending"]]


def drop(message_ids: Iterable[str]) -> None:
    """不再等一轮的消息（被后来定的时刻取代了的自定醒来）：从还没经过一轮的里拿掉，不记结果。"""
    ids = set(message_ids)
    state = _load()
    state["pending"] = [e for e in state["pending"] if e["message"]["message_id"] not in ids]
    _save(state)


def handled(messages: Iterable[Message]) -> None:
    """这几条经过的那一轮跑完了：从还没经过一轮的里拿掉，记成处理完。"""
    taken = list(messages)
    ids = {m.message_id for m in taken}
    state = _load()
    at = now_cst().isoformat()
    state["pending"] = [e for e in state["pending"] if e["message"]["message_id"] not in ids]
    state["handled"].update(dict.fromkeys(ids, at))
    _save(state)


def recorded() -> list[str]:
    """记成处理完、还没删掉的那些消息 id。"""
    return list(_load()["handled"])


def forget(message_ids: Iterable[str]) -> None:
    """这几条的投递成功通信机制已经记下来了，同一条再也到不了处理函数：删掉它们的记录。"""
    ids = set(message_ids)
    if not ids:
        return
    state = _load()
    kept = {m: at for m, at in state["handled"].items() if m not in ids}
    if len(kept) != len(state["handled"]):
        state["handled"] = kept
        _save(state)


def failed(message_ids: Iterable[str], *, give_up_at: int) -> list[Message]:
    """这几条经过的那一轮没跑完：各记一次失败。失败到了 ``give_up_at`` 次的放弃——从还没经过
    一轮的里拿掉（world 自己排的醒来除外），交回放弃了的那几条。"""
    ids = set(message_ids)
    state = _load()
    kept, gave_up = [], []
    for entry in state["pending"]:
        message = Message.from_json(entry["message"])
        if message.message_id in ids:
            entry["failed_rounds"] += 1
            if entry["failed_rounds"] >= give_up_at and not is_own_wake(message):
                gave_up.append(message)
                continue
        kept.append(entry)
    state["pending"] = kept
    _save(state)
    for message in gave_up:
        logger.error(
            "world: message %s was in %d rounds that did not finish; given up, no later round "
            "takes it unless it is delivered again. %s; message: %s",
            message.message_id,
            give_up_at,
            _how_to_redo(message),
            json.dumps(message.to_json(), ensure_ascii=False),
        )
    return gave_up


def _how_to_redo(message: Message) -> str:
    """放弃了的这条怎么照着重做。

    叫醒 world 的：它自己的投递还在重试，或者进了死信，重放死信就重新收下。不叫醒的：它那次投递
    送到时就处理成功了，没有死信；原来的 id 在通信机制那里已经记成处理成功，按原 id 再发会在交到
    world 之前被挡掉，所以要换一个新的消息 id 把正文再发一遍。
    """
    if message.wakes_recipient:
        return (
            "Its own delivery is still being retried or is in the dead-letter queue: replaying "
            "its dead letter takes it in again"
        )
    return (
        "It did not wake world, so its delivery was acknowledged on arrival: there is no dead "
        "letter, and messaging drops anything sent again under its original id. To redo it by "
        "hand, send its body again under a new message id"
    )
