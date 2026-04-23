"""Shared ID helpers for @node functions.

Deterministic UUID5 derivation keeps vector-store point IDs stable across
retries and re-processing: feeding the same ``message_id`` always yields
the same UUID, so durable-edge retry + qdrant upsert are naturally
idempotent without extra bookkeeping.
"""
from __future__ import annotations

import uuid


def vector_id_for(message_id: str) -> str:
    """Derive a stable UUID5 (string form) from a ``message_id``.

    Uses ``uuid.NAMESPACE_DNS`` as the namespace — matching the legacy
    ``vectorize_message`` worker so in-flight messages keep their ids
    across the refactor cut-over.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, message_id))
