"""agent — unified thinking entry point.

Public surface:
    Agent       — the unified Agent class (run / extract), built from an
                  :class:`~app.agent.contracts.AgentSpec`
    PromptSet   — the prompts some agents render, resolved together and checked strictly
    RETRYABLE_EXCEPTIONS — the exceptions a model call is retried on; a plugin that retries a
                  whole call itself (world's perception judgement) retries on the same ones
"""

from app.agent.core import RETRYABLE_EXCEPTIONS, Agent
from app.agent.image_gen import generate_image
from app.agent.prompts import PromptSet

__all__ = [
    "Agent",
    "PromptSet",
    "RETRYABLE_EXCEPTIONS",
    "generate_image",
]
