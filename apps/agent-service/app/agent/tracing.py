"""Langfuse trace / callback management.

Provides a single helper that builds the LangChain ``config`` dict with
the correct ``CallbackHandler`` for Langfuse tracing.
"""

from __future__ import annotations

from typing import Any

from langfuse.langchain import CallbackHandler


def make_config(
    *,
    trace_name: str | None = None,
    parent_run_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a LangChain *config* dict with a Langfuse ``CallbackHandler``.

    Args:
        trace_name: Optional run name (shown in Langfuse UI).
        parent_run_id: If given, attaches this invocation to an existing trace.
        metadata: Extra key-value pairs passed to Langfuse.

    Returns:
        A ``config`` dict ready for ``model.ainvoke(..., config=config)``.
    """
    cb_kwargs: dict[str, Any] = {}
    if parent_run_id:
        cb_kwargs["trace_id"] = parent_run_id
    if metadata:
        cb_kwargs["metadata"] = metadata

    config: dict[str, Any] = {
        "callbacks": [CallbackHandler(**cb_kwargs)],
    }
    if trace_name:
        config["run_name"] = trace_name
    return config
