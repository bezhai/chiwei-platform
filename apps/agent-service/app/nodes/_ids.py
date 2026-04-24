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

    Uses ``uuid.NAMESPACE_DNS`` as the namespace. The namespace is fixed
    so every writer (new or re-run) produces the same point_id for the
    same ``message_id`` — that's how qdrant upsert stays idempotent.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, message_id))
