"""知识来源"记录"：world 的记录里写着什么。只有读的那一半：列目录、读一份。

记录（:mod:`app.world.records`）是 world 自己对这个世界的认识。写记录只有主 agent 能做
（:func:`app.world.actions.write_record`），不在这里。

**读到的指纹记在这一次调用里。** 主 agent 改写一份已有的记录之前，必须在这一轮里读过它现在的
样子（指纹规矩见 :mod:`app.world.records`）：:func:`read_record` 把读到的指纹记进这一次 agent
调用的 :data:`RECORDS_READ`（放在 ``AgentContext.features`` 里的一个"路径 → 指纹"字典），写的
时候拿它做指纹检查。只有主 agent 的一轮放这个字典；感知判断、NPC、应答不写记录，不放，读了
也不记。
"""
from __future__ import annotations

from typing import Annotated

from pydantic import Field

from app.agent.runtime_context import get_context
from app.agent.tooling import tool
from app.agent.tools._common import tool_error
from app.capabilities._errors import CapabilityInvalidArg, CapabilityNotFound
from app.world import records
from app.world.agents import when
from app.world.sources import Source

# ``AgentContext.features`` 里那个"这一次调用读过的记录 → 读到时的指纹"字典的键。
RECORDS_READ = "world_records_read"

RecordPath = Annotated[
    str, Field(description="记录的路径，相对记录根目录，形如 目录/名字.md")
]


@tool
@tool_error("列记录失败")
async def list_records() -> str:
    """列出世界的全部记录：每一份的路径、字数、最后改动的时间。

    记录是世界对自己的认识：这个世界坐落在哪个真实的地方，有哪些地方、那里什么样，有哪些人
    和机构，什么东西现在处在什么状态。
    """
    entries = records.listing()
    if not entries:
        return "还没有任何记录。"
    lines = [f"世界的记录（{len(entries)} 份）："]
    lines += [
        f"- {e.path}（{e.chars} 字，最后改动 {when(e.updated_at)}）" for e in entries
    ]
    return "\n".join(lines)


@tool
@tool_error("读记录失败")
async def read_record(path: RecordPath) -> str:
    """读世界的一份记录的全文。"""
    try:
        record = records.read(path)
    except records.RecordNotFound as exc:
        raise CapabilityNotFound(str(exc)) from exc
    except records.InvalidRecordPath as exc:
        raise CapabilityInvalidArg(str(exc)) from exc
    reads = get_context().features.get(RECORDS_READ)
    if reads is not None:
        reads[record.path] = record.fingerprint
    return (
        f"《{record.path}》（{len(record.text)} 字，最后改动 {when(record.updated_at)}）"
        f"\n\n{record.text}"
    )


SOURCE = Source(name="records", tools=(list_records, read_record))
