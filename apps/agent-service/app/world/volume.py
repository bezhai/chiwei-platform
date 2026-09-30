"""world 的私有卷：``$WORLD_DATA_DIR/<泳道>/`` 这个目录，以及往里写文件的唯一方式。

``WORLD_DATA_DIR`` 由部署给（world App 的 env），指向私有卷上 world 专用的那个根目录。
下面按进程的部署泳道分目录（prod 写成 ``prod``）：泳道那一段由代码拼，不让每条泳道各配
一个路径——忘了配的后果是静默写进别的泳道。路径本身不是世界内容。

目录里放两样东西：``records/``（记录，:mod:`app.world.records`）和私有状态文件
（:mod:`app.world.wake`）。人工读写接口只够得到 ``records/``。

**这里的读写全是同步的。** 文件都很小，卷在本机挂载；同步读写意味着"检查指纹"和
"写下去"之间没有 ``await``，同一个进程里的两个写者（主 agent 的工具、人工接口）不可能
在这两步之间插进来，也不存在"协程被取消了而线程还在写"的情况。world 只跑一个副本，
这个前提由部署保证。
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

from app.runtime.lane_policy import current_deployment_lane

DATA_DIR_ENV = "WORLD_DATA_DIR"


class VolumeUnavailable(RuntimeError):
    """``WORLD_DATA_DIR`` 没配置：world 没有地方放它的记录和状态。"""


def lane_dir() -> Path:
    """这条泳道在私有卷上的目录（不一定已经存在）。"""
    root = os.getenv(DATA_DIR_ENV)
    if not root:
        raise VolumeUnavailable(
            f"{DATA_DIR_ENV} is not configured; world has nowhere to keep its "
            f"records and state"
        )
    return Path(root) / (current_deployment_lane() or "prod")


def write_atomically(target: Path, text: str) -> None:
    """整份写下 ``target``：先写同目录下的临时文件，再原子地换过去。

    读的人要么看到旧的一整份，要么看到新的一整份；进程死在中途，留下的是一个以点开头的
    临时文件，列目录时不算数。
    """
    tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
