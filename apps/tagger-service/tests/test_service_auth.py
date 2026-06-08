from __future__ import annotations

from app.service.auth import bearer_token_allowed, bearer_token_from_header


def test_bearer_token_from_header_extracts_value() -> None:
    assert bearer_token_from_header("Bearer abc123") == "abc123"


def test_bearer_token_from_header_rejects_wrong_scheme() -> None:
    assert bearer_token_from_header("Basic abc123") is None
    assert bearer_token_from_header("abc123") is None


def test_bearer_token_allowed_matches_constant_token_list() -> None:
    assert bearer_token_allowed("Bearer live-token", ("old-token", "live-token"))
    assert not bearer_token_allowed("Bearer wrong", ("live-token",))
    assert not bearer_token_allowed(None, ("live-token",))
    assert not bearer_token_allowed("Bearer live-token", ())
