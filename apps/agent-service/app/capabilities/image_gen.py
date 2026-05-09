"""Image-generation transport helper — Phase 7d Gap 16.

The image-gen path passes a custom ``httpx.AsyncClient`` into the OpenAI SDK
(``AsyncOpenAI(http_client=...)``) so that requests go through a forward proxy.
That httpx client is *not* used to make HTTP calls directly — it is a transport
the SDK owns. Wrapping it through ``HTTPClient`` would change retry semantics
the SDK already implements, so we expose a tiny capability that returns the
proxy-configured client. This keeps ``app/agent/image_gen.py`` free of any
direct httpx import.
"""

from __future__ import annotations

from typing import Any

import httpx


def proxy_http_client(proxy_url: str) -> Any:
    """Return an ``httpx.AsyncClient`` configured to forward through ``proxy_url``.

    Returned as ``Any`` to discourage direct use beyond passing into SDK
    constructors that expect an ``httpx.AsyncClient`` (e.g. ``AsyncOpenAI``).
    """
    return httpx.AsyncClient(proxy=proxy_url)
