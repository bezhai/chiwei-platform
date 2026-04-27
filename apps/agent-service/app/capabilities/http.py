"""HTTPClient — lane-aware httpx adapter.

- If ``service`` is set, non-absolute paths are routed through
  ``lane_router.base_url(service)`` (which reads the current lane from its
  own contextvar).
- Lane and trace headers are injected on every request.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.api.middleware import trace_id_var
from app.infra.lane import lane_router


class HTTPClient:
    """Lane-aware HTTP adapter."""

    def __init__(self, service: str | None = None, timeout: float = 30.0) -> None:
        self._service = service
        self._client = httpx.AsyncClient(timeout=timeout)

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
        headers = self._headers(kw.pop("headers", None))
        return await self._client.get(self._url(path), headers=headers, **kw)

    async def post(self, path: str, **kw: Any) -> httpx.Response:
        headers = self._headers(kw.pop("headers", None))
        return await self._client.post(self._url(path), headers=headers, **kw)

    async def close(self) -> None:
        await self._client.aclose()
