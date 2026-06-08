from __future__ import annotations

from app.service.callbacks import callback_url_allowed


def test_callback_url_allows_configured_private_ip_network() -> None:
    assert callback_url_allowed(
        "http://192.168.1.23/callback",
        allowed_hosts=(),
        allowed_networks=("192.168.0.0/16",),
    )


def test_callback_url_rejects_public_ip_by_default() -> None:
    assert not callback_url_allowed(
        "http://8.8.8.8/callback",
        allowed_hosts=(),
        allowed_networks=("10.0.0.0/8",),
    )


def test_callback_url_allows_explicit_host() -> None:
    assert callback_url_allowed(
        "http://tagger-callback.internal/callback",
        allowed_hosts=("tagger-callback.internal",),
        allowed_networks=(),
    )
