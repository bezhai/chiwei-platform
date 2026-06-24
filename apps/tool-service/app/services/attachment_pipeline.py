"""Shared attachment pipeline: download from Lark → store to TOS → presigned URL.

One core ("download attachment → upload to TOS → get URL") with two callers:

  * **image** (``app.services.image_pipeline``) — compresses bytes before storing,
    names ``temp/<key>.jpg``, downloads as Lark resource type ``image``.
  * **file** (:func:`process_file_pipeline`) — stores bytes raw (NO compression),
    names ``files/<key>`` keeping the original extension, downloads as type ``file``.

The core handles the two-layer cache (upload cache → file_name; url cache →
presigned url) and the per-key upload lock. The byte transform and the storage
name are caller concerns, passed in.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from app.infrastructure import lark_client, redis_client, tos_client
from app.infrastructure.redis_lock import RedisLock

logger = logging.getLogger(__name__)

_UPLOAD_CACHE_TTL = 7 * 24 * 60 * 60  # 7 days
_URL_CACHE_TTL = 10 * 60  # 10 minutes

Downloader = Callable[[], Awaitable[bytes]]
# transform is async so callers can offload CPU-bound work (e.g. image compression)
# to a thread without blocking the event loop.
ByteTransform = Callable[[bytes], Awaitable[bytes]]


async def process_attachment_pipeline(
    *,
    file_key: str,
    download: Downloader,
    transform: ByteTransform,
    file_name: str,
    cache_prefix: str,
) -> dict:
    """download() → (caller transform) → upload to TOS → presigned URL.

    Both ``download`` and ``transform`` are caller concerns (image vs file differ
    in how they fetch bytes and whether they compress); this core owns only the
    store/url/cache/lock steps. ``transform`` is awaited so CPU-bound work can be
    offloaded to a thread. Two-layer cache keyed by ``cache_prefix``:
      * ``{cache_prefix}_upload:{file_key}`` → stored ``file_name`` (7d)
      * ``{cache_prefix}_url:{file_name}`` → presigned url (10min)

    Returns ``{"url", "file_key", "file_name"}``.
    """
    upload_cache_key = f"{cache_prefix}_upload:{file_key}"
    cached_name = await redis_client.redis_get(upload_cache_key)

    if cached_name:
        file_name = cached_name
        logger.debug("attachment already uploaded (cache hit): %s", file_key)
    else:
        async with RedisLock(f"{cache_prefix}_upload_lock:{file_key}", ttl=60, timeout=30):
            cached_name = await redis_client.redis_get(upload_cache_key)
            if cached_name:
                file_name = cached_name
            else:
                raw = await download()
                payload = await transform(raw)
                await tos_client.upload_file(file_name, payload)
                await redis_client.redis_set_with_expire(
                    upload_cache_key, file_name, _UPLOAD_CACHE_TTL
                )
                logger.info("attachment uploaded: %s -> %s", file_key, file_name)

    url_cache_key = f"{cache_prefix}_url:{file_name}"
    url = await redis_client.redis_get(url_cache_key)
    if not url:
        url = await tos_client.get_file_url(file_name)
        await redis_client.redis_set_with_expire(url_cache_key, url, _URL_CACHE_TTL)

    return {"url": url, "file_key": file_key, "file_name": file_name}


def _file_storage_name(file_key: str) -> str:
    """TOS object name for a raw file. Keeps the file_key so it never collides
    with the image pipeline's ``temp/<key>.jpg`` namespace."""
    return f"files/{file_key}"


async def process_file_pipeline(
    file_key: str, message_id: str | None, bot_name: str
) -> dict:
    """File caller: download from Lark (type=file) and store the bytes raw."""

    async def _download() -> bytes:
        return await lark_client.download_message_resource(
            bot_name, message_id, file_key, "file"
        )

    async def _identity(raw: bytes) -> bytes:
        return raw

    return await process_attachment_pipeline(
        file_key=file_key,
        download=_download,
        transform=_identity,
        file_name=_file_storage_name(file_key),
        cache_prefix="attachment",
    )
