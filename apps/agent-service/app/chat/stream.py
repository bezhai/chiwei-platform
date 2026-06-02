"""Stream chunk processing — state tracking, content filtering, length truncation.

Processes the neutral ``StreamChunk`` stream from ``Agent.stream()``. A chunk
populates one of five fields; ``handle_token`` maps each:

  - ``finish_reason == "content_filter"`` -> ``[None]`` (caller yields error msg)
  - ``finish_reason == "length"``         -> length-truncation marker
  - ``text``                              -> accumulate + yield the token
  - ``tool_call`` (after text this turn)  -> SPLIT_MARKER (text→tool boundary)
  - ``tool_result``                       -> consumed silently, counted
  - ``reasoning`` / empty / other finish  -> nothing yielded

Callers iterate ``handle_token`` results and yield non-None text pieces. Special
signals (content_filter, length) are returned as sentinel values the caller must
check and react to.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.agent.neutral import StreamChunk

logger = logging.getLogger(__name__)

# Consumer-side marker for splitting into multiple messages
SPLIT_MARKER = "---split---"

_LENGTH_MARKER = "(后续内容被截断)"


@dataclass
class StreamState:
    """Mutable state tracked across the chunk stream."""

    full_content: str = ""
    agent_token_count: int = 0
    tool_call_count: int = 0
    _has_text_in_current_turn: bool = field(default=False, repr=False)


def handle_token(chunk: StreamChunk, state: StreamState) -> list[str | None]:
    """Process a single stream chunk, return text pieces to yield.

    Return semantics:
      - ``[]``          — nothing to yield (tool_result, reasoning, empty)
      - ``[text, ...]`` — yield each non-None element
      - ``[None]``      — content_filter signal; caller yields error message
      - ``["(...)"]``   — length truncation signal
    """
    if chunk.finish_reason == "content_filter":
        return [None]
    if chunk.finish_reason == "length":
        return [_LENGTH_MARKER]

    # A tool_result chunk closes the current tool round: count it silently and
    # reset the per-turn text flag (a tool call ends the assistant's text turn).
    if chunk.tool_result is not None:
        state.tool_call_count += 1
        state._has_text_in_current_turn = False
        return []

    result: list[str | None] = []

    if chunk.text:
        state._has_text_in_current_turn = True
        state.agent_token_count += 1
        state.full_content += chunk.text
        result.append(chunk.text)

    # text -> tool_call boundary: inject split marker
    if chunk.tool_call is not None and state._has_text_in_current_turn:
        result.append(SPLIT_MARKER)
        state._has_text_in_current_turn = False

    return result


def is_content_filter(result: list[str | None]) -> bool:
    """Check whether ``handle_token`` returned a content_filter signal."""
    return len(result) == 1 and result[0] is None


def is_length_truncated(result: list[str | None]) -> bool:
    """Check whether ``handle_token`` returned a length-truncation signal."""
    return len(result) == 1 and result[0] == _LENGTH_MARKER
