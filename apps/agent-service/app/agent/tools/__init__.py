"""Agent tool sets.

Exports two tool lists:

- ``BASE_TOOLS`` — available to all agents including sub-agents.
- ``ALL_TOOLS`` — only for the main agent.
"""

from app.agent.tools.commit_abstract import commit_abstract_memory
from app.agent.tools.delegation import deep_research
from app.agent.tools.history import (
    check_chat_history,
    list_group_members,
    search_group_history,
)
from app.agent.tools.image import generate_image, read_images
from app.agent.tools.image_search import search_images
from app.agent.tools.notes import resolve_note, write_note
from app.agent.tools.recall import recall
from app.agent.tools.sandbox import sandbox_bash
from app.agent.tools.search import search_web
from app.agent.tools.skill import load_skill
from app.agent.tools.update_schedule import update_schedule

# Base tools: available to all agents (including sub-agents like research)
BASE_TOOLS = [
    search_web,
    search_images,
    generate_image,
    read_images,
    recall,
    commit_abstract_memory,
    write_note,
    resolve_note,
    update_schedule,
]

# All tools: only for the main agent.
# History search tools are intentionally excluded: current-chat and cross-chat
# context should be injected up front rather than searched ad hoc at reply time.
ALL_TOOLS = [
    *BASE_TOOLS,
    list_group_members,
    deep_research,
    load_skill,
    sandbox_bash,
]

__all__ = [
    "BASE_TOOLS",
    "ALL_TOOLS",
    # Individual tools (for callers that need fine-grained control)
    "search_web",
    "search_images",
    "generate_image",
    "read_images",
    "recall",
    "commit_abstract_memory",
    "write_note",
    "resolve_note",
    "update_schedule",
    "check_chat_history",
    "search_group_history",
    "list_group_members",
    "deep_research",
    "load_skill",
    "sandbox_bash",
]
