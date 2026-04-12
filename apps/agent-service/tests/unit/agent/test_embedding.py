"""test_embedding.py -- Embedding tests.

Covers:
  - SparseVector / HybridEmbedding data types
  - Modality constants
  - InstructionBuilder: detect_input_modality, combine, corpus/query/cluster
  - embed_dense: text-only, multimodal, empty input
  - embed_hybrid: text-only (single request), multimodal (two requests), image-only
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.agent import embedding as mod
from app.agent.embedding import (
    HybridEmbedding,
    InstructionBuilder,
    Modality,
    SparseVector,
    embed_dense,
    embed_hybrid,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_INFO = {
    "model_name": "doubao-embedding-vision",
    "api_key": "sk-test",
    "base_url": "https://ark.test.com/v3",
    "client_type": "ark",
    "is_active": True,
    "use_proxy": False,
}


def _fake_info(**overrides: object) -> dict:
    return {**_FAKE_INFO, **overrides}


def _make_sparse_items(pairs: list[tuple[int, float]]) -> list[SimpleNamespace]:
    """Simulate Volcengine SparseEmbedding objects."""
    return [SimpleNamespace(index=idx, value=val) for idx, val in pairs]


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class TestSparseVector:
    def test_named_tuple_fields(self):
        sv = SparseVector(indices=[1, 3], values=[0.5, 0.8])
        assert sv.indices == [1, 3]
        assert sv.values == [0.5, 0.8]

    def test_empty(self):
        sv = SparseVector(indices=[], values=[])
        assert len(sv.indices) == 0


class TestHybridEmbedding:
    def test_dataclass_fields(self):
        he = HybridEmbedding(
            dense=[0.1, 0.2],
            sparse=SparseVector(indices=[0], values=[1.0]),
        )
        assert he.dense == [0.1, 0.2]
        assert he.sparse.indices == [0]

    def test_frozen(self):
        he = HybridEmbedding(
            dense=[0.1], sparse=SparseVector(indices=[], values=[])
        )
        with pytest.raises(AttributeError):
            he.dense = [0.9]  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Modality
# ---------------------------------------------------------------------------


class TestModality:
    def test_constants(self):
        assert Modality.TEXT == "text"
        assert Modality.IMAGE == "image"
        assert Modality.TEXT_AND_IMAGE == "text and image"


# ---------------------------------------------------------------------------
# InstructionBuilder
# ---------------------------------------------------------------------------


class TestInstructionBuilder:
    def test_detect_text_only(self):
        assert InstructionBuilder.detect_input_modality("hello", None) == "text"

    def test_detect_image_only(self):
        assert InstructionBuilder.detect_input_modality(None, ["img"]) == "image"

    def test_detect_text_and_image(self):
        assert (
            InstructionBuilder.detect_input_modality("hi", ["img"]) == "text and image"
        )

    def test_detect_empty_text_fallback(self):
        assert InstructionBuilder.detect_input_modality("", None) == "text"

    def test_detect_whitespace_text_fallback(self):
        assert InstructionBuilder.detect_input_modality("  ", None) == "text"

    def test_combine_corpus_modalities(self):
        result = InstructionBuilder.combine_corpus_modalities("text", "image")
        assert result == "text/image"

    def test_for_corpus(self):
        result = InstructionBuilder.for_corpus("text")
        assert result == "Instruction:Compress the text into one word.\nQuery:"

    def test_for_query(self):
        result = InstructionBuilder.for_query("text/image", "find related docs")
        assert "Target_modality: text/image" in result
        assert "find related docs" in result
        assert result.endswith("Query:")

    def test_for_cluster(self):
        result = InstructionBuilder.for_cluster("text", "Retrieve similar content")
        assert "Target_modality: text" in result
        assert result.endswith("Query:")

    def test_for_cluster_is_for_query(self):
        """for_cluster should produce the same output as for_query."""
        assert InstructionBuilder.for_cluster("text", "X") == InstructionBuilder.for_query("text", "X")


# ---------------------------------------------------------------------------
# embed_dense
# ---------------------------------------------------------------------------


class TestEmbedDense:
    async def test_text_only(self):
        fake_resp = SimpleNamespace(data=SimpleNamespace(embedding=[0.1, 0.2, 0.3]))
        mock_client = AsyncMock()
        mock_client.multimodal_embeddings.create = AsyncMock(return_value=fake_resp)
        mock_client.close = AsyncMock()

        with (
            patch(
                "app.agent.embedding.resolve_model_info",
                new_callable=AsyncMock,
                return_value=_fake_info(),
            ),
            patch.object(
                mod,
                "_create_ark_client",
                return_value=mock_client,
            ),
        ):
            result = await embed_dense(
                "embedding-model",
                text="hello world",
                instructions="test instruction",
            )

        assert result == [0.1, 0.2, 0.3]
        mock_client.multimodal_embeddings.create.assert_called_once()
        call_kwargs = mock_client.multimodal_embeddings.create.call_args
        assert call_kwargs.kwargs["input"] == [{"type": "text", "text": "hello world"}]
        mock_client.close.assert_called_once()

    async def test_multimodal(self):
        fake_resp = SimpleNamespace(data=SimpleNamespace(embedding=[0.4, 0.5]))
        mock_client = AsyncMock()
        mock_client.multimodal_embeddings.create = AsyncMock(return_value=fake_resp)
        mock_client.close = AsyncMock()

        with (
            patch(
                "app.agent.embedding.resolve_model_info",
                new_callable=AsyncMock,
                return_value=_fake_info(),
            ),
            patch.object(
                mod,
                "_create_ark_client",
                return_value=mock_client,
            ),
        ):
            result = await embed_dense(
                "embedding-model",
                text="caption",
                image_base64_list=["data:image/png;base64,abc"],
            )

        assert result == [0.4, 0.5]
        call_input = mock_client.multimodal_embeddings.create.call_args.kwargs["input"]
        assert len(call_input) == 2
        assert call_input[1]["type"] == "image_url"

    async def test_empty_input_raises(self):
        with pytest.raises(ValueError, match="at least"):
            await embed_dense("embedding-model")

    async def test_client_closed_on_error(self):
        mock_client = AsyncMock()
        mock_client.multimodal_embeddings.create = AsyncMock(
            side_effect=RuntimeError("API error")
        )
        mock_client.close = AsyncMock()

        with (
            patch(
                "app.agent.embedding.resolve_model_info",
                new_callable=AsyncMock,
                return_value=_fake_info(),
            ),
            patch.object(
                mod,
                "_create_ark_client",
                return_value=mock_client,
            ),
            pytest.raises(RuntimeError, match="API error"),
        ):
            await embed_dense("embedding-model", text="test")

        mock_client.close.assert_called_once()


# ---------------------------------------------------------------------------
# embed_hybrid
# ---------------------------------------------------------------------------


class TestEmbedHybrid:
    async def test_text_only_single_request(self):
        """Text-only: one request returns both dense and sparse."""
        sparse_items = _make_sparse_items([(10, 0.9), (20, 0.5)])
        fake_resp = SimpleNamespace(
            data=SimpleNamespace(
                embedding=[0.1, 0.2],
                sparse_embedding=sparse_items,
            )
        )
        mock_client = AsyncMock()
        mock_client.multimodal_embeddings.create = AsyncMock(return_value=fake_resp)
        mock_client.close = AsyncMock()

        with (
            patch(
                "app.agent.embedding.resolve_model_info",
                new_callable=AsyncMock,
                return_value=_fake_info(),
            ),
            patch.object(
                mod,
                "_create_ark_client",
                return_value=mock_client,
            ),
        ):
            result = await embed_hybrid("embedding-model", text="hello")

        assert result.dense == [0.1, 0.2]
        assert result.sparse.indices == [10, 20]
        assert result.sparse.values == [0.9, 0.5]
        # Single request for text-only
        assert mock_client.multimodal_embeddings.create.call_count == 1
        mock_client.close.assert_called_once()

    async def test_multimodal_two_requests(self):
        """With images: two requests — dense (multimodal) + sparse (text)."""
        dense_resp = SimpleNamespace(data=SimpleNamespace(embedding=[0.3, 0.4]))
        sparse_items = _make_sparse_items([(5, 0.7)])
        sparse_resp = SimpleNamespace(
            data=SimpleNamespace(
                embedding=[0.9, 0.9],  # ignored
                sparse_embedding=sparse_items,
            )
        )
        mock_client = AsyncMock()
        mock_client.multimodal_embeddings.create = AsyncMock(
            side_effect=[dense_resp, sparse_resp]
        )
        mock_client.close = AsyncMock()

        with (
            patch(
                "app.agent.embedding.resolve_model_info",
                new_callable=AsyncMock,
                return_value=_fake_info(),
            ),
            patch.object(
                mod,
                "_create_ark_client",
                return_value=mock_client,
            ),
        ):
            result = await embed_hybrid(
                "embedding-model",
                text="caption",
                image_base64_list=["data:image/png;base64,abc"],
            )

        assert result.dense == [0.3, 0.4]
        assert result.sparse.indices == [5]
        # Two requests for multimodal
        assert mock_client.multimodal_embeddings.create.call_count == 2

    async def test_image_only_empty_sparse(self):
        """Image-only: dense from image, sparse is empty (no text for sparse)."""
        dense_resp = SimpleNamespace(data=SimpleNamespace(embedding=[0.6, 0.7]))
        mock_client = AsyncMock()
        mock_client.multimodal_embeddings.create = AsyncMock(return_value=dense_resp)
        mock_client.close = AsyncMock()

        with (
            patch(
                "app.agent.embedding.resolve_model_info",
                new_callable=AsyncMock,
                return_value=_fake_info(),
            ),
            patch.object(
                mod,
                "_create_ark_client",
                return_value=mock_client,
            ),
        ):
            result = await embed_hybrid(
                "embedding-model",
                image_base64_list=["data:image/png;base64,xyz"],
            )

        assert result.dense == [0.6, 0.7]
        assert result.sparse.indices == []
        assert result.sparse.values == []
        # Only one request (dense), no sparse request since no text
        assert mock_client.multimodal_embeddings.create.call_count == 1

    async def test_empty_input_raises(self):
        with pytest.raises(ValueError, match="at least"):
            await embed_hybrid("embedding-model")
