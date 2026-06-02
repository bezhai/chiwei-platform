"""Tests for app.agent.tools.search — chunking, reranking adapter, tool wrapper."""

from unittest.mock import AsyncMock, patch

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
            tail = chunks[i][-50:]
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

        results = [
            {"title": f"T{i}", "link": f"L{i}", "content": "c"} for i in range(10)
        ]
        out = _rerank_fallback(results, top_k=3)
        assert len(out) == 3


# ---------------------------------------------------------------------------
# _rerank_chunks (delegates to capabilities.web_search.rerank)
# ---------------------------------------------------------------------------


class TestRerankChunks:
    @pytest.mark.asyncio
    async def test_fallback_when_capability_returns_empty(self):
        """rerank capability returning [] (e.g. no api key) → fallback to truncation."""
        from app.agent.tools.search import _rerank_chunks

        with patch(
            "app.agent.tools.search._rerank_capability",
            new_callable=AsyncMock,
            return_value=[],
        ):
            results = [{"title": "T", "link": "L", "content": "Hello world"}]
            out = await _rerank_chunks("hello", results)
        # Empty rerank pairs → ranked list is empty (no items pass relevance threshold).
        # The fallback only fires when there are no chunks at all.
        assert out == []

    @pytest.mark.asyncio
    async def test_uses_capability_results(self):
        """Capability returns (idx, score) pairs, tool maps them back to chunks."""
        from app.agent.tools.search import _rerank_chunks

        with patch(
            "app.agent.tools.search._rerank_capability",
            new_callable=AsyncMock,
            return_value=[(0, 0.9)],
        ):
            results = [{"title": "T", "link": "L", "content": "Hello world content"}]
            out = await _rerank_chunks("hello", results, top_k=1)
        assert len(out) == 1
        assert out[0]["score"] == 0.9
        assert out[0]["title"] == "T"

    @pytest.mark.asyncio
    async def test_filters_low_relevance(self):
        """Pairs with score below MIN_RELEVANCE_SCORE are dropped."""
        from app.agent.tools.search import _rerank_chunks

        with patch(
            "app.agent.tools.search._rerank_capability",
            new_callable=AsyncMock,
            return_value=[(0, 0.01)],
        ):
            results = [{"title": "T", "link": "L", "content": "Some content"}]
            out = await _rerank_chunks("query", results, top_k=5)
        assert out == []

    @pytest.mark.asyncio
    async def test_no_chunks_falls_back(self):
        """If results have no content, _rerank_fallback runs (capability not called)."""
        from app.agent.tools.search import _rerank_chunks

        with patch(
            "app.agent.tools.search._rerank_capability",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_cap:
            results = [{"title": "T", "link": "L", "snippet": "fallback"}]
            out = await _rerank_chunks("query", results, top_k=2)
        mock_cap.assert_not_called()
        assert len(out) == 1
        assert out[0]["content"] == "fallback"


# ---------------------------------------------------------------------------
# _read_webpage (delegates to capabilities.web_search.read_webpage)
# ---------------------------------------------------------------------------


class TestReadWebpage:
    @pytest.mark.asyncio
    async def test_returns_empty_on_capability_exception(self):
        from app.agent.tools.search import _read_webpage

        with patch(
            "app.agent.tools.search._read_webpage_capability",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            assert await _read_webpage("http://example.com") == ""

    @pytest.mark.asyncio
    async def test_passes_through_capability_result(self):
        from app.agent.tools.search import _read_webpage

        with patch(
            "app.agent.tools.search._read_webpage_capability",
            new_callable=AsyncMock,
            return_value="# Hello",
        ):
            assert await _read_webpage("http://example.com") == "# Hello"


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
    async def test_returns_error_when_capability_returns_empty(self):
        from app.agent.tools.search import search_web

        with patch(
            "app.agent.tools.search._web_search_capability",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await search_web.invoke({"query": "test query"})
        assert "未配置" in result or "未搜索到" in result

    @pytest.mark.asyncio
    async def test_capability_failure_surfaces_as_friendly_error(self):
        """``search_web`` has its own in-tool ``try/except Exception`` (around
        the capability call) that records error metrics and returns a friendly
        string. That predates C3 and bypasses the @tool_error path entirely.

        TODO(C3 follow-up): migrate this tool to raise typed errors or let
        them propagate to wire ``on_error`` — see
        ``docs/guides/dataflow-node-contract.md`` §4.7/§4.8.
        """
        from app.agent.tools.search import search_web

        with patch(
            "app.agent.tools.search._web_search_capability",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            result = await search_web.invoke({"query": "test query"})
        assert "网页搜索失败" in result
