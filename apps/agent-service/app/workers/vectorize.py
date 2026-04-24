"""Memory-vectorize worker — consumer for the ``memory_vectorize`` queue.

Message vectorization was retired in T1.10; the conversation_messages →
Fragment → qdrant pipeline now lives on ``app.workers.runtime_entry``
(dataflow runtime + `wiring/memory.py`). This file keeps only the
memory-v4 side: ``handle_memory_vectorize`` dispatches by ``kind`` to
``vectorize_fragment`` / ``vectorize_abstract`` in ``app.memory``.

Kept as its own ``python -m`` entry point so the prod k8s deployment can
keep running until a follow-up PR migrates ``memory_vectorize`` onto the
dataflow runtime as well.
"""

from __future__ import annotations

import asyncio
import json
import logging

from aio_pika.abc import AbstractIncomingMessage

from app.infra.rabbitmq import MEMORY_VECTORIZE, current_lane, lane_queue, mq
from app.memory.vectorize_memory import vectorize_abstract, vectorize_fragment
from app.workers.common import mq_error_handler

logger = logging.getLogger(__name__)


@mq_error_handler()
async def handle_memory_vectorize(message: AbstractIncomingMessage) -> None:
    """Consume memory_vectorize queue.

    Payload: ``{"kind": "fragment"|"abstract", "id": "<pk>"}``.
    """
    async with message.process(requeue=False):
        data = json.loads(message.body.decode())
        kind = data.get("kind")
        node_id = data.get("id")
        if not node_id:
            logger.warning("memory_vectorize missing id: %s", data)
            return
        if kind == "fragment":
            await vectorize_fragment(node_id)
        elif kind == "abstract":
            await vectorize_abstract(node_id)
        else:
            logger.warning("memory_vectorize unknown kind %s", kind)


async def start_memory_vectorize_consumer() -> None:
    """Connect MQ and start consuming the memory_vectorize queue."""
    await mq.connect()
    await mq.declare_topology()
    lane = current_lane()

    mv_queue = lane_queue(MEMORY_VECTORIZE.queue, lane)
    await mq.consume(mv_queue, handle_memory_vectorize)
    logger.info("Memory-vectorize consumer started (queue=%s)", mv_queue)


if __name__ == "__main__":
    from inner_shared.logger import setup_logging

    setup_logging(log_dir="/logs/agent-service", log_file="vectorize-worker.log")
    logger.info("memory-vectorize-worker started, file logging enabled")

    async def _main():
        await start_memory_vectorize_consumer()
        await asyncio.Future()  # keep alive

    asyncio.run(_main())
