"""Typed exceptions of the runtime."""
from app.runtime.errors import AlreadySucceededError


def test_already_succeeded_error_carries_inflight_keys():
    exc = AlreadySucceededError(edge_id="EdgeA::consumer", idempotent_key="abc123")
    assert exc.edge_id == "EdgeA::consumer"
    assert exc.idempotent_key == "abc123"
    assert "EdgeA::consumer" in str(exc)
    assert "abc123" in str(exc)


def test_already_succeeded_error_is_exception():
    assert issubclass(AlreadySucceededError, Exception)
