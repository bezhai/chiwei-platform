"""Test that init_collections creates v4 memory collections."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.infra.qdrant import init_collections


@pytest.mark.asyncio
async def test_init_collections_creates_memory_fragment_and_abstract():
    with patch("app.infra.qdrant.qdrant") as mock_qdrant:
        mock_qdrant.create_collection = AsyncMock(return_value=True)
        mock_qdrant.create_hybrid_collection = AsyncMock(return_value=True)

        await init_collections()

        created_names = [
            call.kwargs.get("collection_name")
            or call.args[0]  # positional fallback
            for call in mock_qdrant.create_collection.call_args_list
        ]
        assert "memory_fragment" in created_names
        assert "memory_abstract" in created_names
