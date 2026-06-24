"""process_image_pipeline keeps its contract after routing through the shared core.

Locks the image caller's invariants: compresses before storing, names
``temp/<key>.jpg``, uses the ``image_*`` cache prefix (prod cache continuity),
and still falls back to ``download_image`` when there is no message_id.
"""
from __future__ import annotations

import io

import pytest
from PIL import Image

from app.services import image_pipeline


def _png(width: int, height: int) -> bytes:
    img = Image.new("RGB", (width, height), color="red")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class _FakeLark:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.resource_calls: list[tuple] = []
        self.image_calls: list[tuple] = []

    async def download_message_resource(self, bot_name, message_id, file_key, resource_type):
        self.resource_calls.append((bot_name, message_id, file_key, resource_type))
        return self.payload

    async def download_image(self, bot_name, image_key):
        self.image_calls.append((bot_name, image_key))
        return self.payload


class _FakeTos:
    def __init__(self):
        self.uploads: list[tuple[str, bytes]] = []

    async def upload_file(self, file_name, data):
        self.uploads.append((file_name, data))

    async def get_file_url(self, file_name):
        return f"https://tos/{file_name}"


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    async def redis_get(self, key):
        return self.store.get(key)

    async def redis_set_with_expire(self, key, value, seconds):
        self.store[key] = value


class _NoopLock:
    def __init__(self, *_a, **_k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_e):
        return False


@pytest.fixture
def fakes(monkeypatch):
    from app.services import attachment_pipeline

    lark = _FakeLark(_png(2000, 2000))
    tos = _FakeTos()
    redis = _FakeRedis()
    monkeypatch.setattr(attachment_pipeline, "lark_client", lark)
    monkeypatch.setattr(attachment_pipeline, "tos_client", tos)
    monkeypatch.setattr(attachment_pipeline, "redis_client", redis)
    monkeypatch.setattr(attachment_pipeline, "RedisLock", _NoopLock)
    # image_pipeline may still reference lark_client directly for the no-message path
    monkeypatch.setattr(image_pipeline, "lark_client", lark)
    return lark, tos, redis


@pytest.mark.asyncio
async def test_image_with_message_downloads_as_image_and_compresses(fakes):
    lark, tos, redis = fakes

    result = await image_pipeline.process_image_pipeline(
        file_key="img_k", message_id="om_1", bot_name="chiwei"
    )

    # downloaded as image resource type
    assert lark.resource_calls == [("chiwei", "om_1", "img_k", "image")]
    # stored under the jpg temp namespace, image cache prefix
    assert result["file_name"] == "temp/img_k.jpg"
    assert "image_upload:img_k" in redis.store
    # stored bytes are JPEG (compressed), not the original PNG
    name, data = tos.uploads[0]
    assert name == "temp/img_k.jpg"
    assert Image.open(io.BytesIO(data)).format == "JPEG"


@pytest.mark.asyncio
async def test_image_without_message_uses_download_image(fakes):
    lark, _tos, _redis = fakes

    await image_pipeline.process_image_pipeline(
        file_key="img_k2", message_id=None, bot_name="chiwei"
    )

    assert lark.image_calls == [("chiwei", "img_k2")]
    assert lark.resource_calls == []
