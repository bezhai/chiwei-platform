"""Determinism tests for the shared UUID5 helper used by vectorize."""
from __future__ import annotations

import uuid

from app.nodes._ids import vector_id_for


def test_vector_id_for_is_deterministic():
    """Same message_id → identical UUID5 string across calls."""
    mid = "msg-abc-123"
    assert vector_id_for(mid) == vector_id_for(mid)


def test_vector_id_for_is_uuid_string():
    """Output parses cleanly as a UUID and is stable vs. uuid.uuid5 directly."""
    mid = "another-message-id"
    out = vector_id_for(mid)
    parsed = uuid.UUID(out)  # raises if not a valid UUID string
    assert str(parsed) == out
    assert out == str(uuid.uuid5(uuid.NAMESPACE_DNS, mid))
