"""Tests for app.agent.tools.search — web search, reranking, webpage reading."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# ---------------------------------------------------------------------------
# _chunk_text
# ---------------------------------------------------------------------------


class TestChunkText:
    def test_short_text_single_chunk(self):
        from app.agent.tools.search import _chunk_text

        result = _chunk_text("hello world", chunk_size=100)
        assert result == ["hello world"]

    def test_empty_text(self):
        from app.agent.tools.search import _chunk_text

        assert _chunk_text("") == []
        assert _chunk_text("   ") == []

    def test_splits_at_paragraph_boundary(self):
        from app.agent.tools.search import _chunk_text

        text = "A" * 500 + "\n\n" + "B" * 500
        chunks = _chunk_text(text, chunk_size=600, overlap=50)
        assert len(chunks) >= 2
        # First chunk should end near the paragraph boundary
        assert "A" in chunks[0]

    def test_overlap_produces_shared_content(self):
        from app.agent.tools.search import _chunk_text

        text = "\n".join(f"Line {i}" for i in range(100))
        chunks = _chunk_text(text, chunk_size=200, overlap=50)
        assert len(chunks) > 1
        # With overlap, adjacent chunks should share some text
        for i in range(len(chunks) - 1):
            # The tail of chunk[i] should appear in head of chunk[i+1]
            # (due to overlap)
            tail = chunks[i][-50:]
            # At least some portion should overlap
            assert any(word in chunks[i + 1] for word in tail.split() if len(word) > 3)


# ---------------------------------------------------------------------------
# _rerank_fallback
# ---------------------------------------------------------------------------


class TestRerankFallback:
    def test_truncates_content(self):
        from app.agent.tools.search import CHUNK_SIZE, _rerank_fallback

        results = [
            {"title": "T", "link": "L", "content": "X" * 5000},
            {"title": "T2", "link": "L2", "snippet": "short"},
        ]
        out = _rerank_fallback(results, top_k=2)
        assert len(out) == 2
        assert len(out[0]["content"]) == CHUNK_SIZE
        assert out[1]["content"] == "short"

    def test_respects_top_k(self):
        from app.agent.tools.search import _rerank_fallback

        results = [{"title": f"T{i}", "link": f"L{i}", "content": "c"} for i in range(10)]
        out = _rerank_fallback(results, top_k=3)
        assert len(out) == 3


# ---------------------------------------------------------------------------
# _rerank_chunks
# ---------------------------------------------------------------------------


class TestRerankChunks:
    @pytest.mark.asyncio
    async def test_fallback_when_no_api_key(self):
        """Without siliconflow key, should fall back to truncation."""
        from app.agent.tools.search import _rerank_chunks

        with patch("app.agent.tools.search.settings") as mock_settings:
            mock_settings.siliconflow_api_key = None
            results = [{"title": "T", "link": "L", "content": "Hello world"}]
            out = await _rerank_chunks("hello", results)
            assert len(out) == 1
            assert out[0]["title"] == "T"

    @pytest.mark.asyncio
    async def test_calls_siliconflow_api(self):
        """With API key, should call the rerank endpoint."""
        from app.agent.tools.search import _rerank_chunks

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "results": [
                {"index": 0, "relevance_score": 0.9},
            ]
        }

        with (
            patch("app.agent.tools.search.settings") as mock_settings,
            patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response),
        ):
            mock_settings.siliconflow_api_key = "test-key"
            mock_settings.siliconflow_base_url = "http://test"

            results = [{"title": "T", "link": "L", "content": "Hello world content"}]
            out = await _rerank_chunks("hello", results, top_k=1)
            assert len(out) == 1
            assert out[0]["score"] == 0.9

    @pytest.mark.asyncio
    async def test_filters_low_relevance(self):
        """Results below MIN_RELEVANCE_SCORE should be excluded."""
        from app.agent.tools.search import _rerank_chunks

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "results": [
                {"index": 0, "relevance_score": 0.01},  # below threshold
            ]
        }

        with (
            patch("app.agent.tools.search.settings") as mock_settings,
            patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response),
        ):
            mock_settings.siliconflow_api_key = "test-key"
            mock_settings.siliconflow_base_url = "http://test"

            results = [{"title": "T", "link": "L", "content": "Some content"}]
            out = await _rerank_chunks("query", results, top_k=5)
            assert len(out) == 0


# ---------------------------------------------------------------------------
# _read_webpage
# ---------------------------------------------------------------------------


class TestReadWebpage:
    @pytest.mark.asyncio
    async def test_returns_empty_when_not_configured(self):
        from app.agent.tools.search import _read_webpage

        with patch("app.agent.tools.search.settings") as mock_settings:
            mock_settings.you_search_host = None
            mock_settings.you_search_api_key = None
            result = await _read_webpage("http://example.com")
            assert result == ""

    @pytest.mark.asyncio
    async def test_prefers_markdown(self):
        from app.agent.tools.search import _read_webpage

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "contents": [{"markdown": "# Hello", "html": "<h1>Hello</h1>"}]
        }

        with (
            patch("app.agent.tools.search.settings") as mock_settings,
            patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response),
        ):
            mock_settings.you_search_host = "http://you"
            mock_settings.you_search_api_key = "key"
            result = await _read_webpage("http://example.com")
            assert result == "# Hello"

    @pytest.mark.asyncio
    async def test_falls_back_to_html(self):
        from app.agent.tools.search import _read_webpage

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "contents": [{"markdown": "", "html": "<p>Hello world</p>"}]
        }

        with (
            patch("app.agent.tools.search.settings") as mock_settings,
            patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response),
        ):
            mock_settings.you_search_host = "http://you"
            mock_settings.you_search_api_key = "key"
            result = await _read_webpage("http://example.com")
            assert "Hello world" in result


# ---------------------------------------------------------------------------
# _html_to_text
# ---------------------------------------------------------------------------


class TestHtmlToText:
    def test_strips_scripts_and_styles(self):
        from app.agent.tools.search import _html_to_text

        html = "<html><script>alert(1)</script><style>.x{}</style><p>Content</p></html>"
        text = _html_to_text(html)
        assert "alert" not in text
        assert "Content" in text

    def test_empty_html(self):
        from app.agent.tools.search import _html_to_text

        assert _html_to_text("") == ""


# ---------------------------------------------------------------------------
# search_web (tool-level)
# ---------------------------------------------------------------------------


class TestSearchWebTool:
    @pytest.mark.asyncio
    async def test_returns_error_when_no_provider(self):
        from app.agent.tools.search import search_web

        with patch("app.agent.tools.search.settings") as mock_settings:
            mock_settings.you_search_host = None
            mock_settings.you_search_api_key = None
            mock_settings.google_search_host = None
            mock_settings.google_search_api_key = None
            # Call the underlying function (not the @tool wrapper)
            result = await search_web.coroutine("test query")
            assert "未配置" in result or "失败" in result

    @pytest.mark.asyncio
    async def test_timeout_returns_error(self):
        from app.agent.tools.search import search_web

        with patch("app.agent.tools.search.settings") as mock_settings:
            mock_settings.you_search_host = "http://you"
            mock_settings.you_search_api_key = "key"
            with patch(
                "app.agent.tools.search._you_search",
                new_callable=AsyncMock,
                side_effect=httpx.TimeoutException("timeout"),
            ):
                result = await search_web.coroutine("test query")
                assert "超时" in result
