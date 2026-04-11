"""agent — unified thinking entry point.

Public surface:
    Agent       — the unified Agent class (run / stream / extract)
    AgentConfig — per-agent configuration (prompt_id, model_id, trace_name)
    AGENTS      — registry of all pre-configured agents

    build_chat_model   — resolve model_id to a LangChain BaseChatModel
    get_prompt         — fetch a Langfuse prompt (with lane routing)
    compile_prompt     — fetch + compile a prompt
    make_config        — build a LangChain config dict with Langfuse tracing
"""

from app.agent.core import AGENTS, Agent, AgentConfig
from app.agent.models import build_chat_model
from app.agent.prompts import compile_prompt, get_prompt
from app.agent.tracing import make_config

__all__ = [
    "AGENTS",
    "Agent",
    "AgentConfig",
    "build_chat_model",
    "compile_prompt",
    "get_prompt",
    "make_config",
]
