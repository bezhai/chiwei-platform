"""Admin / public API @nodes.

Each node corresponds to one HTTP endpoint; wires in app/wiring/admin.py.
Return types are left un-annotated so the @node decorator skips Data-only
validation — these nodes return dict / list[dict] for sync HTTP RPC.

旧 life-tick / glimpse / schedule 触发 + schedule CRUD 的 admin 节点已随
world/life 重写删除（那套活法不再存在）。剩 voice 触发 + search。
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from app.domain.admin import (
    AdminSearchRequest,
    AdminVoiceRequest,
)
from app.runtime import node


@node
async def admin_trigger_voice_node(r: AdminVoiceRequest):
    from app.memory.voice import generate_voice

    result = await generate_voice(r.persona_id, source="manual")
    return {
        "ok": True,
        "persona_id": r.persona_id,
        "result": result[:200] if result else None,
    }


@node
async def admin_search_node(r: AdminSearchRequest):
    from app.agent.tools.search import _you_search
    from app.infra.config import settings

    if not settings.you_search_host:
        raise HTTPException(503, "You Search API not configured")
    results: dict[str, Any] = {}
    for query in r.queries:
        try:
            hits = await _you_search(query, r.num, "CN", "ZH-HANS")
            results[query] = hits
        except Exception as e:
            results[query] = {"error": str(e)}
    return results
