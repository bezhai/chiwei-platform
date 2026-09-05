"""Image search capability — Phase 7d Gap 16.

Searching is all this does: it returns structured ``ImageHit`` records and
nothing else. Putting the hits into object storage — and into her own record of
what she made (``app.living.pictures``) — is the caller's job. The tool that
used to call this was deleted along with the image registry it depended on;
this capability outlived it because the search itself was never the broken part.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.capabilities.http import HTTPClient
from app.infra.config import settings

_CLIENT = HTTPClient(timeout=15.0)


@dataclass
class ImageHit:
    image_url: str
    title: str
    source_url: str


async def image_search(query: str, *, count: int = 5) -> list[ImageHit]:
    """Search images via You Images API; empty list if not configured.

    contract-allowed empty list (§4.8): "config not provisioned" is a
    deployment-time outcome, not a runtime capability failure. Transport
    errors propagate from ``HTTPClient`` as ``httpx`` exceptions (caller
    decides whether to wrap as ``CapabilityTimeout`` / ``CapabilityCallFailed``).
    """
    if not settings.you_search_host or not settings.you_search_api_key:
        return []
    url = f"{settings.you_search_host}/images"
    headers = {"X-API-Key": settings.you_search_api_key}
    params = {"q": query}
    resp = await _CLIENT.get(url, params=params, headers=headers)
    resp.raise_for_status()
    data = resp.json()

    raw = data.get("images", [])
    if isinstance(raw, dict):
        raw = raw.get("results", [])
    hits: list[ImageHit] = []
    for img in raw[:count]:
        image_url = img.get("image_url") or img.get("url", "")
        if not image_url:
            continue
        hits.append(
            ImageHit(
                image_url=image_url,
                title=img.get("title", ""),
                source_url=img.get("source_url", ""),
            )
        )
    return hits
