"""save_fragment @node: writes Fragment to messages_recall + messages_cluster.

Verifies both upserts happen (concurrently via asyncio.gather) with the
correct shape per collection — hybrid for recall, dense-only for cluster.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.domain.fragment import Fragment


def _sample_fragment() -> Fragment:
    return Fragment(
        fragment_id="c9d05a5e-aaaa-bbbb-cccc-dddddddddddd",
        message_id="m1",
        chat_id="c1",
        dense=[0.1] * 1024,
        sparse={"indices": [1, 7], "values": [0.5, 0.25]},
        dense_cluster=[0.2] * 1024,
        recall_payload={
            "message_id": "m1",
            "user_id": "u1",
            "chat_id": "c1",
            "timestamp": 1,
            "root_message_id": "r1",
            "original_text": "hi",
        },
        cluster_payload={
            "message_id": "m1",
            "user_id": "u1",
            "chat_id": "c1",
            "timestamp": 1,
        },
    )


@pytest.mark.asyncio
async def test_save_fragment_upserts_both_collections():
    from app.nodes.save_fragment import save_fragment

    frag = _sample_fragment()

    with patch(
        "app.nodes.save_fragment.recall_store.upsert", new_callable=AsyncMock
    ) as r, patch(
        "app.nodes.save_fragment.cluster_store.upsert_dense",
        new_callable=AsyncMock,
    ) as c:
        await save_fragment(frag)

    r.assert_awaited_once()
    c.assert_awaited_once()

    # recall: (fragment_id, HybridEmbedding, recall_payload)
    args_r = r.await_args.args
    assert args_r[0] == frag.fragment_id
    hyb = args_r[1]
    assert hyb.dense == frag.dense
    assert list(hyb.sparse.indices) == [1, 7]
    assert list(hyb.sparse.values) == [0.5, 0.25]
    assert args_r[2] == frag.recall_payload

    # cluster: (fragment_id, dense_cluster, cluster_payload)
    c.assert_awaited_once_with(
        frag.fragment_id, frag.dense_cluster, frag.cluster_payload
    )


@pytest.mark.asyncio
async def test_save_fragment_propagates_failure():
    """Partial-failure: if one upsert raises, save_fragment raises.

    Runtime's durable-edge layer nacks + retries the message. qdrant upsert
    is idempotent per point_id, so retrying doesn't corrupt the collection
    that already succeeded.
    """
    from app.nodes.save_fragment import save_fragment

    frag = _sample_fragment()

    with patch(
        "app.nodes.save_fragment.recall_store.upsert",
        new_callable=AsyncMock,
        side_effect=RuntimeError("qdrant down"),
    ), patch(
        "app.nodes.save_fragment.cluster_store.upsert_dense",
        new_callable=AsyncMock,
        return_value=True,
    ):
        with pytest.raises(RuntimeError, match="qdrant down"):
            await save_fragment(frag)
