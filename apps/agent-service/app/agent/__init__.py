"""agent — unified thinking entry point.

Public surface:
    Agent       — the unified Agent class (run / extract), built from an
                  :class:`~app.agent.contracts.AgentSpec`
    PromptSet   — the prompts some agents render, resolved together and checked strictly
"""

from app.agent.core import Agent
from app.agent.image_gen import generate_image
from app.agent.prompts import PromptSet

__all__ = [
    "Agent",
    "PromptSet",
    "generate_image",
]
