"""Object storage and tool-service's image pipeline, at the HTTP boundary.

Two hosts served on :mod:`tests.replay.harness.network`. Everything above runs for real:
``image_client`` (lane headers, the ``{success, data}`` envelope, its error handling), the reading
round's byte fetch, the phone's "is this picture really there" check, ``upload_image``.

* **tool-service** (``http://tool-service[:port]``, whatever ``lane_router`` resolves).

  * ``POST /api/image-pipeline/get-url`` signs a ``file_name`` into a store address valid for
    1.5 hours (tool-service's ``tos_client.get_file_url`` expiry). Signing is pure computation
    there too, so it signs any name, stored or not; whether the object exists only shows when it
    is fetched.
  * ``POST /api/image-pipeline/to-tos`` stores an image and answers ``{url, file_name}``:
    ``source_type: "base64"`` stores the decoded bytes; ``source_type: "url"`` stores what that
    address serves on the web, which the scenario gives (:meth:`ObjectStore.on_the_web`; an
    address it did not give is refused). The name is tool-service's shape,
    ``temp/tos_<md5 of the bytes, 16 hex>_<8 hex>.jpg``, with the upload's ordinal as the last
    part where tool-service has 8 random hex digits. tool-service re-encodes every upload as
    JPEG; the store here keeps the bytes and content type it was given.

  Any other tool-service path is refused (:class:`~tests.replay.harness.network.UnservedRequest`):
  add it here.
* **the object store** (``https://object-store.replay/<file_name>?signed-until=<CST time>``).
  ``GET`` answers what the scenario put there (:meth:`ObjectStore.put`) or a ``to-tos`` stored,
  with its content type; ``404`` for a name nothing was stored under; ``403`` once the signature
  has run out (the frozen clock is past ``signed-until``).

Every request to those two hosts goes onto the step's effects timeline, in order with commits
and publishes::

    {"http": "POST tool-service/api/image-pipeline/get-url",
     "lane": "coe-replay", "request": {"file_name": "files/k1"}, "status": 200}
    {"http": "POST tool-service/api/image-pipeline/to-tos", "lane": "coe-replay",
     "request": {"source_type": "url", "data": "https://..."}, "status": 200,
     "stored": "temp/tos_...jpg: image/png, 69 bytes, sha256:1a2b3c4d5e6f"}
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

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote, unquote

import httpx

from app.infra.cst_time import CST, now_cst
from tests.replay.harness.network import Network, json_response

TOOL_SERVICE_HOST = "tool-service"
STORE_HOST = "object-store.replay"

# tool-service signs for 1.5 hours (``tos_client.get_file_url``: ``expires=int(1.5*60*60)``).
SIGNATURE_LIFETIME = timedelta(minutes=90)

_SIGNED_UNTIL = "signed-until"
_SIGNED_FORMAT = "%Y-%m-%dT%H:%M:%S"

_GET_URL = "/api/image-pipeline/get-url"
_TO_TOS = "/api/image-pipeline/to-tos"


@dataclass(frozen=True)
class StoredObject:
    data: bytes
    content_type: str

    def describe(self) -> str:
        digest = hashlib.sha256(self.data).hexdigest()[:12]
        return f"{self.content_type}, {len(self.data)} bytes, sha256:{digest}"


class ObjectStore:
    """What the object store holds, and the two hosts that serve it."""

    def __init__(self, network: Network) -> None:
        self._network = network
        self._objects: dict[str, StoredObject] = {}
        # What addresses on the web serve, for tool-service's ``to-tos`` of a url.
        self._web = network.answers("the web (tool-service to-tos)")
        self._uploads = 0
        network.serve(TOOL_SERVICE_HOST, self._tool_service)
        network.serve(STORE_HOST, self._store)

    # ------------------------------------------------------------------ scenario API

    def put(self, file_name: str, data: bytes, content_type: str) -> None:
        """An object the store holds under ``file_name`` (an upload that happened before the
        scenario; not recorded)."""
        self._objects[file_name] = StoredObject(bytes(data), content_type)

    def on_the_web(self, url: str, data: bytes, content_type: str) -> None:
        """What ``url`` serves on the web, when tool-service is asked to store it (``to-tos``
        with ``source_type: "url"``: a picture she found online)."""
        self._web.give(url, StoredObject(bytes(data), content_type))

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
        if request.method == "POST" and path == _GET_URL:
            body = json.loads(request.content or b"{}")
            payload = {"success": True, "data": {"url": self.sign(body["file_name"])}}
            self._network.effects.append(
                {
                    "http": f"POST {TOOL_SERVICE_HOST}{path}",
                    "lane": request.headers.get("x-ctx-lane"),
                    "request": body,
                    "status": 200,
                }
            )
            return json_response(request, payload)
        if request.method == "POST" and path == _TO_TOS:
            return self._to_tos(request, json.loads(request.content or b"{}"))
        self._network.refuse(
            request,
            "tool-service path not served; add it to tests/replay/harness/objects.py",
        )

    def _to_tos(self, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        source_type, data = body.get("source_type"), body.get("data")
        if source_type == "base64":
            image = _decoded(data, request, self._network)
        elif source_type == "url":
            image = self._web.take(data, request)
        else:
            self._network.refuse(request, f"to-tos of source_type {source_type!r}")
        self._uploads += 1
        file_id = hashlib.md5(image.data).hexdigest()[:16]
        file_name = f"temp/tos_{file_id}_{self._uploads:08x}.jpg"
        self._objects[file_name] = image
        self._network.effects.append(
            {
                "http": f"POST {TOOL_SERVICE_HOST}{request.url.path}",
                "lane": request.headers.get("x-ctx-lane"),
                "request": body,
                "status": 200,
                "stored": f"{file_name}: {image.describe()}",
            }
        )
        payload = {
            "success": True,
            "data": {"url": self.sign(file_name), "file_name": file_name},
            "message": "ok",
        }
        return json_response(request, payload)

    async def _store(self, request: httpx.Request) -> httpx.Response:
        if request.method != "GET":
            self._network.refuse(
                request,
                "object store method not served; add it to tests/replay/harness/objects.py",
            )
        status, file_name, stored = self._look_up(request.url)
        event: dict[str, Any] = {
            "http": f"GET object-store/{file_name}",
            "status": status,
        }
        if stored is not None:
            event["served"] = stored.describe()
        self._network.effects.append(event)
        if stored is None:
            return httpx.Response(status, request=request)
        return httpx.Response(
            200,
            content=stored.data,
            headers={"content-type": stored.content_type},
            request=request,
        )


def _decoded(data: Any, request: httpx.Request, network: Network) -> StoredObject:
    """The bytes of a ``to-tos`` base64 upload: a ``data:`` URI or bare base64, as tool-service
    takes it (everything after the first comma)."""
    if not isinstance(data, str):
        network.refuse(request, "to-tos base64 upload without data")
    head, payload = data.split(",", 1) if "," in data else ("", data)
    content_type = (
        head.removeprefix("data:").split(";")[0] or "application/octet-stream"
    )
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        network.refuse(request, "to-tos base64 upload that does not decode")
    return StoredObject(raw, content_type)


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
