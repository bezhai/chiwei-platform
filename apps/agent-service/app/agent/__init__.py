"""agent — unified thinking entry point.

Public surface:
    Agent       — the unified Agent class (run / stream / extract)
    AgentConfig — per-agent configuration (prompt_id, model_id, trace_name)
    AGENTS      — registry of all pre-configured agents

    build_chat_model   — resolve model_id to a LangChain BaseChatModel
    get_prompt         — fetch a Langfuse prompt (with lane routing)
    compile_prompt     — fetch + compile a prompt
"""

from app.agent.core import AGENTS, Agent, AgentConfig
from app.agent.embedding import (
    HybridEmbedding,
    InstructionBuilder,
    Modality,
    SparseVector,
    embed_dense,
    embed_hybrid,
    generate_image,
)
from app.agent.models import build_chat_model
from app.agent.prompts import compile_prompt, get_prompt
from app.agent.tools import ALL_TOOLS, BASE_TOOLS

__all__ = [
    "AGENTS",
    "Agent",
    "AgentConfig",
    "ALL_TOOLS",
    "BASE_TOOLS",
    "HybridEmbedding",
    "InstructionBuilder",
    "Modality",
    "SparseVector",
    "build_chat_model",
    "compile_prompt",
    "embed_dense",
    "embed_hybrid",
    "generate_image",
    "get_prompt",
]
