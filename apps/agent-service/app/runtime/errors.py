"""Typed exceptions of the runtime.

- AlreadySucceededError: raised by runtime/inflight.delete_inflight when
  a caller targets an already-succeeded inflight row in edge_idempotent
  mode. Used by the DLQ requeue protocol (zombie detection).
"""
from __future__ import annotations


class AlreadySucceededError(Exception):
    """Raised by runtime/inflight.delete_inflight in edge_idempotent mode
    when the targeted (edge_id, idempotent_key) already has state='succeeded'.

    The DLQ requeue 6-step protocol catches this and treats the original
    DLQ message as a zombie (ack + audit status='zombie_acked'); see
    nodes/dlq_admin.py.
    """

    def __init__(self, *, edge_id: str, idempotent_key: str) -> None:
        super().__init__(
            f"inflight already succeeded: edge_id={edge_id!r} "
            f"idempotent_key={idempotent_key!r}"
        )
        self.edge_id = edge_id
        self.idempotent_key = idempotent_key
