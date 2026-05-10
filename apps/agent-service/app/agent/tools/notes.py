"""Notes tool set — 赤尾自己的主动清单 (CRUD)。

Tools exposed:
- ``upsert_note`` (create / update content / update when_at / clear when_at)
- ``list_note``  (full active list, with when_label)
- ``resolve_note`` (mark as completed)
- ``delete_note`` (soft delete with mandatory reason)
"""

from __future__ import annotations

from datetime import UTC, datetime

from langchain.tools import tool
from langgraph.runtime import get_runtime

from app.agent.context import AgentContext
from app.agent.tools._common import tool_error
from app.data.queries import (
    delete_note as delete_note_query,
)
from app.data.queries import (
    list_active_notes as list_active_notes_query,
)
from app.data.queries import (
    resolve_note as resolve_note_query,
)
from app.data.queries import (
    upsert_note as upsert_note_query,
)
from app.data.queries.memory_edges import _UNSET
from app.domain.agent_tool_events import NoteCreated
from app.memory.notes_format import format_when_label
from app.runtime.db import emit_tx, tx


def _serialize(n) -> dict:
    return {
        "note_id": n.id,
        "content": n.content,
        "when_at": n.when_at.isoformat() if n.when_at else None,
        "created_at": n.created_at.isoformat() if n.created_at else None,
        "when_label": format_when_label(
            when_at=n.when_at,
            created_at=n.created_at,
            now=datetime.now(UTC),
        ),
    }


# ---------------------------------------------------------------------------
# upsert_note
# ---------------------------------------------------------------------------

async def _upsert_note_impl(
    *,
    persona_id: str,
    content: str,
    when_at_raw: str | None,
    note_id: str | None,
) -> dict:
    content = (content or "").strip()
    if not content:
        return {"error": "content 不能为空"}

    # Translate when_at_raw into the query-layer sentinel pattern:
    # - None       → _UNSET (don't touch column on update; default NULL on create)
    # - "clear"    → None   (explicit clear on update)
    # - ISO string → datetime
    if when_at_raw is None:
        when_at: datetime | None | object = _UNSET
    elif when_at_raw.strip().lower() == "clear":
        when_at = None
    else:
        try:
            when_at = datetime.fromisoformat(when_at_raw)
        except ValueError:
            return {"error": f"when_at 格式无效: {when_at_raw}"}

    is_create = note_id is None
    async with tx():
        try:
            row = await upsert_note_query(
                persona_id=persona_id,
                content=content,
                when_at=when_at,
                note_id=note_id,
            )
        except LookupError as e:
            return {"error": str(e)}
        if is_create:
            await emit_tx(NoteCreated(note_id=row.id, persona_id=persona_id))

    return {"id": row.id, "note": _serialize(row)}


# ---------------------------------------------------------------------------
# list_note
# ---------------------------------------------------------------------------

async def _list_note_impl(*, persona_id: str) -> dict:
    rows = await list_active_notes_query(persona_id=persona_id)
    return {"items": [_serialize(n) for n in rows]}


# ---------------------------------------------------------------------------
# resolve_note
# ---------------------------------------------------------------------------

async def _resolve_note_impl(
    *,
    persona_id: str,
    note_id: str,
    resolution: str,
) -> dict:
    resolution = (resolution or "").strip()
    if not note_id or not resolution:
        return {"error": "note_id 和 resolution 都不能为空"}

    await resolve_note_query(note_id=note_id, resolution=resolution)
    return {"ok": True}


# ---------------------------------------------------------------------------
# delete_note
# ---------------------------------------------------------------------------

async def _delete_note_impl(
    *,
    persona_id: str,
    note_id: str,
    reason: str,
) -> dict:
    reason = (reason or "").strip()
    if not note_id:
        return {"error": "note_id 不能为空"}
    if not reason:
        return {"error": "reason 不能为空，请说明为什么删"}

    await delete_note_query(note_id=note_id, reason=reason)
    return {"ok": True}


# ---------------------------------------------------------------------------
# @tool wrappers — exposed to the LLM
# ---------------------------------------------------------------------------

@tool
@tool_error("笔记保存失败")
async def upsert_note(
    content: str,
    when_at: str | None = None,
    note_id: str | None = None,
) -> dict:
    """把一件你觉得必须记住的事写进清单，或者更新已有的一条。

    这是你自己的清单，不是系统强加的承诺列表。只有你觉得"不能忘"、"需要专门记住"的
    事才写。

    什么时候用：
    - 第一次提到一件想记住的事 → 不传 note_id，会创建新的一条
    - 用户重复提到同一件事（比如"那家餐厅改成下周去了"）→ 传清单里看到的 note_id，会更新

    Args:
        content: 笔记内容（必填）。
        when_at: ISO 8601 时间戳（"2026-05-15T19:00:00+08:00"）。**如果这件事和某个时间相关
            （"明天"/"周五"/"下个月"/具体日子），强烈建议填**。没明确时间线索就别硬填。
            想清空已有的 when_at 传 "clear"（仅在更新场景有意义）。
        note_id: 已有 note 的 id（形如 "n_xxx"，从清单里看到）；不传则新建。
    """
    context = get_runtime(AgentContext).context
    return await _upsert_note_impl(
        persona_id=context.persona_id,
        content=content,
        when_at_raw=when_at,
        note_id=note_id,
    )


@tool
@tool_error("清单查询失败")
async def list_note() -> dict:
    """列出你目前的全部清单（没完成、没删除的）。

    什么时候用：
    - 用户问起"你都记了啥"
    - 你想盘点一下有没有重复的、可以合并的
    - 你想看看有没有挂了很久该处理的事

    注意：context 里通常已经有"最近活跃"的几条了。需要看全量、找特定 id、
    清盘的时候才用这个。

    Returns:
        ``{"items": [{"note_id": "n_xxx", "content": "...", "when_at": "...|null",
        "created_at": "...", "when_label": "还有 2 天"}, ...]}``
    """
    context = get_runtime(AgentContext).context
    return await _list_note_impl(persona_id=context.persona_id)


@tool
@tool_error("清单更新失败")
async def resolve_note(note_id: str, resolution: str) -> dict:
    """把一条已经完结的笔记划掉。

    比如电影看了、想法落实了。resolution 写一句话说明结果（"看完了"/"做完了"）。

    这是"完成"，不是"删除"。如果是改主意了 / 记错了 / 重复了，用 delete_note。

    Args:
        note_id: 笔记 id（形如 "n_xxxxxx"）
        resolution: 结果描述（必填）
    """
    context = get_runtime(AgentContext).context
    return await _resolve_note_impl(
        persona_id=context.persona_id,
        note_id=note_id,
        resolution=resolution,
    )


@tool
@tool_error("清单删除失败")
async def delete_note(note_id: str, reason: str) -> dict:
    """真删除一条清单项。

    和 resolve 不同 —— resolve 是"做完了"留个痕，delete 是"这条本来就不该存在"。

    什么时候用：
    - 改主意了，不打算做这件事了
    - 当时记错了，根本不是这件事
    - 发现是重复的（已经有一模一样的另一条）

    Args:
        note_id: 笔记 id（形如 "n_xxx"）
        reason: 必填，写明为什么删（"改主意了" / "记错了" / "和 n_xyz 重复"）
    """
    context = get_runtime(AgentContext).context
    return await _delete_note_impl(
        persona_id=context.persona_id,
        note_id=note_id,
        reason=reason,
    )
