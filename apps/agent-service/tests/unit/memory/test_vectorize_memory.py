"""Test memory node vectorization functions."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.memory.vectorize_memory import vectorize_abstract, vectorize_fragment


@pytest.mark.asyncio
async def test_vectorize_fragment_upserts_to_qdrant():
    mock_fragment = MagicMock()
    mock_fragment.id = "f_1"
    mock_fragment.persona_id = "chiwei"
    mock_fragment.content = "他说明天要下雨"
    mock_fragment.source = "afterthought"
    mock_fragment.chat_id = "oc_xxx"
    mock_fragment.clarity = "clear"
    mock_fragment.last_touched_at = datetime(2026, 4, 18, tzinfo=UTC)

    with patch("app.memory.vectorize_memory.get_fragment_by_id", new=AsyncMock(return_value=mock_fragment)):
        with patch("app.memory.vectorize_memory.embed_dense", new=AsyncMock(return_value=[0.1] * 1024)) as emb:
            with patch("app.memory.vectorize_memory.qdrant") as q:
                q.upsert_vectors = AsyncMock(return_value=None)
                ok = await vectorize_fragment("f_1")
    assert ok is True
    emb.assert_awaited_once()
    q.upsert_vectors.assert_awaited_once()
    # payload carries persona_id and clarity
    upsert_call = q.upsert_vectors.call_args
    payloads = upsert_call.kwargs.get("payloads") or upsert_call.args[3]
    assert payloads[0]["persona_id"] == "chiwei"
    assert payloads[0]["clarity"] == "clear"
    assert payloads[0]["last_touched_at"] == "2026-04-18T00:00:00+00:00"


@pytest.mark.asyncio
async def test_vectorize_fragment_missing_returns_false():
    with patch("app.memory.vectorize_memory.get_fragment_by_id", new=AsyncMock(return_value=None)):
        ok = await vectorize_fragment("f_missing")
    assert ok is False


@pytest.mark.asyncio
async def test_vectorize_fragment_empty_content_returns_false():
    mock_fragment = MagicMock()
    mock_fragment.content = "   "  # whitespace only
    with patch("app.memory.vectorize_memory.get_fragment_by_id", new=AsyncMock(return_value=mock_fragment)):
        ok = await vectorize_fragment("f_blank")
    assert ok is False


@pytest.mark.asyncio
async def test_vectorize_fragment_qdrant_failure_propagates():
    # Qdrant exceptions must propagate up so the durable consumer nacks
    # and retries — silent False-returning swallowing was the prior bug.
    mock_fragment = MagicMock()
    mock_fragment.id = "f_1"
    mock_fragment.persona_id = "chiwei"
    mock_fragment.content = "test"
    mock_fragment.source = "manual"
    mock_fragment.chat_id = None
    mock_fragment.clarity = "clear"
    mock_fragment.last_touched_at = None

    with patch("app.memory.vectorize_memory.get_fragment_by_id", new=AsyncMock(return_value=mock_fragment)):
        with patch("app.memory.vectorize_memory.embed_dense", new=AsyncMock(return_value=[0.1] * 1024)):
            with patch("app.memory.vectorize_memory.qdrant") as q:
                q.upsert_vectors = AsyncMock(side_effect=RuntimeError("qdrant offline"))
                with pytest.raises(RuntimeError, match="qdrant offline"):
                    await vectorize_fragment("f_1")


@pytest.mark.asyncio
async def test_vectorize_abstract_upserts_to_qdrant():
    mock_a = MagicMock()
    mock_a.id = "a_1"
    mock_a.persona_id = "chiwei"
    mock_a.subject = "user:u1"
    mock_a.content = "他是程序员"
    mock_a.created_by = "chiwei"
    mock_a.clarity = "clear"
    mock_a.last_touched_at = datetime(2026, 4, 18, tzinfo=UTC)

    with patch("app.memory.vectorize_memory.get_abstract_by_id", new=AsyncMock(return_value=mock_a)):
        with patch("app.memory.vectorize_memory.embed_dense", new=AsyncMock(return_value=[0.2] * 1024)):
            with patch("app.memory.vectorize_memory.qdrant") as q:
                q.upsert_vectors = AsyncMock(return_value=None)
                ok = await vectorize_abstract("a_1")
    assert ok is True
    payloads = q.upsert_vectors.call_args.kwargs.get("payloads") or q.upsert_vectors.call_args.args[3]
    assert payloads[0]["subject"] == "user:u1"
    assert payloads[0]["persona_id"] == "chiwei"
    assert payloads[0]["last_touched_at"] == "2026-04-18T00:00:00+00:00"


@pytest.mark.asyncio
async def test_vectorize_abstract_missing_returns_false():
    with patch("app.memory.vectorize_memory.get_abstract_by_id", new=AsyncMock(return_value=None)):
        ok = await vectorize_abstract("a_missing")
    assert ok is False
