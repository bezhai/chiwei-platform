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
* ``handled`` —— 处理完的消息 id，和处理完的时刻。

**放弃。** 一条消息经过的失败轮数到了上限（调用方给，见 :mod:`app.world.rounds`），就从
``pending`` 里拿掉，之后的轮不再带它：不然一条每次都让一轮跑不完的消息会留在每一轮里，world
再也跑不完一轮。放弃只是拿掉，不是结果：它之后再有投递（死信重放、通信机制还没用完的重试）
就照新的一条收下，失败次数从零数起；通信机制给一次投递的处理次数有限，所以这样来回也有尽头。
放弃时记一条 error，末尾是整条消息（id、类型、发送方、时间、要不要叫醒、全文）的 JSON：
不叫醒的消息送到时就确认掉了，没有死信，这条日志是照着重做的唯一依据。world 给自己排的醒来
不放弃：``pending`` 里的自定醒来总是状态里的最新唤醒（旧的在每一轮开头就挪掉了，:func:`drop`），
拿掉了它就再没有东西会叫醒 world。

**处理完的只留 :data:`KEPT_FOR`。** 它们只用来挡住迟到的投递，迟到能迟多久见那里的说明；
每次写都把更早的删掉，文件不会越写越长。

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
from datetime import datetime, timedelta
from pathlib import Path

from app.infra.cst_time import now_cst
from app.messaging.message import Message
from app.world.volume import lane_dir, set_aside, write_atomically
from app.world.wake import is_own_wake

logger = logging.getLogger(__name__)

_FILE = "pending.json"

# 处理完的消息 id 留多久。一条消息的投递在它被处理完之后还能再来：通信机制的重试（退避
# 封顶 10 分钟）、处理它的那次投递的占位租约过期之后被重投（world 的租约一个多小时，见
# :data:`app.world.rounds.Rounds.delivery_timeout`）、进程死了 broker 在下一个进程起来时重投。
# 一天比这些都长得多；文件里一天大约几百个 id，每次整份重写也不贵。
KEPT_FOR = timedelta(days=1)


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
    cutoff = now_cst() - KEPT_FOR
    state["handled"] = {
        message_id: at
        for message_id, at in state["handled"].items()
        if datetime.fromisoformat(at) >= cutoff
    }
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


def handled(message_ids: Iterable[str]) -> None:
    """这几条经过的那一轮跑完了：记成处理完。"""
    ids = set(message_ids)
    state = _load()
    at = now_cst().isoformat()
    state["pending"] = [e for e in state["pending"] if e["message"]["message_id"] not in ids]
    state["handled"].update(dict.fromkeys(ids, at))
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
            "takes it unless it is delivered again (a dead-letter replay, a retry). To redo it "
            "by hand, send it again as it was; message: %s",
            message.message_id,
            give_up_at,
            json.dumps(message.to_json(), ensure_ascii=False),
        )
    return gave_up
