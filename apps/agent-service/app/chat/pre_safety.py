"""Chat pipeline pre-safety entry — emit_and_wait wrapper.

Thin wrapper that hides the dataflow request/reply mechanics from
``chat_node``: build a ``PreSafetyRequest`` with a fresh correlation id,
emit it into the graph and await the matching ``PreSafetyVerdict``.

This module replaces ``pre_safety_gate.py`` (deleted) — the old global
``_waiters`` dict + bespoke ``register`` / ``resolve`` / ``cleanup``
plumbing now lives generically in :mod:`app.runtime.emit_wait`. The
chat-pipeline-specific knobs that stay here:

* ``pre_request_id`` = a per-call uuid4 (same shape the gate used).
* Timeout = 21s — ``_run_pre_audit`` caps its 3 LLM checks at 20s, plus
  1s scheduling slack.
* Fail-open: ``EmitWaitTimeout`` or any in-flight emit failure is
  surfaced to the caller as an exception; chat_node's
  ``_resolve_pre_safety_for_part`` already catches it and falls back to
  ALLOW. Keeping the fail-open behaviour out of this helper keeps the
  invariant in one place (chat_node's segment-boundary check).
"""
from __future__ import annotations

import uuid

from app.domain.safety import PreSafetyRequest, PreSafetyVerdict
from app.runtime.emit_wait import emit_and_wait

# 节点内部超时 20s（``_run_pre_audit`` ceiling），加 1s 缓冲
_PRE_SAFETY_TIMEOUT_SECONDS: float = 21.0


async def run_pre_safety_check(
    message_id: str, content: str, persona_id: str
) -> PreSafetyVerdict:
    """Chat pipeline entry: emit PreSafetyRequest, wait for PreSafetyVerdict."""
    pre_request_id = str(uuid.uuid4())
    return await emit_and_wait(
        PreSafetyRequest(
            pre_request_id=pre_request_id,
            message_id=message_id,
            message_content=content,
            persona_id=persona_id,
        ),
        wait_for=PreSafetyVerdict,
        correlation=pre_request_id,
        correlation_field="pre_request_id",
        timeout_s=_PRE_SAFETY_TIMEOUT_SECONDS,
    )
