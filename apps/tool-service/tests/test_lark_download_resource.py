"""download_message_resource must pass the resource type through to the Lark SDK.

Images and files are both downloaded via the same message-resource endpoint,
but the Lark SDK request requires the correct ``type`` (``image`` vs ``file``).
The shared attachment pipeline downloads files, so the type must be a parameter
rather than hardcoded to ``image`` — a file downloaded as ``image`` fails.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.infrastructure import lark_client


class _OkResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def success(self) -> bool:
        return True

    @property
    def file(self):
        f = MagicMock()
        f.read.return_value = self._payload
        return f


@pytest.mark.asyncio
async def test_download_message_resource_passes_file_type(monkeypatch):
    captured: dict[str, str] = {}

    # Patch the Lark SDK request builder so we can capture .type(...)
    from lark_oapi.api.im.v1 import GetMessageResourceRequest

    real_builder = GetMessageResourceRequest.builder

    def spy_builder():
        b = real_builder()
        orig_type = b.type

        def type_capture(value):
            captured["type"] = value
            return orig_type(value)

        b.type = type_capture
        return b

    monkeypatch.setattr(GetMessageResourceRequest, "builder", staticmethod(spy_builder))

    fake_client = MagicMock()
    fake_client.im.v1.message_resource.aget = AsyncMock(
        return_value=_OkResponse(b"file-bytes")
    )
    monkeypatch.setattr(lark_client, "get_client", lambda _bot: fake_client)

    out = await lark_client.download_message_resource(
        "chiwei", "om_1", "file_k", "file"
    )

    assert out == b"file-bytes"
    assert captured["type"] == "file"


@pytest.mark.asyncio
async def test_download_message_resource_passes_image_type(monkeypatch):
    captured: dict[str, str] = {}
    from lark_oapi.api.im.v1 import GetMessageResourceRequest

    real_builder = GetMessageResourceRequest.builder

    def spy_builder():
        b = real_builder()
        orig_type = b.type

        def type_capture(value):
            captured["type"] = value
            return orig_type(value)

        b.type = type_capture
        return b

    monkeypatch.setattr(GetMessageResourceRequest, "builder", staticmethod(spy_builder))

    fake_client = MagicMock()
    fake_client.im.v1.message_resource.aget = AsyncMock(
        return_value=_OkResponse(b"img-bytes")
    )
    monkeypatch.setattr(lark_client, "get_client", lambda _bot: fake_client)

    out = await lark_client.download_message_resource(
        "chiwei", "om_1", "img_k", "image"
    )

    assert out == b"img-bytes"
    assert captured["type"] == "image"
