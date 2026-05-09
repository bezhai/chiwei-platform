"""Phase 7b Gap 12: minimal RabbitMQ Management HTTP API client.

Used by /admin/dlq/inspect to peek at DLQ / review-queue contents
without consuming. Credentials piggyback on the AMQP user (see
ConfigBundle conventions). For requeue, use the AMQP basic_get path
in nodes/dlq_admin (the management API has no transactional guarantees).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx


@dataclass
class RabbitMQManagementClient:
    base_url: str
    auth: tuple[str, str]
    vhost: str

    @classmethod
    def from_env(cls) -> RabbitMQManagementClient:
        host = os.environ["RABBITMQ_HOST"]
        port = os.getenv("RABBITMQ_MANAGEMENT_PORT", "15672")
        user = os.environ["RABBITMQ_USER"]
        pw = os.environ["RABBITMQ_PASSWORD"]
        vhost = os.getenv("RABBITMQ_VHOST", "/")
        return cls(
            base_url=f"http://{host}:{port}",
            auth=(user, pw),
            vhost=vhost,
        )

    async def peek_messages(
        self, *, queue: str, limit: int = 20,
    ) -> list[dict[str, Any]]:
        """List up to ``limit`` messages without consuming.

        Uses ackmode=ack_requeue_true so messages stay in the queue
        (the management API requeues them after the read).
        """
        vhost_enc = quote(self.vhost, safe="")
        url = f"{self.base_url}/api/queues/{vhost_enc}/{queue}/get"
        body = {
            "count": limit,
            "ackmode": "ack_requeue_true",
            "encoding": "auto",
            "truncate": 50000,
        }
        return await self._post_json(url, body)

    async def _post_json(self, url: str, body: dict[str, Any]) -> Any:
        async with httpx.AsyncClient(auth=self.auth, timeout=10.0) as c:
            r = await c.post(url, json=body)
            r.raise_for_status()
            return r.json()
