"""``_download_image_from_url`` hardening: scheme allowlist + body size cap.

QQ inbound image urls come from Ed25519-verified QQ events; forgery is blocked at
gateway verification. Still, the download must reject non-http(s) schemes (no
``file://`` / ``ftp://`` SSRF surface) and cap the response body at 10MB via BOTH
a Content-Length precheck AND a streaming byte counter (so a missing or lying
Content-Length cannot make us buffer an unbounded body).
"""
from __future__ import annotations

import httpx
import pytest

from app.services.image_pipeline import UpstreamError, _download_image_from_url

_LIMIT = 10 * 1024 * 1024


class _FakeStreamResp:
    """Stand-in for the response yielded by ``async with client.stream(...)``."""

    def __init__(self, chunks, headers=None):
        self._chunks = chunks
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_e):
        return False

    def raise_for_status(self):
        return None

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


class _FakeClient:
    def __init__(self, resp, recorder):
        self._resp = resp
        self._recorder = recorder

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_e):
        return False

    def stream(self, method, url):
        self._recorder["method"] = method
        self._recorder["url"] = url
        return self._resp


def _install_client(monkeypatch, resp):
    recorder: dict = {}

    def factory(*_a, **kwargs):
        recorder["init_kwargs"] = kwargs
        return _FakeClient(resp, recorder)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return recorder


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_url", ["file:///etc/passwd", "ftp://host/x.png"])
async def test_rejects_non_http_scheme(bad_url):
    with pytest.raises(UpstreamError):
        await _download_image_from_url(bad_url)


@pytest.mark.asyncio
async def test_returns_bytes_for_valid_http_url(monkeypatch):
    payload = b"\x89PNG-tiny-body"
    resp = _FakeStreamResp([payload[:4], payload[4:]])
    recorder = _install_client(monkeypatch, resp)

    out = await _download_image_from_url("https://qq.cdn.example/a.png")

    assert out == payload
    assert recorder["url"] == "https://qq.cdn.example/a.png"
    assert recorder["method"] == "GET"
    # KEEP timeout (10s) + forward-proxy behavior exactly as before
    assert recorder["init_kwargs"]["timeout"] == 10
    assert "proxy" in recorder["init_kwargs"]


@pytest.mark.asyncio
async def test_aborts_when_body_exceeds_limit_streaming(monkeypatch):
    """No Content-Length but the streamed body blows past 10MB → the running
    byte counter must abort and raise (defends against missing/lying length)."""
    one_mb = b"x" * (1024 * 1024)
    resp = _FakeStreamResp([one_mb] * 11, headers={})  # 11MB, no header
    _install_client(monkeypatch, resp)

    with pytest.raises(UpstreamError):
        await _download_image_from_url("https://qq.cdn.example/big.png")


@pytest.mark.asyncio
async def test_aborts_when_content_length_over_limit(monkeypatch):
    """Content-Length already over the cap → abort before streaming the body."""
    resp = _FakeStreamResp([b"x"], headers={"content-length": str(_LIMIT + 1)})
    _install_client(monkeypatch, resp)

    with pytest.raises(UpstreamError):
        await _download_image_from_url("https://qq.cdn.example/claims-big.png")
