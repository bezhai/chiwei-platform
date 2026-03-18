import asyncio
import base64
import logging

from prometheus_client import Counter, Histogram

from app.infrastructure import lark_client, redis_client, tos_client
from app.infrastructure.redis_lock import RedisLock
from app.services.image_service import process_image

logger = logging.getLogger(__name__)


class UpstreamError(Exception):
    """Error from an upstream service (Lark API, external URL)."""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


class UpstreamTimeoutError(UpstreamError):
    """Timeout from an upstream service."""

    def __init__(self, message: str):
        super().__init__(message, status_code=504)

IMAGE_PIPELINE_DURATION = Histogram(
    "image_pipeline_step_duration_seconds",
    "Duration of each image pipeline step",
    ["step"],  # download, compress, upload_tos, get_url
)
IMAGE_PIPELINE_TOTAL = Counter(
    "image_pipeline_requests_total",
    "Total image pipeline requests",
    ["source_type", "status"],  # base64/url, success/error
)

_UPLOAD_CACHE_TTL = 7 * 24 * 60 * 60  # 7 days
_URL_CACHE_TTL = 10 * 60  # 10 minutes


async def process_image_pipeline(
    file_key: str, message_id: str | None, bot_name: str
) -> dict:
    """
    Image pipeline: download from Lark -> compress -> upload to TOS -> return URL.
    Two-layer cache: upload cache (7d) + URL cache (10min).
    """
    # Layer 1: check if file already uploaded
    upload_cache_key = f"image_upload:{file_key}"
    file_name = await redis_client.redis_get(upload_cache_key)

    if not file_name:
        # Acquire distributed lock to prevent concurrent uploads
        async with RedisLock(f"image_upload_lock:{file_key}", ttl=60, timeout=30):
            # Double-check after acquiring lock
            file_name = await redis_client.redis_get(upload_cache_key)
            if not file_name:
                # Download image from Lark
                if message_id:
                    image_bytes = await lark_client.download_message_resource(
                        bot_name, message_id, file_key
                    )
                else:
                    image_bytes = await lark_client.download_image(bot_name, file_key)

                # Compress: 1440x1440, JPEG q80 (CPU-bound, offload to thread)
                compressed, _, _ = await asyncio.to_thread(
                    process_image,
                    image_bytes, max_width=1440, max_height=1440, quality=80, format="JPEG",
                )

                # Upload to TOS
                file_name = f"temp/{file_key}.jpg"
                await tos_client.upload_file(file_name, compressed)

                # Write layer 1 cache
                await redis_client.redis_set_with_expire(upload_cache_key, file_name, _UPLOAD_CACHE_TTL)
                logger.info(f"Image uploaded: {file_key} -> {file_name}")
    else:
        logger.debug(f"Image already uploaded (cache hit): {file_key}")

    # Layer 2: get pre-signed URL with cache
    url_cache_key = f"image_url:{file_name}"
    url = await redis_client.redis_get(url_cache_key)
    if not url:
        url = await tos_client.get_file_url(file_name)
        await redis_client.redis_set_with_expire(url_cache_key, url, _URL_CACHE_TTL)

    return {"url": url, "file_key": file_key, "file_name": file_name}


async def get_file_url(file_name: str) -> dict:
    """Get pre-signed URL for an already-uploaded TOS file. No Lark download."""
    url_cache_key = f"image_url:{file_name}"
    url = await redis_client.redis_get(url_cache_key)
    if not url:
        url = await tos_client.get_file_url(file_name)
        await redis_client.redis_set_with_expire(url_cache_key, url, _URL_CACHE_TTL)
    return {"url": url, "file_name": file_name}


async def upload_to_tos(source_type: str, data: str) -> dict:
    """
    Upload image to TOS from base64 or external URL.
    Compress (1440x1440 JPEG q80) then upload, return pre-signed URL.

    Args:
        source_type: "base64" or "url"
        data: base64 string or URL
    """
    import hashlib
    import time
    import uuid

    t_start = time.monotonic()

    if source_type == "base64":
        raw = data
        if "," in raw:
            raw = raw.split(",", 1)[1]
        image_bytes = base64.b64decode(raw)
        file_id = hashlib.md5(image_bytes).hexdigest()[:16]
        t_download = time.monotonic() - t_start
        IMAGE_PIPELINE_DURATION.labels(step="decode_base64").observe(t_download)
    elif source_type == "url":
        import httpx
        from app.config.config import settings as _settings

        proxy = _settings.forward_proxy_url
        try:
            async with httpx.AsyncClient(timeout=10, proxy=proxy) as client:
                resp = await client.get(data)
                resp.raise_for_status()
                image_bytes = resp.content
        except httpx.TimeoutException:
            raise UpstreamTimeoutError(f"URL download timed out: {data[:200]}")
        except httpx.HTTPStatusError as e:
            raise UpstreamError(
                f"URL download failed: {e.response.status_code}",
                status_code=e.response.status_code,
            )
        t_download = time.monotonic() - t_start
        IMAGE_PIPELINE_DURATION.labels(step="download_url").observe(t_download)
        file_id = hashlib.md5(image_bytes).hexdigest()[:16]
    else:
        raise ValueError(f"Invalid source_type: {source_type}, expected 'base64' or 'url'")

    # Compress: 1440x1440, JPEG q80
    t0 = time.monotonic()
    compressed, _, _ = await asyncio.to_thread(
        process_image,
        image_bytes, max_width=1440, max_height=1440, quality=80, format="JPEG",
    )
    t_compress = time.monotonic() - t0
    IMAGE_PIPELINE_DURATION.labels(step="compress").observe(t_compress)

    # Upload to TOS
    t0 = time.monotonic()
    file_name = f"temp/tos_{file_id}_{uuid.uuid4().hex[:8]}.jpg"
    await tos_client.upload_file(file_name, compressed)
    t_upload = time.monotonic() - t0
    IMAGE_PIPELINE_DURATION.labels(step="upload_tos").observe(t_upload)

    # Get pre-signed URL
    url_cache_key = f"image_url:{file_name}"
    url = await tos_client.get_file_url(file_name)
    await redis_client.redis_set_with_expire(url_cache_key, url, _URL_CACHE_TTL)

    t_total = time.monotonic() - t_start
    IMAGE_PIPELINE_TOTAL.labels(source_type=source_type, status="success").inc()
    logger.info(
        "upload_to_tos done: source=%s size=%dKB "
        "download=%.2fs compress=%.2fs upload=%.2fs total=%.2fs",
        source_type, len(image_bytes) // 1024,
        t_download, t_compress, t_upload, t_total,
    )

    return {"url": url, "file_name": file_name}


async def upload_base64_image(base64_data: str, bot_name: str) -> dict:
    """
    Upload a base64 image to Lark, return image_key.
    """
    # Strip data:image/...;base64, prefix
    if "," in base64_data:
        base64_data = base64_data.split(",", 1)[1]

    image_bytes = base64.b64decode(base64_data)
    image_key = await lark_client.upload_image(bot_name, image_bytes)
    logger.info(f"Base64 image uploaded, image_key: {image_key}")
    return {"image_key": image_key}
