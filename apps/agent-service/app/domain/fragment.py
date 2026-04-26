"""Fragment Data — transient vectorize output, consumed by save_fragment.

The vectorize ``@node`` produces a Fragment per input message; it flows
through an in-process edge to ``save_fragment`` which writes dense/sparse
embeddings plus the two payload shapes into their respective vector
collections (``messages_recall`` and ``messages_cluster``).

Fragment is *transient*: it is never persisted to pg. The migrator skips
any Data class with ``Meta.transient = True`` (no CREATE, no ALTER). The
``recall_payload`` and ``cluster_payload`` fields are raw dicts on
purpose — the consumers (save_fragment + qdrant client) want the payload
shapes they will actually ship without intermediate re-modeling.
"""
from __future__ import annotations

from typing import Annotated

from app.runtime import Data, Key


class Fragment(Data):
    fragment_id: Annotated[str, Key]  # UUID5(NAMESPACE_DNS, message_id), str form
    message_id: str
    chat_id: str
    dense: list[float]
    sparse: dict  # {"indices": [...], "values": [...]}
    dense_cluster: list[float]
    recall_payload: dict  # full payload for messages_recall
    cluster_payload: dict  # reduced payload for messages_cluster

    class Meta:
        transient = True  # not persisted to pg; straight to VectorStore
