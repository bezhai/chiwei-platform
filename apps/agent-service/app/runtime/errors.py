"""Phase 7b Gap 18: typed exceptions for runtime error policy.

These exceptions are part of the framework's surface for business code:
- DuplicateData / NeedsReview: business code raises these from a consumer
  to signal a specific failure semantic; the durable handler routes them
  per the wire's on_error policy (see runtime/durable.py).
- AlreadySucceededError: raised by runtime/inflight.delete_inflight when
  a caller targets an already-succeeded inflight row in edge_idempotent
  mode. Used by the DLQ requeue protocol (zombie detection).
"""
from __future__ import annotations


class DuplicateData(Exception):
    """Business code raises this from a consumer to signal that the
    incoming Data is a business-level duplicate (beyond what the
    runtime_inflight (edge_id, idempotent_key) dedup already covers).

    Framework behavior:
      - on_error="ignore-duplicate" -> ack + log warning + no DLQ + no retry
      - on_error other values       -> falls through to generic Exception
                                       path (mark_failed + decide_retry +
                                       eventually DLQ). Safe-default for
                                       misconfigured wires.
    """


class NeedsReview(Exception):
    """Business code raises this from a consumer to signal that the
    Data requires human review before any retry / dispatch decision.

    Framework behavior:
      - on_error="manual-review"   -> publish to manual-review queue + ack
      - on_error other values      -> falls through to generic Exception
                                       path. Safe-default for misconfigured
                                       wires.
    """


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
