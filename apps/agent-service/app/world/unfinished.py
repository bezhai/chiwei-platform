"""还没进主 agent 上下文的、已经发生的事：报告出去的变化、出过场的 NPC，以及判断要告知谁。

一轮失败会整轮重来（:mod:`app.world.main_agent`）。它写过的记录留在盘上，重来时读得到；可告知
一旦发给居民就收不回来，而这一轮的上下文还没存下。重来的那一次要是不知道，多半会把同一个变化
再报告一遍，居民就收到两份说法不同的告知——模型重跑写不出一字不差的同一段话，接收方按消息 id
去重挡不住这种重复。

所以一件事判断完、**发出任何一条告知之前**，先把它连同要发的告知（发给谁、发什么、预先定好的
消息 id）记进私有卷上的 ``unfinished.json``（:func:`note`），然后才按这些 id 去发
（:func:`app.world.perception.tell`）。下一轮开始、模型跑之前，先按原 id 把这里记着的告知全部
再发一遍，再把这些事摆到主 agent 眼前；这一轮的上下文存下之后清空（:func:`clear`）——存下之后，
上下文里已经有它们了。出事的位置不同，接下来是这样：

* 判断的时候：什么都没记、什么都没发，重来就是重来；
* 记下之后、发之前，或者两次发送之间（进程死了、被取消、发送出错）：下一轮按原 id 全部再发，
  已经收到的人按 id 去重，没收到的这次收到；
* 全发完之后、上下文存下之前：下一轮按原 id 再发一遍（都会被去重），主 agent 看得到它们；
* 上下文存下之后、清空之前：同上，主 agent 在上下文和这里各看到一次；
* 清空之后：没有要补的，上下文里有。

不管下一轮是同一条消息重来，还是那条消息重试用完进了死信之后由别的什么叫醒，补发的、摆出来的
都是真实发生过的事。怎么接着做由主 agent 判断，代码不替它决定。

只有 world 的主 agent 这一侧读写这个文件，应答 agent 不碰它。写只有拿着卷的写锁的进程能做，
走 :func:`app.world.volume.write_atomically`（同目录临时文件 + 原子替换），读的人看不到半份。

**读不出来的文件挪到旁边，不当成"没有"。** 文件里哪怕只有一条坏了（少了字段、不是 JSON、不是
UTF-8），整份原样改名成 ``unfinished.json.unreadable-<时刻>`` 留在同一个目录，记一条点出这个名字
的 error，然后按"没有要补的"接着跑：world 不会因为它一直停在每一轮的开头，后面的 :func:`note`
写的是一份新文件、:func:`clear` 删的也只是新文件，挪开的那份不会被覆盖或者删掉。里面记着的告知
这时不会补发，由人看过之后处理。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.infra.cst_time import now_cst
from app.world.perception import Notice
from app.world.volume import lane_dir, require_writer_lock, write_atomically

logger = logging.getLogger(__name__)

_FILE = "unfinished.json"


@dataclass(frozen=True)
class Happening:
    """一件已经发生的事：什么时候、发生了什么（给主 agent 看的那段话）、要告知谁。"""

    at: datetime
    what: str
    notices: tuple[Notice, ...]


def _path() -> Path:
    return lane_dir() / _FILE


def _from_json(raw: dict) -> Happening:
    return Happening(
        at=datetime.fromisoformat(raw["at"]),
        what=str(raw["what"]),
        notices=tuple(
            Notice(who=str(n["who"]), what=str(n["what"]), message_id=str(n["message_id"]))
            for n in raw["notices"]
        ),
    )


def _to_json(happening: Happening) -> dict:
    return {
        "at": happening.at.isoformat(),
        "what": happening.what,
        "notices": [
            {"who": n.who, "what": n.what, "message_id": n.message_id}
            for n in happening.notices
        ],
    }


def _set_aside(path: Path) -> None:
    """把读不出来的那份原样改名留在旁边，点名记一条 error。"""
    require_writer_lock()
    aside = path.with_name(f"{path.name}.unreadable-{now_cst():%Y%m%dT%H%M%S%f}")
    os.replace(path, aside)
    logger.error(
        "world: %s is unreadable; moved it to %s and went on as if nothing were left over. "
        "The notices kept in it were not resent: read it and handle them by hand",
        path,
        aside,
        exc_info=True,
    )


def read() -> list[Happening]:
    """还没进上下文的那几件事，按发生的先后。文件读不出来就挪到旁边（见模块说明），交回空的。"""
    path = _path()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return []
    try:
        return [_from_json(entry) for entry in json.loads(raw.decode("utf-8"))]
    except (ValueError, KeyError, TypeError):
        _set_aside(path)
        return []


def note(what: str, notices: list[Notice]) -> Happening:
    """记下一件刚发生的事和要发的告知。在发出其中任何一条之前调。"""
    happening = Happening(at=now_cst(), what=what, notices=tuple(notices))
    entries = [_to_json(h) for h in [*read(), happening]]
    lane_dir().mkdir(parents=True, exist_ok=True)
    write_atomically(_path(), json.dumps(entries, ensure_ascii=False))
    return happening


def clear() -> None:
    """这一轮的上下文存下了：清空。"""
    require_writer_lock()
    _path().unlink(missing_ok=True)
