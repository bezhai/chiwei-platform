"""The image model she draws with, at the HTTP boundary.

``app.agent.image_gen.generate_image`` resolves a model id (``resolve_model_info``, a database
lookup in production) and dispatches on the provider's client type. In a replay the image module's
resolution answers every model id with an OpenAI-compatible provider at ``image-model.replay``
(:func:`install`), the way the model boundary answers every chat model id with the scripted
client. From there everything runs for real: the dispatch, the OpenAI SDK building and sending
``POST /v1/images/generations``, the ``data:`` URI made of the answer.

The host is served on :mod:`tests.replay.harness.network`: the answer is the picture the scenario
gave for the request's ``prompt`` (:meth:`ImageModel.draws`), as ``b64_json``; any other prompt is
refused. The request goes onto the effects timeline::

    {"http": "POST image-model/v1/images/generations",
     "request": {"model": "generate-image-high-model", "prompt": "...", "size": "1024x1024", ...},
     "status": 200, "served": "71 bytes, sha256:1a2b3c4d5e6f"}
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

import httpx

from app.infra.cst_time import now_cst
from tests.replay.harness.network import Network, json_response

HOST = "image-model.replay"


class ImageModel:
    def __init__(self, network: Network) -> None:
        self._network = network
        self._pictures = network.answers("image model")
        network.serve(HOST, self._serve)

    def draws(self, prompt: str, picture: bytes) -> None:
        """What the image model draws for ``prompt``."""
        self._pictures.give(prompt, bytes(picture))

    async def _serve(self, request: httpx.Request) -> httpx.Response:
        if request.method != "POST" or request.url.path != "/v1/images/generations":
            self._network.refuse(request, "image model path not served")
        body = json.loads(request.content or b"{}")
        picture = self._pictures.take(body.get("prompt"), request)
        digest = hashlib.sha256(picture).hexdigest()[:12]
        self._network.effects.append(
            {
                "http": f"POST image-model{request.url.path}",
                "request": body,
                "status": 200,
                "served": f"{len(picture)} bytes, sha256:{digest}",
            }
        )
        answer = {
            "created": int(now_cst().timestamp()),
            "data": [{"b64_json": base64.b64encode(picture).decode()}],
        }
        return json_response(request, answer)


def install(monkeypatch) -> None:
    import app.agent.image_gen as image_gen

    async def resolve(model_id: str, **_: Any) -> dict[str, Any]:
        return {
            "client_type": "openai",
            "model_name": model_id,
            "api_key": "replay-key",
            "base_url": f"https://{HOST}/v1",
            "use_proxy": False,
        }

    monkeypatch.setattr(image_gen, "resolve_model_info", resolve)
