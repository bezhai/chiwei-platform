"""Phase 7b Gap 18: per-wire manual-review queue.

Each durable wire with on_error='manual-review' gets its own queue
named ``durable_<data_snake>_<consumer>_review``. Unlike DLQ, the review
queue is a TERMINAL — it has no DLX and no consumer. Operators inspect
via /admin/dlq/inspect (queue_kind='review') and decide manually
(replay → delete_inflight + re-publish to original durable queue;
ignore → ack via /admin/dlq/requeue with no reroute).

publish_to_review_queue uses mq.publish_with_confirm so the durable
handler can fall through to DLQ when the broker doesn't confirm.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from app.infra.rabbitmq import Route, mq
from app.runtime.data import Data
from app.runtime.naming import to_snake
from app.runtime.propagation import inject_context
from app.runtime.wire import WireSpec

logger = logging.getLogger(__name__)


def review_queue_name_for(wire: WireSpec, consumer: Callable) -> str:
    """Per-(data, consumer) review queue name."""
    data_snake = to_snake(wire.data_type.__name__)
    return f"durable_{data_snake}_{consumer.__name__}_review"


def route_for_review(queue: str) -> Route:
    """Build a Route for a review queue.

    Review queues are simple direct-bound queues with no DLX and
    lane_fallback=True (prod-compatible). The Route object is stateless —
    callers own declaration via mq.declare_route().
    """
    return Route(queue=queue, rk=queue.replace("_", "."))


async def publish_to_review_queue(
    *,
    wire: WireSpec,
    consumer: Callable,
    data: Data,
    exc: BaseException,
    attempts: int,
    last_error: str,
) -> bool:
    """Publish a NeedsReview-tagged envelope to the wire's review queue.

    Returns True iff broker confirmed; on False the durable handler
    falls through to DLQ raise (helper contract).
    """
    queue = review_queue_name_for(wire, consumer)
    route = route_for_review(queue)
    body = {
        "data": data.model_dump(mode="json"),
        "data_type": f"{type(data).__module__}.{type(data).__qualname__}",
        "exc_class": type(exc).__name__,
        "last_error": last_error,
        "attempts": attempts,
    }
    headers = inject_context({"data_type": "manual_review_envelope"})
    # lane defaults to ... sentinel which calls current_lane() internally.
    confirmed = await mq.publish_with_confirm(
        route,
        body,
        headers=headers,
    )
    if not confirmed:
        logger.warning(
            "review queue publish-confirm FAILED queue=%s data_type=%s",
            queue, body["data_type"],
        )
    return confirmed
