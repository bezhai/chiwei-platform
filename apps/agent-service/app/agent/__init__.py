"""agent — unified thinking entry point.

Public surface:
    Agent       — the unified Agent class (run / stream / extract), built from an
                  :class:`~app.agent.contracts.AgentSpec`

    get_prompt         — fetch a Langfuse prompt (with lane routing)
"""

from app.agent.core import Agent
from app.agent.image_gen import generate_image
from app.agent.prompts import get_prompt

__all__ = [
    "Agent",
    "generate_image",
    "get_prompt",
]
