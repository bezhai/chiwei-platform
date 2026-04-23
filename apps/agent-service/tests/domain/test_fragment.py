"""Fragment Data — transient output of vectorize, straight to VectorStore.

Fragment is not persisted to pg (business code never queries it). The
migrator skips Data classes with ``Meta.transient = True``; the runtime
passes Fragment instances through an in-process edge to ``save_fragment``
which writes to the two vector collections.

Fragment carries two separate payloads because recall and cluster
collections store different shapes — the vectorize node fills both and
``save_fragment`` writes them to the respective collections.
"""
from __future__ import annotations

from app.domain.fragment import Fragment
from app.runtime.data import key_fields


def test_fragment_key():
    assert key_fields(Fragment) == ("fragment_id",)


def test_fragment_transient_marker():
    assert getattr(Fragment.Meta, "transient", False) is True


def test_fragment_dual_payload_instance():
    f = Fragment(
        fragment_id="c9d05a5e-aaaa-bbbb-cccc-dddddddddddd",
        message_id="m1",
        chat_id="c1",
        dense=[0.0] * 1024,
        sparse={"indices": [1], "values": [0.5]},
        dense_cluster=[0.0] * 1024,
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
    assert f.message_id == "m1"
    assert "original_text" in f.recall_payload
    assert "original_text" not in f.cluster_payload
