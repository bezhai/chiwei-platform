"""vectorize @node: Message -> Fragment | None via EmbedderClient.

Covers all six legacy-behavior branches:
  1. text-only path produces a Fragment with dual payloads;
  2. empty content + no images -> None, no embedder calls;
  3. ``only_owner`` permission drops images but text continues;
  4. permission allowed -> images downloaded and embedded;
  5. text empty + all image downloads fail -> None (post-download check);
  6. partial image download failure -> only successful base64s survive.

External dependencies are patched at the ``app.nodes.vectorize`` module
namespace (where the names are bound) so legacy call sites are not touched.
String-based ``patch`` targets are required because the ``app.nodes``
package re-exports the ``vectorize`` function, shadowing the submodule
attribute on the parent package.
"""
from __future__ import annotations

import importlib
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.agent.embedding import HybridEmbedding, SparseVector
from app.domain.message import Message
from app.nodes._ids import vector_id_for

# Force-load the submodule so ``vectorize_mod.vectorize`` refers to the
# function without going through the parent-package re-export.
vectorize_mod = importlib.import_module("app.nodes.vectorize")


def _msg(content: str = '{"v":2,"text":"hello","items":[]}', chat_id: str = "c1") -> Message:
    """Plausible Message covering all 13 fields for the @node under test."""
    return Message(
        message_id="m1",
        user_id="u1",
        content=content,
        role="user",
        root_message_id="r1",
        reply_message_id=None,
        chat_id=chat_id,
        chat_type="group",
        create_time=1700_000_000,
        message_type="text",
        vector_status="pending",
        bot_name=None,
        response_id=None,
    )


def _hybrid(dense_val: float = 0.1) -> HybridEmbedding:
    return HybridEmbedding(
        dense=[dense_val] * 1024,
        sparse=SparseVector(indices=[1], values=[0.5]),
    )


def _parsed(text: str, image_keys: list[str]):
    """Stand-in for ParsedContent: only ``.render()`` and ``.image_keys`` are read."""
    return SimpleNamespace(render=lambda: text, image_keys=image_keys)


def _stub_session():
    """Patch-target value: an async-context-manager factory yielding a sentinel."""

    @asynccontextmanager
    async def _cm():
        yield AsyncMock()

    return _cm


@pytest.mark.asyncio
async def test_text_only_produces_fragment():
    msg = _msg()
    with patch(
        "app.nodes.vectorize.parse_content", return_value=_parsed("hello", [])
    ), patch(
        "app.nodes.vectorize.embedder.hybrid",
        new_callable=AsyncMock,
        return_value=_hybrid(0.1),
    ) as hybrid_mock, patch(
        "app.nodes.vectorize.embedder.dense",
        new_callable=AsyncMock,
        return_value=[0.2] * 1024,
    ) as dense_mock:
        frag = await vectorize_mod.vectorize(msg)

    assert frag is not None
    assert frag.fragment_id == vector_id_for(msg.message_id)
    assert frag.message_id == msg.message_id
    assert frag.chat_id == msg.chat_id
    assert frag.dense == [0.1] * 1024
    assert frag.sparse == {"indices": [1], "values": [0.5]}
    assert frag.dense_cluster == [0.2] * 1024

    # dual-payload shapes (warning #6)
    assert frag.recall_payload["original_text"] == "hello"
    assert "root_message_id" in frag.recall_payload
    assert frag.recall_payload["timestamp"] == msg.create_time
    assert "root_message_id" not in frag.cluster_payload
    assert "original_text" not in frag.cluster_payload
    assert frag.cluster_payload["message_id"] == msg.message_id

    hybrid_mock.assert_awaited_once()
    dense_mock.assert_awaited_once()
    # image_base64_list arg must be falsy (None) since there were no images
    assert hybrid_mock.await_args.kwargs.get("image_base64_list") is None


@pytest.mark.asyncio
async def test_empty_content_and_no_images_returns_none():
    with patch(
        "app.nodes.vectorize.parse_content", return_value=_parsed("", [])
    ), patch(
        "app.nodes.vectorize.embedder.hybrid", new_callable=AsyncMock
    ) as hybrid_mock, patch(
        "app.nodes.vectorize.embedder.dense", new_callable=AsyncMock
    ) as dense_mock:
        frag = await vectorize_mod.vectorize(_msg())

    assert frag is None
    hybrid_mock.assert_not_awaited()
    dense_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_permission_only_owner_drops_images_but_text_continues():
    with patch(
        "app.nodes.vectorize.parse_content", return_value=_parsed("t", ["k1"])
    ), patch("app.nodes.vectorize.get_session", _stub_session()), patch(
        "app.nodes.vectorize.find_group_download_permission",
        new_callable=AsyncMock,
        return_value="only_owner",
    ), patch(
        "app.nodes.vectorize.image_client.download_image_as_base64",
        new_callable=AsyncMock,
    ) as dl_mock, patch(
        "app.nodes.vectorize.embedder.hybrid",
        new_callable=AsyncMock,
        return_value=_hybrid(),
    ) as hybrid_mock, patch(
        "app.nodes.vectorize.embedder.dense",
        new_callable=AsyncMock,
        return_value=[0.2] * 1024,
    ):
        frag = await vectorize_mod.vectorize(_msg())

    assert frag is not None
    dl_mock.assert_not_awaited()
    # hybrid called but with no images (image_base64_list falsy -> None)
    assert hybrid_mock.await_args.kwargs.get("image_base64_list") is None


@pytest.mark.asyncio
async def test_permission_allowed_downloads_images():
    with patch(
        "app.nodes.vectorize.parse_content", return_value=_parsed("t", ["k1", "k2"])
    ), patch("app.nodes.vectorize.get_session", _stub_session()), patch(
        "app.nodes.vectorize.find_group_download_permission",
        new_callable=AsyncMock,
        return_value=None,
    ), patch(
        "app.nodes.vectorize.image_client.download_image_as_base64",
        new_callable=AsyncMock,
        side_effect=["b64_1", "b64_2"],
    ), patch(
        "app.nodes.vectorize.embedder.hybrid",
        new_callable=AsyncMock,
        return_value=_hybrid(),
    ) as hybrid_mock, patch(
        "app.nodes.vectorize.embedder.dense",
        new_callable=AsyncMock,
        return_value=[0.2] * 1024,
    ) as dense_mock:
        frag = await vectorize_mod.vectorize(_msg())

    assert frag is not None
    assert hybrid_mock.await_args.kwargs["image_base64_list"] == ["b64_1", "b64_2"]
    assert dense_mock.await_args.kwargs["image_base64_list"] == ["b64_1", "b64_2"]


@pytest.mark.asyncio
async def test_image_download_all_fail_no_text_returns_none():
    with patch(
        "app.nodes.vectorize.parse_content", return_value=_parsed("", ["k1"])
    ), patch("app.nodes.vectorize.get_session", _stub_session()), patch(
        "app.nodes.vectorize.find_group_download_permission",
        new_callable=AsyncMock,
        return_value=None,
    ), patch(
        "app.nodes.vectorize.image_client.download_image_as_base64",
        new_callable=AsyncMock,
        side_effect=RuntimeError("boom"),
    ), patch(
        "app.nodes.vectorize.embedder.hybrid", new_callable=AsyncMock
    ) as hybrid_mock, patch(
        "app.nodes.vectorize.embedder.dense", new_callable=AsyncMock
    ) as dense_mock:
        frag = await vectorize_mod.vectorize(_msg())

    assert frag is None
    hybrid_mock.assert_not_awaited()
    dense_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_image_download_partial_failure_filters_successes():
    with patch(
        "app.nodes.vectorize.parse_content",
        return_value=_parsed("", ["k1", "k2"]),
    ), patch("app.nodes.vectorize.get_session", _stub_session()), patch(
        "app.nodes.vectorize.find_group_download_permission",
        new_callable=AsyncMock,
        return_value=None,
    ), patch(
        "app.nodes.vectorize.image_client.download_image_as_base64",
        new_callable=AsyncMock,
        side_effect=["good", RuntimeError("boom")],
    ), patch(
        "app.nodes.vectorize.embedder.hybrid",
        new_callable=AsyncMock,
        return_value=_hybrid(),
    ) as hybrid_mock, patch(
        "app.nodes.vectorize.embedder.dense",
        new_callable=AsyncMock,
        return_value=[0.2] * 1024,
    ):
        frag = await vectorize_mod.vectorize(_msg())

    assert frag is not None
    assert hybrid_mock.await_args.kwargs["image_base64_list"] == ["good"]
