"""Web search (search, page contents, image search) and the reranker, at the HTTP boundary.

Two hosts served on :mod:`tests.replay.harness.network`, where the replay's settings point the
providers (:data:`SETTINGS`): You Search as the search provider (Google CSE unset, so it is never
chosen) and SiliconFlow as the reranker. Everything above runs for real:
``app.capabilities.web_search`` and ``app.capabilities.image_search`` (``HTTPClient``, the request
parameters, response parsing), ``search_web``'s fetch-chunk-rerank pipeline, and the tools.

* ``GET  web-search/v1/search``   answers :meth:`Web.results` for the ``query`` parameter.
* ``POST web-search/v1/contents`` answers :meth:`Web.page` for the one url in ``urls``.
* ``GET  web-search/images``      answers :meth:`Web.images` for the ``q`` parameter.
* ``POST rerank/v1/rerank``       answers :meth:`Web.rerank` for ``query``: one score per document
  sent, in the order sent (a page shorter than 2000 characters is one document; pages without
  contents send none). The answer lists them best first, cut to ``top_n``, as SiliconFlow does.
  Scores for a different number of documents than were sent are refused.

A request the scenario gave no answer for is refused. Each request goes onto the effects
timeline with what it asked (query parameters or JSON body)::

    {"http": "GET web-search/v1/search", "request": {"query": "...", "count": "5", ...},
     "status": 200}
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import httpx

from tests.replay.harness.network import Network, json_response

SEARCH_HOST = "web-search.replay"
RERANK_HOST = "rerank.replay"

SETTINGS = {
    "you_search_host": f"https://{SEARCH_HOST}",
    "you_search_api_key": "replay-key",
    "google_search_host": None,
    "google_search_api_key": None,
    "google_search_cx": None,
    "siliconflow_base_url": f"https://{RERANK_HOST}/v1",
    "siliconflow_api_key": "replay-key",
}


@dataclass(frozen=True)
class Hit:
    """One search result as the provider returns it."""

    title: str
    url: str
    snippet: str = ""


@dataclass(frozen=True)
class FoundImage:
    """One image-search result as the provider returns it."""

    image_url: str
    title: str = ""
    source_url: str = ""


class Web:
    def __init__(self, network: Network) -> None:
        self._network = network
        self._searches = network.answers("web search")
        self._pages = network.answers("web page contents")
        self._images = network.answers("image search")
        self._rerank = network.answers("rerank")
        network.serve(SEARCH_HOST, self._search_host)
        network.serve(RERANK_HOST, self._rerank_host)

    # ------------------------------------------------------------------ scenario API

    def results(self, query: str, hits: list[Hit]) -> None:
        """What a web search for ``query`` returns."""
        self._searches.give(query, list(hits))

    def page(self, url: str, text: str) -> None:
        """What reading the page at ``url`` returns (its markdown)."""
        self._pages.give(url, text)

    def images(self, query: str, found: list[FoundImage]) -> None:
        """What an image search for ``query`` returns."""
        self._images.give(query, list(found))

    def rerank(self, query: str, scores: list[float]) -> None:
        """The reranker's score for each document sent with ``query``, in the order sent."""
        self._rerank.give(query, list(scores))

    # ------------------------------------------------------------------ the two hosts

    def _record(self, request: httpx.Request, asked: object) -> None:
        host = "web-search" if request.url.host == SEARCH_HOST else "rerank"
        self._network.effects.append(
            {
                "http": f"{request.method} {host}{request.url.path}",
                "request": asked,
                "status": 200,
            }
        )

    async def _search_host(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        params = dict(request.url.params)
        if method == "GET" and path == "/v1/search":
            hits = self._searches.take(params.get("query"), request)
            self._record(request, params)
            web = [
                {"title": h.title, "url": h.url, "description": h.snippet} for h in hits
            ]
            return json_response(request, {"results": {"web": web}})
        if method == "POST" and path == "/v1/contents":
            body = json.loads(request.content or b"{}")
            urls = body.get("urls") or []
            if len(urls) != 1:
                self._network.refuse(request, f"contents of {len(urls)} urls at once")
            text = self._pages.take(urls[0], request)
            self._record(request, body)
            return json_response(
                request, {"contents": [{"url": urls[0], "markdown": text}]}
            )
        if method == "GET" and path == "/images":
            found = self._images.take(params.get("q"), request)
            self._record(request, params)
            results = [
                {"image_url": f.image_url, "title": f.title, "source_url": f.source_url}
                for f in found
            ]
            return json_response(request, {"images": {"results": results}})
        self._network.refuse(request, "web search path not served")

    async def _rerank_host(self, request: httpx.Request) -> httpx.Response:
        if request.method != "POST" or request.url.path != "/v1/rerank":
            self._network.refuse(request, "rerank path not served")
        body = json.loads(request.content or b"{}")
        documents = body.get("documents") or []
        scores = self._rerank.take(body.get("query"), request)
        if len(scores) != len(documents):
            self._network.refuse(
                request,
                f"{len(documents)} documents sent, the scenario scored {len(scores)}",
            )
        self._record(request, body)
        ranked = sorted(enumerate(scores), key=lambda pair: -pair[1])[
            : body.get("top_n")
        ]
        results = [{"index": i, "relevance_score": score} for i, score in ranked]
        return json_response(request, {"results": results})
