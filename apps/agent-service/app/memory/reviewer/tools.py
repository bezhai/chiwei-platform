"""Reviewer-only tools — called by the memory reviewer agent to mutate graph state.

Not bound to the chat agent; only bound at reviewer agent invocation time.
"""

from __future__ import annotations

import logging

from langchain.tools import tool

from app.data.ids import new_id
from app.data.queries import (
    delete_edge,
    delete_fragment_query,
    get_abstract_by_id,
    get_fragment_by_id,
    insert_memory_edge,
    set_clarity,
    touch_abstract,
    touch_fragment,
    update_abstract_content_query,
)
from app.data.session import get_session

logger = logging.getLogger(__name__)


@tool
async def update_abstract_content(abstract_id: str, new_content: str, reason: str) -> dict:
    """Rewrite the content of an existing abstract (演化 / 合并)."""
    async with get_session() as s:
        await update_abstract_content_query(s, abstract_id=abstract_id, new_content=new_content)
    logger.info("reviewer update_abstract %s: %s", abstract_id, reason)
    return {"ok": True}


@tool
async def fade_node(node_id: str, node_type: str, clarity: str, reason: str) -> dict:
    """Set clarity: 'clear' / 'vague' / 'forgotten'. node_type: 'abstract' | 'fact'."""
    if clarity not in ("clear", "vague", "forgotten"):
        return {"ok": False, "error": f"invalid clarity {clarity}"}
    async with get_session() as s:
        await set_clarity(s, node_id=node_id, node_type=node_type, clarity=clarity)
    logger.info("reviewer fade %s (%s) -> %s: %s", node_id, node_type, clarity, reason)
    return {"ok": True}


@tool
async def touch_node(node_id: str, node_type: str) -> dict:
    """Strengthen a node (update last_touched_at)."""
    async with get_session() as s:
        if node_type == "abstract":
            await touch_abstract(s, node_id)
        elif node_type == "fact":
            await touch_fragment(s, node_id)
        else:
            return {"ok": False, "error": f"unknown node_type {node_type}"}
    return {"ok": True}


@tool
async def delete_fragment(fragment_id: str, reason: str) -> dict:
    """Permanently remove a fragment (trivial-only; for abstract use fade_node→forgotten)."""
    async with get_session() as s:
        await delete_fragment_query(s, fragment_id=fragment_id)
    logger.info("reviewer delete_fragment %s: %s", fragment_id, reason)
    return {"ok": True}


@tool
async def connect(
    from_id: str, from_type: str, to_id: str, to_type: str,
    edge_type: str, reason: str,
) -> dict:
    """Create an edge. edge_type ∈ {'supports','parent_of','related_to','conflicts_with'}."""
    if edge_type not in ("supports", "parent_of", "related_to", "conflicts_with"):
        return {"ok": False, "error": f"invalid edge_type {edge_type}"}
    async with get_session() as s:
        if from_type == "abstract":
            n = await get_abstract_by_id(s, from_id)
        else:
            n = await get_fragment_by_id(s, from_id)
        if n is None:
            return {"ok": False, "error": f"from node {from_id} not found"}
        persona_id = n.persona_id
        await insert_memory_edge(
            s, id=new_id("e"), persona_id=persona_id,
            from_id=from_id, from_type=from_type,
            to_id=to_id, to_type=to_type,
            edge_type=edge_type, created_by="reviewer", reason=reason,
        )
    return {"ok": True}


@tool
async def disconnect(edge_id: str, reason: str) -> dict:
    """Remove an edge."""
    async with get_session() as s:
        await delete_edge(s, edge_id=edge_id)
    logger.info("reviewer disconnect %s: %s", edge_id, reason)
    return {"ok": True}


def make_reviewer_tools() -> list:
    """Full tool set for the reviewer agent."""
    from app.agent.tools.commit_abstract import commit_abstract_memory
    from app.agent.tools.recall import recall
    return [
        commit_abstract_memory, recall,
        update_abstract_content, fade_node, touch_node, delete_fragment,
        connect, disconnect,
    ]
