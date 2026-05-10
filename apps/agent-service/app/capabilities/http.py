"""HTTPClient — lane-aware httpx adapter with method-aware retry (Phase 7d Gap 16).

Retry decision matrix (spec §4.2.1):

- ConnectError / ConnectTimeout / DNS / TLS: all methods retry
  (the request never reached the server, so it is safe to replay).
- 429 Too Many Requests: all methods retry (rate-limit politeness).
- ReadTimeout / WriteTimeout / RemoteProtocolError / 5xx: GET/HEAD only.
  POST is NOT retried because the server may already have executed the
  side-effect.
- POST + ``idempotency_key``: an ``Idempotency-Key`` header is injected and
  the call is allowed to retry on connect-stage errors and 429, but it still
  does NOT retry on ReadTimeout/5xx — the server is expected to dedupe via
  the key on its own next time, not because we replayed.

If ``service`` is set, non-absolute paths route through
``lane_router.base_url(service)`` (which reads the current lane from its
own contextvar). Lane and trace headers are injected on every request.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.api.middleware import trace_id_var
from app.infra.lane import lane_router

logger = logging.getLogger(__name__)


class HTTPClient:
    """Lane-aware HTTP adapter with method-aware retry."""

    def __init__(
        self,
        service: str | None = None,
        *,
        timeout: float = 30.0,
        retries: int = 3,
        retry_post: int = 0,
        retry_backoff: float = 0.5,
        retry_on_status_get: frozenset[int] = frozenset({429, 500, 502, 503, 504}),
        retry_on_status_post: frozenset[int] = frozenset({429}),
    ) -> None:
        self._service = service
        self._client = httpx.AsyncClient(timeout=timeout)
        self._retries_get = retries
        self._retries_post = retry_post
        self._backoff = retry_backoff
        self._status_get = retry_on_status_get
        self._status_post = retry_on_status_post

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        h: dict[str, str] = dict(extra) if extra else {}
        h.update(lane_router.get_headers())
        if tid := trace_id_var.get():
            h["X-Trace-Id"] = tid
        return h

    def _url(self, path: str) -> str:
        if not self._service or path.startswith(("http://", "https://")):
            return path
        return lane_router.base_url(self._service) + path

    async def get(self, path: str, **kw: Any) -> httpx.Response:
        return await self._request("GET", path, **kw)

    async def post(
        self,
        path: str,
        *,
        idempotency_key: str | None = None,
        **kw: Any,
    ) -> httpx.Response:
        if idempotency_key:
            headers = kw.pop("headers", None) or {}
            headers = dict(headers)
            headers["Idempotency-Key"] = idempotency_key
            kw["headers"] = headers
        return await self._request(
            "POST", path, _idempotency=bool(idempotency_key), **kw
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        _idempotency: bool = False,
        **kw: Any,
    ) -> httpx.Response:
        is_get_like = method in ("GET", "HEAD")
        if is_get_like:
            retries = self._retries_get
        else:
            retries = self._retries_post if _idempotency else 0
        retry_status = self._status_get if is_get_like else self._status_post

        url = self._url(path)
        headers = self._headers(kw.pop("headers", None))

        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = await self._client.request(method, url, headers=headers, **kw)
                if resp.status_code in retry_status and attempt < retries:
                    delay = self._backoff * (2**attempt)
                    logger.warning(
                        "HTTP %s %s status=%d, retrying in %.2fs (%d/%d)",
                        method,
                        url,
                        resp.status_code,
                        delay,
                        attempt + 1,
                        retries,
                    )
                    await asyncio.sleep(delay)
                    continue
                return resp
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                # Request never reached the server — safe to retry for any method.
                last_exc = e
                if attempt >= retries:
                    raise
                delay = self._backoff * (2**attempt)
                logger.warning(
                    "HTTP %s %s connect error %s, retrying in %.2fs (%d/%d)",
                    method,
                    url,
                    type(e).__name__,
                    delay,
                    attempt + 1,
                    retries,
                )
                await asyncio.sleep(delay)
            except (
                httpx.ReadTimeout,
                httpx.WriteTimeout,
                httpx.RemoteProtocolError,
            ) as e:
                # Request was sent — only retry idempotent (GET/HEAD) methods.
                # Even POST + idempotency_key does NOT retry here; the server
                # is responsible for deduping if the client replays later.
                if not is_get_like:
                    raise
                last_exc = e
                if attempt >= retries:
                    raise
                delay = self._backoff * (2**attempt)
                logger.warning(
                    "HTTP %s %s read/write timeout %s, retrying in %.2fs (%d/%d)",
                    method,
                    url,
                    type(e).__name__,
                    delay,
                    attempt + 1,
                    retries,
                )
                await asyncio.sleep(delay)

        # Should be unreachable: the loop either returns or raises.
        if last_exc:
            raise last_exc
        raise RuntimeError("HTTPClient retry loop exited unexpectedly")

    async def close(self) -> None:
        await self._client.aclose()
