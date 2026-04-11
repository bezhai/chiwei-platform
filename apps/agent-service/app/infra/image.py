"""Image pipeline client + per-request image registry.

``image_client`` — calls tool-service for process/upload/download.
``ImageRegistry`` — Redis Hash based N.png numbering per message.
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Any

import httpx
from redis.asyncio import Redis

from app.api.middleware import get_app_name, get_trace_id
from app.infra.config import settings

try:
    from inner_shared.lane_router import (
        _HAS_METRICS,
        OUTBOUND_REQUEST_DURATION,
        OUTBOUND_REQUESTS_TOTAL,
    )
except ImportError:
    _HAS_METRICS = False

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Outbound metrics helper
# ---------------------------------------------------------------------------


def _record_outbound(method: str, status: str, duration: float) -> None:
    if _HAS_METRICS:
        OUTBOUND_REQUESTS_TOTAL.labels(
            target_service="tool-service", method=method, status=status
        ).inc()
        OUTBOUND_REQUEST_DURATION.labels(
            target_service="tool-service", method=method
        ).observe(duration)


# ---------------------------------------------------------------------------
# Image process client (talks to tool-service)
# ---------------------------------------------------------------------------


def _lane_router():
    """Lazy import to avoid circular deps at module load time."""
    from app.infra.lane import lane_router

    return lane_router


class _ImageClient:
    """HTTP client for the tool-service image pipeline."""

    @property
    def _timeout(self) -> int:
        return settings.main_server_timeout

    def _auth_headers(self, *, app_name: str = "") -> dict[str, str]:
        router = _lane_router()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.inner_http_secret}",
            "X-Trace-Id": get_trace_id() or "",
            **router.get_headers(),
        }
        if app_name:
            headers["X-App-Name"] = app_name
        return headers

    def _base_url(self) -> str:
        return _lane_router().base_url("tool-service")

    # -- process (download from Lark -> TOS -> return URL) --

    async def process_image(
        self,
        file_key: str,
        message_id: str | None,
        bot_name: str | None = None,
    ) -> dict[str, str] | None:
        """Process an image, returns ``{"url": ..., "file_name": ...}``."""
        app_name = bot_name or get_app_name() or ""
        start = time.monotonic()
        status = "network_error"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._base_url()}/api/image-pipeline/process",
                    json={"message_id": message_id, "file_key": file_key},
                    headers=self._auth_headers(app_name=app_name),
                )
                status = str(resp.status_code)
                resp.raise_for_status()
                data = resp.json()
                if data.get("success") and data.get("data"):
                    result = data["data"]
                    return {
                        "url": result["url"],
                        "file_name": result.get("file_name", ""),
                    }
                logger.error(
                    "Image process failed: %s",
                    data.get("message", "unknown"),
                )
                return None
        except httpx.TimeoutException:
            logger.warning("Image process timeout: %ds", self._timeout)
            return None
        except httpx.HTTPStatusError as e:
            logger.error(
                "Image process HTTP %d: %s",
                e.response.status_code,
                e.response.text,
            )
            return None
        except Exception as e:
            logger.error("Image process call failed: %s", e)
            return None
        finally:
            _record_outbound("POST", status, time.monotonic() - start)

    # -- get_url (TOS pre-signed URL) --

    async def get_url(self, file_name: str) -> str | None:
        """Get a pre-signed URL for a TOS file_name."""
        start = time.monotonic()
        status = "network_error"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._base_url()}/api/image-pipeline/get-url",
                    json={"file_name": file_name},
                    headers=self._auth_headers(),
                )
                status = str(resp.status_code)
                resp.raise_for_status()
                data = resp.json()
                if data.get("success") and data.get("data"):
                    return data["data"]["url"]
                return None
        except Exception as e:
            logger.warning("Get image URL failed: %s - %s", file_name, e)
            return None
        finally:
            _record_outbound("POST", status, time.monotonic() - start)

    # -- upload base64 to Lark --

    async def upload_base64_image(
        self, base64_data: str, bot_name: str | None = None
    ) -> str | None:
        """Upload a base64 image to Lark, return image_key."""
        app_name = bot_name or get_app_name() or ""
        start = time.monotonic()
        status = "network_error"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._base_url()}/api/image-pipeline/upload-base64",
                    json={"base64_data": base64_data},
                    headers=self._auth_headers(app_name=app_name),
                )
                status = str(resp.status_code)
                resp.raise_for_status()
                data = resp.json()
                if data.get("success") and data.get("data"):
                    return data["data"]["image_key"]
                logger.error(
                    "Base64 upload failed: %s",
                    data.get("message", "unknown"),
                )
                return None
        except httpx.TimeoutException:
            logger.warning("Base64 upload timeout: %ds", self._timeout)
            return None
        except httpx.HTTPStatusError as e:
            logger.error(
                "Base64 upload HTTP %d: %s",
                e.response.status_code,
                e.response.text,
            )
            return None
        except Exception as e:
            logger.error("Base64 upload failed: %s", e)
            return None
        finally:
            _record_outbound("POST", status, time.monotonic() - start)

    # -- upload to TOS --

    async def upload_to_tos(self, source_type: str, data: str) -> str | None:
        """Upload image to TOS (compress + store), return pre-signed URL."""
        start = time.monotonic()
        status = "network_error"
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{self._base_url()}/api/image-pipeline/to-tos",
                    json={"source_type": source_type, "data": data},
                    headers=self._auth_headers(),
                )
                status = str(resp.status_code)
                resp.raise_for_status()
                body = resp.json()
                if body.get("success") and body.get("data"):
                    return body["data"]["url"]
                logger.error(
                    "Upload to TOS failed: %s",
                    body.get("message", "unknown"),
                )
                return None
        except httpx.TimeoutException:
            logger.warning("Upload to TOS timeout")
            return None
        except httpx.HTTPStatusError as e:
            logger.error(
                "Upload to TOS HTTP %d: %s",
                e.response.status_code,
                e.response.text,
            )
            return None
        except Exception as e:
            logger.error("Upload to TOS failed: %s", e)
            return None
        finally:
            _record_outbound("POST", status, time.monotonic() - start)

    # -- download as base64 --

    async def download_image_as_base64(
        self,
        file_key: str,
        message_id: str | None,
        bot_name: str | None = None,
    ) -> str | None:
        """Download an image and return it as a data URI."""
        try:
            result = await self.process_image(file_key, message_id, bot_name)
            if not result:
                return None

            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(result["url"])
                resp.raise_for_status()

                content_type = resp.headers.get("content-type", "image/jpeg")
                fmt = content_type.split("/")[-1].split(";")[0].lower()
                fmt = {
                    "jpeg": "jpeg",
                    "jpg": "jpeg",
                    "png": "png",
                    "gif": "gif",
                    "webp": "webp",
                    "bmp": "bmp",
                }.get(fmt, "jpeg")

                b64 = base64.b64encode(resp.content).decode()
                return f"data:image/{fmt};base64,{b64}"
        except httpx.TimeoutException:
            logger.warning("Image download timeout: %s", file_key)
            return None
        except httpx.HTTPStatusError as e:
            logger.error(
                "Image download HTTP %d: %s",
                e.response.status_code,
                file_key,
            )
            return None
        except Exception as e:
            logger.error("Image download failed: %s - %s", file_key, e)
            return None


# Module-level instance
image_client = _ImageClient()


# ---------------------------------------------------------------------------
# Image registry (per-message N.png numbering via Redis Hash)
# ---------------------------------------------------------------------------

_REGISTRY_TTL = 30 * 60  # 30 minutes

_REGISTER_LUA = """
local key = KEYS[1]
local url = ARGV[1]
local ttl = tonumber(ARGV[2])

local n = redis.call('HINCRBY', key, '__counter__', 1)
local filename = n .. '.png'
redis.call('HSET', key, filename, url)
redis.call('EXPIRE', key, ttl)
return n
"""


class ImageRegistry:
    """Per-request image registry backed by a Redis Hash.

    Redis key: ``image_registry:{message_id}``
    Fields: ``__counter__`` -> N, ``1.png`` -> url, ``2.png`` -> url, ...
    TTL: 30 minutes.
    """

    def __init__(self, message_id: str, redis: Redis) -> None:
        self.message_id = message_id
        self._key = f"image_registry:{message_id}"
        self._redis = redis

    async def register(self, tos_url: str) -> str:
        """Register a TOS URL, return filename like ``3.png``."""
        n = await self._redis.eval(_REGISTER_LUA, 1, self._key, tos_url, _REGISTRY_TTL)
        return f"{n}.png"

    async def register_batch(self, urls: list[str]) -> list[str]:
        """Register multiple URLs via pipeline."""
        if not urls:
            return []
        pipe = self._redis.pipeline(transaction=False)
        for url in urls:
            pipe.eval(_REGISTER_LUA, 1, self._key, url, _REGISTRY_TTL)
        results = await pipe.execute()
        return [f"{n}.png" for n in results]

    async def resolve(self, filename: str) -> str | None:
        """Resolve a filename to its TOS URL."""
        return await self._redis.hget(self._key, filename)

    async def resolve_all(self) -> dict[str, str]:
        """Get all filename -> URL mappings (excludes __counter__)."""
        data: dict[str, Any] = await self._redis.hgetall(self._key)
        data.pop("__counter__", None)
        return data
