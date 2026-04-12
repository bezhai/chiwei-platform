"""Stream token processing — state tracking, content filtering, length truncation.

Processes the raw token stream from ``Agent.stream()``:
  - ``AIMessageChunk``: text token / finish_reason / tool_call boundary
  - ``ToolMessage``: tool call result (consumed silently)

Callers iterate ``handle_token`` results and yield non-None text pieces.
Special signals (content_filter, length truncation) are returned as sentinel
values that callers must check and react to.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from langchain_core.messages import AIMessageChunk, ToolMessage

logger = logging.getLogger(__name__)

# Consumer-side marker for splitting into multiple messages
SPLIT_MARKER = "---split---"


@dataclass
class StreamState:
    """Mutable state tracked across the token stream."""

    full_content: str = ""
    agent_token_count: int = 0
    tool_call_count: int = 0
    _has_text_in_current_turn: bool = field(default=False, repr=False)


def handle_token(token: object, state: StreamState) -> list[str | None]:
    """Process a single stream token, return text pieces to yield.

    Return semantics:
      - ``[]``         — nothing to yield (ToolMessage, empty chunk)
      - ``[text, ...]`` — yield each non-None element
      - ``[None]``      — content_filter signal; caller yields error message
      - ``["(...)"]``   — length truncation signal
    """
    if isinstance(token, AIMessageChunk):
        finish_reason = token.response_metadata.get("finish_reason")

        if finish_reason == "content_filter":
            return [None]
        if finish_reason == "length":
            return ["(后续内容被截断)"]

        result: list[str | None] = []

        if token.text:
            state._has_text_in_current_turn = True
            state.agent_token_count += 1
            state.full_content += token.text
            result.append(token.text)

        # text -> tool_call boundary: inject split marker
        if token.tool_call_chunks and state._has_text_in_current_turn:
            result.append(SPLIT_MARKER)
            state._has_text_in_current_turn = False

        return result

    if isinstance(token, ToolMessage):
        state.tool_call_count += 1
        state._has_text_in_current_turn = False

    return []


def is_content_filter(result: list[str | None]) -> bool:
    """Check whether ``handle_token`` returned a content_filter signal."""
    return len(result) == 1 and result[0] is None


def is_length_truncated(result: list[str | None]) -> bool:
    """Check whether ``handle_token`` returned a length-truncation signal."""
    return len(result) == 1 and result[0] == "(后续内容被截断)"
