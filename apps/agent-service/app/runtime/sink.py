"""Sink specs: declarative descriptors for outbound edges of a wire.

A ``SinkSpec`` names an out-of-graph destination that receives Data
from a wire. The runtime stays at the protocol layer — it only knows
how to write a RabbitMQ queue. Business-specific destinations (feishu
``im/v1/messages`` API, external webhooks, …) live in dedicated
consumer services that read from those queues; the graph publishes,
they consume.

Example:
    ``wire(Reply).to(Sink.mq("chat_response"))`` — agent-service emits
    Reply onto the ``chat_response`` queue; ``chat-response-worker``
    (a separate TS deployment) consumes it and calls Lark.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SinkSpec:
    kind: str
    params: dict = field(default_factory=dict)


class Sink:
    @staticmethod
    def mq(queue: str) -> SinkSpec:
        """Publish each Data to ``queue`` on the shared RabbitMQ exchange.

        Body is the Data's JSON serialization. Lane suffixing follows
        the same convention as ``Source.mq`` (``"chat_response"`` becomes
        ``"chat_response_{lane}"`` outside prod).
        """
        return SinkSpec("mq", {"queue": queue})
