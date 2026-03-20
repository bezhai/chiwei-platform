import sys
from types import ModuleType, SimpleNamespace

import pytest

_ark_module = ModuleType("volcenginesdkarkruntime")
_ark_module.AsyncArk = object
sys.modules.setdefault("volcenginesdkarkruntime", _ark_module)

_ark_types_module = ModuleType("volcenginesdkarkruntime.types")
sys.modules.setdefault("volcenginesdkarkruntime.types", _ark_types_module)

_ark_multimodal_module = ModuleType("volcenginesdkarkruntime.types.multimodal_embedding")
_ark_multimodal_module.EmbeddingInputParam = dict
sys.modules.setdefault(
    "volcenginesdkarkruntime.types.multimodal_embedding",
    _ark_multimodal_module,
)

from app.agents.core.context import AgentContext, MediaContext, MessageContext
from app.agents.tools.search.google_lens import (
    _build_params,
    _extract_response,
    _resolve_image_input,
    search_by_image,
)
from app.config import settings

pytestmark = pytest.mark.unit


class FakeRegistry:
    def __init__(self, mapping: dict[str, str]):
        self.mapping = mapping

    async def resolve(self, filename: str) -> str | None:
        return self.mapping.get(filename)


class FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class FakeAsyncClient:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def get(self, url: str, params: dict):
        self.calls.append((url, params))
        return FakeResponse(self.payload)


def _make_runtime(registry: FakeRegistry | None = None):
    context = AgentContext(
        message=MessageContext(message_id="msg_1", chat_id="chat_1"),
        media=MediaContext(registry=registry),
    )
    return SimpleNamespace(context=context)


def test_build_params_includes_serpapi_fields(monkeypatch):
    monkeypatch.setattr(settings, "serpapi_api_key", "test-key")

    params = _build_params(
        image_url="https://example.com/test.png",
        search_type="visual_matches",
        q="red version",
        hl="zh-CN",
        country="CN",
    )

    assert params == {
        "engine": "google_lens",
        "url": "https://example.com/test.png",
        "type": "visual_matches",
        "hl": "zh-CN",
        "country": "cn",
        "api_key": "test-key",
        "q": "red version",
    }


@pytest.mark.asyncio
async def test_resolve_image_input_supports_registry_reference(monkeypatch):
    registry = FakeRegistry({"3.png": "https://tos.example.com/3.png"})
    monkeypatch.setattr(
        "app.agents.tools.search.google_lens.get_runtime",
        lambda _schema: _make_runtime(registry),
    )

    resolved = await _resolve_image_input("@3.png")

    assert resolved == "https://tos.example.com/3.png"


def test_extract_response_summarizes_supported_sections():
    payload = {
        "knowledge_graph": [
            {"title": "Illustration", "description": "Likely reposted fanart."}
        ],
        "visual_matches": [
            {
                "title": "Mirror upload",
                "source": "example.com",
                "link": "https://example.com/mirror",
                "snippet": "Same artwork with cropped border.",
            }
        ],
        "exact_matches": [
            {
                "title": "Original post",
                "source": "artist.site",
                "link": "https://artist.site/original",
            }
        ],
    }

    result = _extract_response(payload)

    assert result["about_this_image"][0]["title"] == "Illustration"
    assert result["visual_matches"][0]["title"] == "Mirror upload"
    assert result["exact_matches"][0]["title"] == "Original post"
    assert "背景信息" in result["best_summary"]
    assert "相似图片" in result["best_summary"]
    assert "原图线索" in result["best_summary"]


@pytest.mark.asyncio
async def test_search_by_image_calls_serpapi_and_returns_normalized_result(monkeypatch):
    monkeypatch.setattr(settings, "serpapi_api_key", "test-key")
    monkeypatch.setattr(settings, "serpapi_google_lens_host", "https://serpapi.test/search")
    monkeypatch.setattr(
        "app.agents.tools.search.google_lens.get_runtime",
        lambda _schema: _make_runtime(None),
    )

    payload = {
        "about_this_image": [
            {"title": "Artwork source", "description": "Found on a blog."}
        ],
        "visual_matches": [
            {
                "title": "Repost",
                "source": "blog.example",
                "link": "https://blog.example/repost",
            }
        ],
    }
    fake_client = FakeAsyncClient(payload)
    monkeypatch.setattr(
        "app.agents.tools.search.google_lens.httpx.AsyncClient",
        lambda timeout: fake_client,
    )

    result = await search_by_image.ainvoke(
        {
            "image": "https://example.com/input.png",
            "search_type": "about_this_image",
            "hl": "en",
            "country": "US",
        }
    )

    assert fake_client.calls == [
        (
            "https://serpapi.test/search",
            {
                "engine": "google_lens",
                "url": "https://example.com/input.png",
                "type": "about_this_image",
                "hl": "en",
                "country": "us",
                "api_key": "test-key",
            },
        )
    ]
    assert isinstance(result, dict)
    assert result["about_this_image"][0]["title"] == "Artwork source"
    assert "背景信息" in result["best_summary"]
