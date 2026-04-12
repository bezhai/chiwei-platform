"""agent — unified thinking entry point.

Public surface:
    Agent       — the unified Agent class (run / stream / extract)
    AgentConfig — per-agent configuration (prompt_id, model_id, trace_name)

    build_chat_model   — resolve model_id to a LangChain BaseChatModel
    get_prompt         — fetch a Langfuse prompt (with lane routing)
"""

from app.agent.core import Agent, AgentConfig
from app.agent.embedding import (
    HybridEmbedding,
    InstructionBuilder,
    Modality,
    SparseVector,
    embed_dense,
    embed_hybrid,
)
from app.agent.image_gen import generate_image
from app.agent.models import build_chat_model
from app.agent.prompts import get_prompt

__all__ = [
    "Agent",
    "AgentConfig",
    "HybridEmbedding",
    "InstructionBuilder",
    "Modality",
    "SparseVector",
    "build_chat_model",
    "embed_dense",
    "embed_hybrid",
    "generate_image",
    "get_prompt",
]
