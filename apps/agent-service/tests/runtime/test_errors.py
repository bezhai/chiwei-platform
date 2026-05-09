"""Phase 7b Gap 18: typed exceptions for error policy."""
from app.runtime.errors import AlreadySucceededError, DuplicateData, NeedsReview


def test_duplicate_data_is_exception():
    assert issubclass(DuplicateData, Exception)
    exc = DuplicateData("dup id=42")
    assert str(exc) == "dup id=42"


def test_needs_review_is_exception():
    assert issubclass(NeedsReview, Exception)
    exc = NeedsReview("needs operator approval")
    assert str(exc) == "needs operator approval"


def test_already_succeeded_error_carries_inflight_keys():
    exc = AlreadySucceededError(edge_id="EdgeA::consumer", idempotent_key="abc123")
    assert exc.edge_id == "EdgeA::consumer"
    assert exc.idempotent_key == "abc123"
    assert "EdgeA::consumer" in str(exc)
    assert "abc123" in str(exc)


def test_already_succeeded_error_is_exception():
    assert issubclass(AlreadySucceededError, Exception)
