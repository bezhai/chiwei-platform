"""update_schedule tool — append schedule_revision + enqueue state_sync.

The new revision is persisted to schedule_revision (append-only history). A
``sync_life_state_after_schedule`` arq job is enqueued so the life-engine state
gets re-evaluated; the consumer itself lives in Plan D.
"""

from __future__ import annotations

import logging

from langchain.tools import tool
from langgraph.runtime import get_runtime

from app.agent.context import AgentContext
from app.agent.tools._common import tool_error
from app.data.ids import new_id
from app.data.queries import insert_schedule_revision
from app.data.session import get_session

logger = logging.getLogger(__name__)


async def enqueue_state_sync(*, revision_id: str) -> None:
    """Enqueue arq job ``sync_life_state_after_schedule`` (consumer in Plan D).

    Lazy-imports `arq` and `WorkerSettings` to avoid pulling the worker module's
    cron-import chain into tool-load time. TODO(Plan D): when multiple tools
    need to enqueue arq jobs, migrate to a shared pool in ``app/infra/arq_pool.py``
    instead of creating/closing per call.
    """
    from arq import create_pool

    from app.workers.arq_settings import WorkerSettings

    pool = await create_pool(WorkerSettings.redis_settings)
    try:
        await pool.enqueue_job(
            "sync_life_state_after_schedule",
            revision_id=revision_id,
            _queue_name=WorkerSettings.queue_name,
        )
    finally:
        await pool.close(close_connection_pool=True)


async def _update_schedule_impl(
    *, persona_id: str, content: str, reason: str, created_by: str,
) -> dict:
    content = (content or "").strip()
    reason = (reason or "").strip()
    if not content or not reason:
        return {"error": "content 和 reason 都不能为空"}

    rid = new_id("sr")
    async with get_session() as s:
        await insert_schedule_revision(
            s, id=rid, persona_id=persona_id,
            content=content, reason=reason, created_by=created_by,
        )

    try:
        await enqueue_state_sync(revision_id=rid)
    except Exception as e:
        # Don't roll back the revision — it's already committed. The Plan D
        # consumer will pick up unsynced revisions when it lands; until then
        # the next update_schedule call will re-enqueue.
        logger.warning("enqueue_state_sync failed for %s: %s", rid, e)

    return {"revision_id": rid, "schedule": content}


@tool
@tool_error("日程更新失败")
async def update_schedule(content: str, reason: str) -> dict:
    """更新你今天剩下的日程（覆盖式，你决定保留什么舍弃什么）。

    content 是一段自然语言，描述你当下状态 + 接下来要干嘛。稳定骨架和近期改动都
    写在一起。粒度自由，你觉得合适就行。reason 简短说一下为什么要改。

    调用后，state 会在后台重新评估（可能会立刻切状态或段内刷新）。

    Args:
        content: 新的日程段落
        reason: 本次更新的原因
    """
    context = get_runtime(AgentContext).context
    return await _update_schedule_impl(
        persona_id=context.persona_id,
        content=content, reason=reason,
        created_by="chiwei",
    )
