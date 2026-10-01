"""一轮没跑完时已经发生、收不回来的事：报告出去的变化、出过场的 NPC，以及告知了谁。

一轮失败会整轮重来（:mod:`app.world.main_agent`）。它写过的记录留在盘上，重来时读得到；可告知
已经发给了居民、收不回来，而这一轮的上下文还没存下。重来的那一次不知道这些已经发生过，多半会
把同一个变化再报告一遍，居民就收到两份说法不同的告知——模型重跑写不出一字不差的同一段话，按
消息 id 去重挡不住这种重复。

所以报告变化、让 NPC 出场的结果一产生就记进私有卷上的 ``unfinished.json``（:func:`note`）；
下一轮开始时摆在主 agent 眼前（:func:`read`），这一轮的上下文存下之后清空（:func:`clear`）——
存下之后，上下文里已经有它们了。不管下一轮是同一条消息重来，还是那条消息重试用完进了死信
之后由别的什么叫醒，摆出来的都是真实发生过的事。怎么接着做由主 agent 判断，代码不替它决定。

只有 world 自己读写这个文件；写只有拿着卷的写锁的进程能做（:func:`app.world.volume.write_atomically`）。
读不出来按"没有"处理、记一条 error：宁可少看见几件事，也不让 world 停转。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.infra.cst_time import now_cst
from app.world.volume import lane_dir, require_writer_lock, write_atomically

logger = logging.getLogger(__name__)

_FILE = "unfinished.json"


@dataclass(frozen=True)
class Happened:
    """一件已经发生的事：什么时候、交回给主 agent 的那段结果。"""

    at: datetime
    what: str


def _path() -> Path:
    return lane_dir() / _FILE


def read() -> list[Happened]:
    """还没进上下文的那几件事，按发生的先后。"""
    path = _path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return [Happened(datetime.fromisoformat(e["at"]), str(e["what"])) for e in raw]
    except FileNotFoundError:
        return []
    except (ValueError, KeyError, TypeError):
        logger.error("world: %s is unreadable; treated as nothing left over", path, exc_info=True)
        return []


def note(what: str) -> None:
    """记下一件刚发生、收不回来的事。"""
    entries = [{"at": h.at.isoformat(), "what": h.what} for h in read()]
    entries.append({"at": now_cst().isoformat(), "what": what})
    directory = lane_dir()
    directory.mkdir(parents=True, exist_ok=True)
    write_atomically(_path(), json.dumps(entries, ensure_ascii=False))


def clear() -> None:
    """这一轮的上下文存下了：清空。"""
    require_writer_lock()
    _path().unlink(missing_ok=True)
