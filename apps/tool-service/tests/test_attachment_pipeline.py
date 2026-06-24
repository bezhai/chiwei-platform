"""Tests for the shared attachment pipeline (download → store TOS → presigned URL).

The image pipeline (compress before store) and the file pipeline (store raw)
are the two callers of one shared "download attachment → upload to TOS → get
URL" core. These tests pin the shared core's behaviour and the file caller's
"no compression, raw bytes" contract.
"""
from __future__ import annotations

import pytest

from app.services import attachment_pipeline


class _FakeLark:
    """Records download calls, returns fixed bytes per (resource_type)."""

    def __init__(self, payload: bytes = b"raw-attachment-bytes"):
        self.payload = payload
        self.calls: list[tuple[str, str, str, str]] = []

    async def download_message_resource(
        self, bot_name: str, message_id: str, file_key: str, resource_type: str
    ) -> bytes:
        self.calls.append((bot_name, message_id, file_key, resource_type))
        return self.payload


class _FakeTos:
    def __init__(self):
        self.uploads: list[tuple[str, bytes]] = []

    async def upload_file(self, file_name: str, data: bytes) -> None:
        self.uploads.append((file_name, data))

    async def get_file_url(self, file_name: str) -> str:
        return f"https://tos.example/{file_name}?sig=1"


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    async def redis_get(self, key: str) -> str | None:
        return self.store.get(key)

    async def redis_set_with_expire(self, key: str, value: str, seconds: int) -> None:
        self.store[key] = value


@pytest.fixture
def fakes(monkeypatch):
    lark = _FakeLark()
    tos = _FakeTos()
    redis = _FakeRedis()

    monkeypatch.setattr(attachment_pipeline, "lark_client", lark)
    monkeypatch.setattr(attachment_pipeline, "tos_client", tos)
    monkeypatch.setattr(attachment_pipeline, "redis_client", redis)
    # RedisLock is a no-op context manager in tests (no real redis lock backend).
    monkeypatch.setattr(
        attachment_pipeline, "RedisLock", _NoopLock, raising=True
    )
    return lark, tos, redis


class _NoopLock:
    def __init__(self, *_args, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


@pytest.mark.asyncio
async def test_file_pipeline_stores_raw_bytes_unchanged(fakes):
    """File caller: bytes go to TOS exactly as downloaded — NO compression."""
    lark, tos, _redis = fakes
    lark.payload = b"chapter one was a quiet morning ..."

    result = await attachment_pipeline.process_file_pipeline(
        file_key="file_abc",
        message_id="om_1",
        bot_name="chiwei",
    )

    # downloaded with resource_type='file' (not 'image')
    assert lark.calls == [("chiwei", "om_1", "file_abc", "file")]
    # uploaded raw, byte-for-byte
    assert len(tos.uploads) == 1
    uploaded_name, uploaded_bytes = tos.uploads[0]
    assert uploaded_bytes == b"chapter one was a quiet morning ..."
    # returns a stable file_name + presigned url + echoes file_key
    assert result["file_key"] == "file_abc"
    assert result["file_name"] == uploaded_name
    assert result["url"].startswith("https://tos.example/")


@pytest.mark.asyncio
async def test_file_pipeline_file_name_is_distinct_from_image_jpg(fakes):
    """File name must not collide with the image pipeline's temp/<key>.jpg."""
    result = await attachment_pipeline.process_file_pipeline(
        file_key="file_abc", message_id="om_1", bot_name="chiwei"
    )
    assert not result["file_name"].endswith(".jpg")
    assert "file_abc" in result["file_name"]


@pytest.mark.asyncio
async def test_file_pipeline_upload_cache_hit_skips_download(fakes):
    """Second call for the same file_key reuses cached file_name, no re-download."""
    lark, tos, _redis = fakes

    first = await attachment_pipeline.process_file_pipeline(
        file_key="file_abc", message_id="om_1", bot_name="chiwei"
    )
    second = await attachment_pipeline.process_file_pipeline(
        file_key="file_abc", message_id="om_1", bot_name="chiwei"
    )

    assert first["file_name"] == second["file_name"]
    # download happened only once (second served from upload cache)
    assert len(lark.calls) == 1
    assert len(tos.uploads) == 1


@pytest.mark.asyncio
async def test_shared_core_applies_caller_transform_before_upload(fakes):
    """The shared core applies the caller-provided byte transform before TOS upload.

    Image = compress transform; file = identity. Both download and transform are
    caller concerns, not baked into the shared store/url/cache steps.
    """
    _lark, tos, _redis = fakes

    async def download() -> bytes:
        return b"original"

    async def upper(b: bytes) -> bytes:
        return b.upper()

    result = await attachment_pipeline.process_attachment_pipeline(
        file_key="k1",
        download=download,
        transform=upper,
        file_name="temp/k1.bin",
        cache_prefix="attachment",
    )

    assert tos.uploads == [("temp/k1.bin", b"ORIGINAL")]
    assert result["file_name"] == "temp/k1.bin"


@pytest.mark.asyncio
async def test_shared_core_url_cache_hit_skips_resign(fakes):
    """URL layer: a cached url for the file_name is reused, not re-signed."""
    _lark, tos, redis = fakes
    redis.store["attachment_upload:k9"] = "temp/k9.bin"
    redis.store["attachment_url:temp/k9.bin"] = "https://cached/url"

    downloaded = False

    async def download() -> bytes:
        nonlocal downloaded
        downloaded = True
        return b"x"

    async def identity(b: bytes) -> bytes:
        return b

    result = await attachment_pipeline.process_attachment_pipeline(
        file_key="k9",
        download=download,
        transform=identity,
        file_name="temp/k9.bin",
        cache_prefix="attachment",
    )

    assert result["url"] == "https://cached/url"
    assert tos.uploads == []  # nothing uploaded (upload cache hit)
    assert downloaded is False  # download skipped on upload-cache hit
