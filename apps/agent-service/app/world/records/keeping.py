"""插件 ``record_keeping``：world 的记录怎么写下来、主 agent 眼前有哪些记录、人怎么读写它们。

* :func:`write_record` —— 主 agent 整份写下一份记录（:mod:`app.world.records`）。没有删除：一样东西
  没了，改写那份记录说它没了。人工接口可以删。它的返回是它自己做过的事，跟着它自己的话一起留
  （``trim="kept"``）。
* 状态段 ``records.listing``（:func:`records_listing`）—— 它有哪些记录，只写进上下文清理的那条
  标记消息（:mod:`app.agent.continuity`；每轮都一样的东西不每轮重发）。
* prompt 的 ``world_round/write-record`` 那一段：写记录的规矩。
* **记录的人工读写接口**（:mod:`app.world.records.admin`）：四条路由都要内网凭据；请求要去的泳道
  不是这个进程的泳道，就一步都不做；回答都带上这个进程的泳道。

读记录的两个查询工具在插件 ``records``（:mod:`app.world.records.reading`）。开关（``world_sources``）
能关掉的是那一个：关掉它，主 agent 和另外三类 agent 都查不到记录，可这里的写记录、记录目录和人工
接口照旧在。

**改写一份已有的记录，必须在这一轮里读过它现在的样子。** 模型不用自己搬指纹：读记录的工具
（:func:`app.world.records.reading.read_record`）把读到的指纹记进这一次调用的
:class:`app.world.records.reading.RecordsRead`，写的时候拿它去做 :func:`app.world.records.write`
的指纹检查。它读过之后别人（人工接口）又改过，这次写就不落盘，告诉它重新读。
"""
from __future__ import annotations

import logging
from typing import Annotated

from pydantic import Field

from app.agent.round import Round, current_call, current_round
from app.agent.tooling import tool
from app.agent.tools._common import tool_error
from app.capabilities._errors import CapabilityInvalidArg
from app.host import Context, Plugin
from app.world import records
from app.world.agents import world_segments
from app.world.records.admin import (
    RecordDeleteRequest,
    RecordListRequest,
    RecordReadRequest,
    RecordWriteRequest,
    record_delete_node,
    record_listing_node,
    record_read_node,
    record_write_node,
)
from app.world.records.reading import RecordPath, RecordsRead

logger = logging.getLogger(__name__)

# 主 agent 的 prompt 里写记录的那一段（Langfuse id）。
WRITE_RECORD_SEGMENT = "world_round/write-record"

_RECORDS = "/admin/world/records"
_DOCUMENT = f"{_RECORDS}/document"

ROUTES = (
    ("GET", _RECORDS, RecordListRequest, record_listing_node),
    ("GET", _DOCUMENT, RecordReadRequest, record_read_node),
    ("PUT", _DOCUMENT, RecordWriteRequest, record_write_node),
    ("DELETE", _DOCUMENT, RecordDeleteRequest, record_delete_node),
)


@tool
@tool_error("写记录失败")
async def write_record(
    path: RecordPath,
    text: Annotated[str, Field(description="这一份记录的全文，整份替换原来的")],
) -> str:
    """整份写下一份记录：没有就新建，有就整份替换。

    改写一份已经有的记录之前，要在这一轮里读过它现在的样子。
    """
    reads = current_call().state(RecordsRead).fingerprints
    try:
        written = records.write(path, text, expected=reads.get(path))
    except (records.InvalidRecordPath, records.InvalidRecordText) as exc:
        raise CapabilityInvalidArg(str(exc)) from exc
    except records.RecordConflict:
        if path in reads:
            return (
                f"没有写：「{path}」在你读过之后被改过了。重新读一遍（read_record）"
                f"再决定怎么写。"
            )
        return (
            f"没有写：「{path}」已经有了，你这一轮还没读过它现在的样子。先读一遍"
            f"（read_record）再写。"
        )
    reads[written.path] = written.fingerprint
    logger.info("world: round %s wrote record %s", current_round().round_id, written.path)
    return f"写好了：「{written.path}」（{len(written.text)} 字）。"


async def records_listing(round: Round) -> str:
    """状态段（``records.listing``）：它有哪些记录。只写进上下文清理的那条标记消息。"""
    entries = records.listing()
    if not entries:
        return "你还没有任何记录。"
    return "\n".join(
        [f"你的记录（{len(entries)} 份）："]
        + [f"- {e.path}（{e.chars} 字）" for e in entries]
    )


def setup(ctx: Context) -> None:
    for segment in world_segments(WRITE_RECORD_SEGMENT).values():
        ctx.segment(segment)
    ctx.tool(write_record, trim="kept")
    ctx.section("records.listing", records_listing)
    for method, path, request, handler in ROUTES:
        ctx.route(
            method,
            path,
            request,
            handler,
            inner_secret=True,
            lane_match=True,
            answers_with_lane=True,
        )


PLUGIN = Plugin(name="record_keeping", setup=setup)
