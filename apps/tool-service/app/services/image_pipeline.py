import asyncio
import base64
import logging
from urllib.parse import urlsplit

import httpx
from prometheus_client import Counter, Histogram

from app.infrastructure import lark_client, redis_client, tos_client
from app.services.attachment_pipeline import process_attachment_pipeline
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
_MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024  # 10MB cap on inbound image url bodies


def _compress_image(image_bytes: bytes) -> bytes:
    """Image-specific transform: compress to 1440x1440 JPEG q80."""
    compressed, _, _ = process_image(
        image_bytes, max_width=1440, max_height=1440, quality=80, format="JPEG"
    )
    return compressed


async def _download_image_from_url(url: str) -> bytes:
    """HTTP GET an image from a public url (QQ inbound images arrive as urls).

    Shared by the url branch of ``process_image_pipeline`` and ``upload_to_tos``
    so the one download (forward proxy, timeout, upstream error mapping) lives in
    a single place. Raises ``UpstreamTimeoutError`` / ``UpstreamError`` on
    upstream failure.
    """
    from app.config.config import settings as _settings

    # Scheme allowlist: only http/https. Blocks file://, ftp://, gopher:// etc.
    # 私网拦截依赖上游 webhook 验签 + forward proxy 出口；多租户/更高安全要求时
    # 再加 DNS 解析后私网/metadata IP 段拦截。
    scheme = urlsplit(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise UpstreamError(
            f"URL download rejected: unsupported scheme {scheme!r}, expected http/https",
            status_code=400,
        )

    proxy = _settings.forward_proxy_url
    try:
        async with httpx.AsyncClient(timeout=10, proxy=proxy) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                # Double safeguard against an oversize body.
                # 1) Content-Length precheck (when the header is present + honest).
                declared = resp.headers.get("content-length")
                if declared is not None:
                    try:
                        if int(declared) > _MAX_DOWNLOAD_BYTES:
                            raise UpstreamError(
                                f"URL download too large: {declared} bytes > "
                                f"{_MAX_DOWNLOAD_BYTES} cap",
                                status_code=413,
                            )
                    except ValueError:
                        pass  # malformed header → rely on the streaming counter
                # 2) Streaming byte counter (catches absent/lying Content-Length).
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > _MAX_DOWNLOAD_BYTES:
                        raise UpstreamError(
                            f"URL download exceeded {_MAX_DOWNLOAD_BYTES} byte cap",
                            status_code=413,
                        )
                    chunks.append(chunk)
                return b"".join(chunks)
    except httpx.TimeoutException:
        raise UpstreamTimeoutError(f"URL download timed out: {url[:200]}")
    except httpx.HTTPStatusError as e:
        raise UpstreamError(
            f"URL download failed: {e.response.status_code}",
            status_code=e.response.status_code,
        )


async def process_image_pipeline(
    file_key: str, message_id: str | None, bot_name: str, url: str | None = None
) -> dict:
    """
    Image caller of the shared attachment pipeline: download -> compress
    -> upload to TOS -> return URL. Two-layer cache (``image_*`` prefix, preserved
    for prod cache continuity): upload cache (7d) + URL cache (10min).

    Download source by origin (only the source switches; compress + TOS upload
    are unchanged):
      * ``url`` set (QQ inbound, public http url) -> HTTP GET, no Lark SDK.
      * else with a message_id -> Lark message resource (type image).
      * else -> Lark ``download_image`` by image_key.
    """

    async def _download() -> bytes:
        if url:
            return await _download_image_from_url(url)
        if message_id:
            return await lark_client.download_message_resource(
                bot_name, message_id, file_key, "image"
            )
        return await lark_client.download_image(bot_name, file_key)

    # Compression is CPU-bound; offload to a thread so the event loop is free.
    async def _transform(raw: bytes) -> bytes:
        return await asyncio.to_thread(_compress_image, raw)

    return await process_attachment_pipeline(
        file_key=file_key,
        download=_download,
        transform=_transform,
        file_name=f"temp/{file_key}.jpg",
        cache_prefix="image",
    )


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
        image_bytes = await _download_image_from_url(data)
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
