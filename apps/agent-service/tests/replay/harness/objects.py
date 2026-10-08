"""Object storage and tool-service's image pipeline, at the HTTP boundary.

Interception point: ``httpx.AsyncHTTPTransport.handle_async_request``, the last step before a
request goes out on the network. Everything above it runs for real: ``image_client`` (lane
headers, the ``{success, data}`` envelope, its error handling), the reading round's byte fetch,
the phone's "is this picture really there" check. Two hosts are served; a request to any other
host goes out exactly as it did before this boundary existed.

* **tool-service** (``http://tool-service[:port]``, whatever ``lane_router`` resolves).
  ``POST /api/image-pipeline/get-url`` signs a ``file_name`` into a store address valid for 1.5
  hours (tool-service's ``tos_client.get_file_url`` expiry). Signing is pure computation there
  too, so it signs any name, stored or not; whether the object exists only shows when it is
  fetched. Any other tool-service path raises :class:`UnservedRequest`, a ``BaseException`` so
  that ``image_client``'s ``except Exception`` cannot turn a missing boundary into a quiet
  ``None``: add the path here instead.
* **the object store** (``https://object-store.replay/<file_name>?signed-until=<CST time>``).
  ``GET`` answers what the scenario put there (:meth:`ObjectStore.put`) with its content type;
  ``404`` for a name nothing was put under; ``403`` once the signature has run out (the frozen
  clock is past ``signed-until``).

Every request to those two hosts goes onto the step's effects timeline, in order with commits
and publishes::

    {"http": "POST tool-service/api/image-pipeline/get-url",
     "lane": "coe-replay", "request": {"file_name": "files/k1"}, "status": 200}
    {"http": "GET object-store/files/k1", "status": 200,
     "served": "text/plain; charset=utf-8, 2001 bytes, sha256:1a2b3c4d5e6f"}

Bytes never appear in a baseline, only ``<content type>, <length> bytes, sha256:<12 hex>``.

**Model requests.** The model adapters are replaced by the scripted model, so the Gemini
adapter's own download of every image url (``_fetch_remote_image``, which runs on every call,
history included) never runs in a replay. What it would have sent is still part of the request,
so the model boundary annotates each image block whose url points into the store with what
fetching it at the moment of the call returns: ``"fetched": "image/png, 67 bytes,
sha256:..."``, or ``"403: signed until 15:30:00, fetched at 15:40:00"`` / ``"404: no such
object"``. That annotation is a read of the store, not an effect, and is not on the timeline.
``data:`` URIs are described by the baseline normaliser (:mod:`tests.replay.harness.baseline`).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote, unquote

import httpx

from app.infra.cst_time import CST, now_cst

TOOL_SERVICE_HOST = "tool-service"
STORE_HOST = "object-store.replay"

# tool-service signs for 1.5 hours (``tos_client.get_file_url``: ``expires=int(1.5*60*60)``).
SIGNATURE_LIFETIME = timedelta(minutes=90)

_SIGNED_UNTIL = "signed-until"
_SIGNED_FORMAT = "%Y-%m-%dT%H:%M:%S"


class UnservedRequest(BaseException):
    """A request to a served host that the harness does not answer. A ``BaseException``: the
    code's own ``except Exception`` must not turn a missing boundary into a quiet failure."""


@dataclass(frozen=True)
class StoredObject:
    data: bytes
    content_type: str

    def describe(self) -> str:
        digest = hashlib.sha256(self.data).hexdigest()[:12]
        return f"{self.content_type}, {len(self.data)} bytes, sha256:{digest}"


class ObjectStore:
    """What the object store holds, and the two hosts that serve it."""

    def __init__(self, effects) -> None:
        self._effects = effects
        self._objects: dict[str, StoredObject] = {}

    # ------------------------------------------------------------------ scenario API

    def put(self, file_name: str, data: bytes, content_type: str) -> None:
        """An object the store holds under ``file_name`` (an upload that happened before the
        scenario; not recorded)."""
        self._objects[file_name] = StoredObject(bytes(data), content_type)

    # ------------------------------------------------------------------ signing / fetching

    def sign(self, file_name: str) -> str:
        until = (now_cst() + SIGNATURE_LIFETIME).strftime(_SIGNED_FORMAT)
        return f"https://{STORE_HOST}/{quote(file_name)}?{_SIGNED_UNTIL}={until}"

    def _look_up(self, url: httpx.URL) -> tuple[int, str, StoredObject | None]:
        """``(status, file_name, object)`` for a GET of ``url`` right now."""
        file_name = unquote(url.path.lstrip("/"))
        until = url.params.get(_SIGNED_UNTIL)
        if until is None or now_cst() > _signed_until(until):
            return 403, file_name, None
        stored = self._objects.get(file_name)
        if stored is None:
            return 404, file_name, None
        return 200, file_name, stored

    def describe(self, url: str) -> str | None:
        """What fetching ``url`` returns right now, or ``None`` when it is not a store address."""
        try:
            parsed = httpx.URL(url)
        except (httpx.InvalidURL, TypeError):
            return None
        if parsed.host != STORE_HOST:
            return None
        status, _, stored = self._look_up(parsed)
        if stored is not None:
            return stored.describe()
        if status == 404:
            return "404: no such object"
        until = parsed.params.get(_SIGNED_UNTIL, "?")
        return (
            f"403: signed until {until[-8:]}, fetched at "
            f"{now_cst().strftime('%H:%M:%S')}"
        )

    def annotate(self, message: dict[str, Any]) -> dict[str, Any]:
        """A recorded model message, each store-addressed image block annotated with what
        fetching it now returns. Messages without such a block come back unchanged."""
        content = message.get("content")
        if not isinstance(content, list):
            return message
        blocks = []
        changed = False
        for block in content:
            url = _image_url_of(block)
            fetched = self.describe(url) if url is not None else None
            if fetched is None:
                blocks.append(block)
                continue
            blocks.append({**block, "fetched": fetched})
            changed = True
        return {**message, "content": blocks} if changed else message

    # ------------------------------------------------------------------ the two hosts

    async def _tool_service(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content or b"{}")
        if request.method != "POST" or path != "/api/image-pipeline/get-url":
            raise UnservedRequest(
                f"replay: tool-service {request.method} {path} is not served; add it to "
                f"tests/replay/harness/objects.py"
            )
        payload = {"success": True, "data": {"url": self.sign(body["file_name"])}}
        self._effects.append(
            {
                "http": f"POST {TOOL_SERVICE_HOST}{path}",
                "lane": request.headers.get("x-ctx-lane"),
                "request": body,
                "status": 200,
            }
        )
        return httpx.Response(200, json=payload, request=request)

    def _store(self, request: httpx.Request) -> httpx.Response:
        if request.method != "GET":
            raise UnservedRequest(
                f"replay: object store {request.method} is not served; add it to "
                f"tests/replay/harness/objects.py"
            )
        status, file_name, stored = self._look_up(request.url)
        event: dict[str, Any] = {
            "http": f"GET object-store/{file_name}",
            "status": status,
        }
        if stored is not None:
            event["served"] = stored.describe()
        self._effects.append(event)
        if stored is None:
            return httpx.Response(status, request=request)
        return httpx.Response(
            200,
            content=stored.data,
            headers={"content-type": stored.content_type},
            request=request,
        )

    def install(self, monkeypatch) -> None:
        real = httpx.AsyncHTTPTransport.handle_async_request
        store = self

        async def handle(transport, request: httpx.Request) -> httpx.Response:
            host = request.url.host
            if host == TOOL_SERVICE_HOST:
                store._effects.alive()
                return await store._tool_service(request)
            if host == STORE_HOST:
                store._effects.alive()
                return store._store(request)
            return await real(transport, request)

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)


def _signed_until(value: str) -> datetime:
    return datetime.strptime(value, _SIGNED_FORMAT).replace(tzinfo=CST)


def _image_url_of(block: Any) -> str | None:
    """The address an image block points at (``image`` or OpenAI-style ``image_url``)."""
    if not isinstance(block, dict):
        return None
    if block.get("type") == "image":
        url = block.get("url")
    elif block.get("type") == "image_url":
        url = (block.get("image_url") or {}).get("url")
    else:
        return None
    return url if isinstance(url, str) else None
