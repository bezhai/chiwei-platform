"""Agent tool sets.

Exports two tool lists:

- ``BASE_TOOLS`` — available to all agents including sub-agents.
- ``ALL_TOOLS`` — only for the main agent (adds delegation, skill, sandbox).
"""

from app.agent.tools.delegation import deep_research
from app.agent.tools.image import generate_image, read_images
from app.agent.tools.recall import recall
from app.agent.tools.sandbox import sandbox_bash
from app.agent.tools.search import search_images, search_web
from app.agent.tools.skill import load_skill

# Base tools: available to all agents (including sub-agents like research)
BASE_TOOLS = [
    search_web,
    search_images,
    generate_image,
    read_images,
    recall,
]

# All tools: only for the main agent
ALL_TOOLS = [
    *BASE_TOOLS,
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
    "deep_research",
    "load_skill",
    "sandbox_bash",
]
