"""Admin / public-API request Data classes — for HTTP source RPC endpoints.

Each Data wraps one HTTP endpoint's input; all transient (no DB row); wired
via Source.http(...) with response=True.

旧 life-tick / glimpse / schedule 触发 + schedule CRUD 的 request Data 已随
world/life 重写删除；voice 触发随 voice 子系统拆除删除。剩 search。
"""
from __future__ import annotations

from typing import Annotated

from app.runtime import Data, Key


class AdminSearchRequest(Data):
    # Data 要求至少一个 Key，AdminSearch 实际不去重（transient）；选 num 仅为
    # 满足约束（int 可序列化进 dedup hash）。queries 单独保留为 list 字段。
    queries: list[str]
    num: Annotated[int, Key] = 5

    class Meta:
        transient = True
