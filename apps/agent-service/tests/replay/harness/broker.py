"""RabbitMQ at the client boundary: the ``mq`` singleton's methods are replaced by an in-memory
broker with queues, bindings and consumers.

Interception point: the methods of ``app.infra.rabbitmq.mq`` (every module shares that one
instance) plus ``app.runtime.durable.publish_with_confirm``, an alias bound at import time.
Everything above it is real: the messaging layer's records, claims, retries and settle
decisions; sink dispatch for the two outbound queues; the durable and debounce consumers.

What the broker does and does not do on its own:

* A publish is routed by routing key to the queues bound to it, the way the main exchange
  routes. It is recorded as an event: target queues, routing key, delay, headers, body, and for
  ``publish_with_confirm`` whether the broker confirmed.
* Nothing is delivered on its own, except to consumers that do not acknowledge (a process's
  private reply queue). The scenario delivers explicitly (:meth:`FakeBroker.deliver`), which
  runs the registered consumer callback in its own task, as aio-pika would, and records the
  delivery and then how the consumer settled it (ack, reject, nack) as two timeline entries,
  so everything the consumer did sits between them. A rejected message with ``requeue`` goes back
  to the front of its queue; one rejected without requeue goes to the dead-letter queue its
  queue names.
* Another process's inbox is declared with :meth:`FakeBroker.declare_inbox`. Questions sent to
  it can be answered by a callback standing in for that process; the answer goes to the asker's
  reply queue in the shape the messaging layer replies with, and is not recorded as an event
  (it is input, not output of the code under test).
* :meth:`FakeBroker.refuse_confirms` makes ``publish_with_confirm`` report "not confirmed" for
  matching routing keys (a send failure); the message is then not queued.
* A killed process (:mod:`tests.replay.harness.timeline`) publishes nothing more. A delivery
  whose consumer was killed is never settled; RabbitMQ puts such a message back on its queue when
  the dead consumer's channel closes, and :meth:`FakeBroker.requeue_unsettled` does that (marked
  ``redelivered``) when the scenario calls it, typically right after ``restart()``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import itertools
import json
from collections import deque
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from app.infra.rabbitmq import (
    ISOLATED_DEAD_LETTERS,
    Route,
    _lane_rk,
    current_lane,
    lane_queue,
    mq,
)
from tests.replay.harness.timeline import ProcessKilled

_MQ_METHODS = (
    "connect",
    "declare_topology",
    "declare_route",
    "publish",
    "publish_with_confirm",
    "queue_exists",
    "open_channel",
    "declare_private_queue",
    "consume",
    "close",
)


# A process's own reply queue: server-named, so its consumer is not part of what the app opens.
_PRIVATE_PREFIX = "amq.gen-"


def _resolve_lane(lane: Any) -> str | None:
    if lane is ...:
        lane = current_lane()
    return None if lane in (None, "prod") else lane


@dataclass
class _Queued:
    body: bytes
    headers: dict[str, Any]
    routing_key: str
    redelivered: bool = False


@dataclass
class _Consumer:
    tag: str
    callback: Callable
    no_ack: bool


@dataclass
class FakeQueue:
    name: str
    dead_letter_to: str | None = None
    messages: deque[_Queued] = field(default_factory=deque)
    consumers: list[_Consumer] = field(default_factory=list)
    broker: FakeBroker | None = None

    async def consume(self, callback, no_ack: bool = False, **_: Any) -> str:
        tag = f"ctag-{self.name}-{len(self.consumers) + 1}"
        self.consumers.append(_Consumer(tag, callback, no_ack))
        if self.broker is not None:
            if not self.name.startswith(_PRIVATE_PREFIX):
                self.broker.consumers_started.append(self.name)
            if no_ack:
                self.broker._drain(self)
        return tag

    async def cancel(self, tag: str, **_: Any) -> None:
        self.consumers = [c for c in self.consumers if c.tag != tag]

    async def bind(self, *_: Any, **__: Any) -> None:
        return None


class FakeChannel:
    def __init__(self, broker: FakeBroker) -> None:
        self._broker = broker
        self.is_closed = False
        self.close_callbacks: set = set()

    async def get_queue(self, name: str, *_: Any, **__: Any) -> FakeQueue:
        queue = self._broker.queues.get(name)
        if queue is None:
            raise LookupError(f"replay broker: queue {name!r} was never declared")
        return queue

    async def set_qos(self, *_: Any, **__: Any) -> None:
        return None

    async def close(self) -> None:
        self.is_closed = True


class FakeIncoming:
    """What a consumer callback receives; remembers how the consumer settled it."""

    def __init__(self, queued: _Queued, channel: FakeChannel) -> None:
        self.body = queued.body
        self.headers = dict(queued.headers)
        self.routing_key = queued.routing_key
        self.redelivered = queued.redelivered
        self.channel = channel
        self.settled: str | None = None
        self._queued = queued

    async def ack(self, *_: Any, **__: Any) -> None:
        self.settled = self.settled or "ack"

    async def nack(self, requeue: bool = True, *_: Any, **__: Any) -> None:
        self.settled = self.settled or ("nack(requeue)" if requeue else "nack")

    async def reject(self, requeue: bool = False, *_: Any, **__: Any) -> None:
        self.settled = self.settled or ("reject(requeue)" if requeue else "reject")

    @asynccontextmanager
    async def process(
        self, requeue: bool = False, ignore_processed: bool = False, **_: Any
    ):
        try:
            yield self
        except BaseException:
            if self.settled is None:
                await self.reject(requeue=requeue)
            raise
        if self.settled is None and not ignore_processed:
            await self.ack()


Answerer = Callable[[Any], "str | None"]


class FakeBroker:
    def __init__(self, effects) -> None:
        self.queues: dict[str, FakeQueue] = {}
        self._bindings: dict[str, set[str]] = {}
        self._answerers: dict[str, Answerer] = {}
        self._refused: list[list] = []
        self._private = itertools.count(1)
        self._tasks: set[asyncio.Task] = set()
        # Deliveries whose consumer was killed before settling them: (queue, message).
        self._unsettled: list[tuple[str, _Queued]] = []
        # The step's effects timeline (shared with the database and file recorders), and the
        # queues that got a consumer.
        self._effects = effects
        self.consumers_started: list[str] = []

    # ------------------------------------------------------------------ setup

    def install(self, monkeypatch) -> None:
        import app.runtime.durable as durable

        for name in _MQ_METHODS:
            monkeypatch.setattr(mq, name, getattr(self, name))
        monkeypatch.setattr(durable, "publish_with_confirm", self.publish_with_confirm)

    def declare_inbox(
        self, participant: str, *, answers: Answerer | None = None
    ) -> None:
        """Another process's inbox (and its question queue) on this lane.

        ``answers`` stands in for that process answering questions: it gets the question as a
        messaging ``Message`` and returns the answer text, or ``None`` for "no answer".
        """
        from app.messaging.broker import inbox_route, lane, question_route

        for route in (inbox_route(participant), question_route(participant)):
            self._declare(route, lane())
        if answers is not None:
            self._answerers[lane_queue(question_route(participant).queue, lane())] = (
                answers
            )

    def inject(self, queue_name: str, body: dict, headers: dict | None = None) -> None:
        """Put a message on ``queue_name`` as another process would have; not recorded."""
        queue = self.queues.get(queue_name)
        if queue is None:
            raise LookupError(f"replay broker: queue {queue_name!r} was never declared")
        queue.messages.append(
            _Queued(json.dumps(body).encode(), dict(headers or {}), queue_name)
        )

    def refuse_confirms(
        self, matches: Callable[[str], bool], *, times: int = 1
    ) -> None:
        """The next ``times`` ``publish_with_confirm`` calls to a routing key ``matches`` accepts
        are not confirmed."""
        self._refused.append([matches, times])

    # ------------------------------------------------------------------ mq surface

    async def connect(self) -> None:
        return None

    async def declare_topology(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def declare_route(self, route: Route, lane: Any = ...) -> None:
        self._declare(route, _resolve_lane(lane))

    async def queue_exists(self, name: str) -> bool:
        return name in self.queues

    async def open_channel(self, prefetch_count: int = 10) -> FakeChannel:
        return FakeChannel(self)

    async def declare_private_queue(
        self, channel, route: Route, lane: str | None
    ) -> FakeQueue:
        queue = self._queue(f"{_PRIVATE_PREFIX}{next(self._private)}")
        self._bindings.setdefault(_lane_rk(route.rk, lane), set()).add(queue.name)
        return queue

    async def consume(self, queue_name: str, callback) -> tuple[FakeQueue, str]:
        queue = self.queues.get(queue_name)
        if queue is None:
            raise LookupError(f"replay broker: queue {queue_name!r} was never declared")
        return queue, await queue.consume(callback)

    async def publish(
        self,
        route: Route,
        body: dict,
        delay_ms: int | None = None,
        headers: dict | None = None,
        lane: Any = ...,
    ) -> None:
        self._publish(route, body, delay_ms, headers, _resolve_lane(lane), confirm=None)

    async def publish_with_confirm(
        self,
        route: Route,
        body: dict,
        *,
        delay_ms: int | None = None,
        headers: dict | None = None,
        lane: Any = ...,
        timeout_s: float = 5.0,
    ) -> bool:
        return self._publish(
            route, body, delay_ms, headers, _resolve_lane(lane), confirm=True
        )

    # ------------------------------------------------------------------ delivery

    def queued(self, queue_name: str) -> list[dict[str, Any]]:
        """The bodies waiting on ``queue_name``, oldest first."""
        return [json.loads(q.body) for q in self.queues[queue_name].messages]

    async def deliver(
        self, queue_name: str, *, picking: Callable[[dict], bool] | None = None
    ) -> str:
        """Hand the oldest message on ``queue_name`` (or the oldest one ``picking`` accepts) to
        its consumer, wait for the consumer to settle it, and return how it settled."""
        self._effects.alive()
        queue = self.queues.get(queue_name)
        if queue is None or not queue.messages:
            raise LookupError(f"replay broker: nothing queued on {queue_name!r}")
        if not queue.consumers:
            raise LookupError(f"replay broker: {queue_name!r} has no consumer")
        index = 0
        if picking is not None:
            index = next(
                i for i, q in enumerate(queue.messages) if picking(json.loads(q.body))
            )
        queued = queue.messages[index]
        del queue.messages[index]
        incoming = FakeIncoming(queued, FakeChannel(self))
        body = json.loads(queued.body)
        message_id = body.get("message_id") if isinstance(body, dict) else None
        self._effects.append({"deliver": queue_name, "message_id": message_id})
        settled: dict[str, Any] = {
            "settled": None,
            "queue": queue_name,
            "message_id": message_id,
        }
        task = asyncio.get_running_loop().create_task(
            queue.consumers[0].callback(incoming)
        )
        try:
            await task
        except ProcessKilled:
            self._unsettled.append((queue_name, queued))
            raise
        except Exception as exc:
            settled["consumer_raised"] = f"{type(exc).__name__}: {exc}"
        settled["settled"] = incoming.settled
        self._effects.append(settled)
        if incoming.settled in ("reject(requeue)", "nack(requeue)"):
            queue.messages.appendleft(queued)
        elif incoming.settled in ("reject", "nack") and queue.dead_letter_to:
            self._queue(queue.dead_letter_to).messages.append(queued)
        return incoming.settled or "unsettled"

    def requeue_unsettled(self) -> int:
        """Put every message a killed consumer never settled back at the front of its queue,
        marked redelivered, as RabbitMQ does once the dead consumer's channel closes. Not
        recorded (it is the broker's doing, not the code's). Returns how many."""
        count = len(self._unsettled)
        for queue_name, queued in reversed(self._unsettled):
            self.queues[queue_name].messages.appendleft(
                dataclasses.replace(queued, redelivered=True)
            )
        self._unsettled.clear()
        return count

    async def settle(self) -> None:
        """Wait for deliveries the broker started on its own (private reply queues)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # ------------------------------------------------------------------ internals

    def _queue(self, name: str, dead_letter_to: str | None = None) -> FakeQueue:
        queue = self.queues.get(name)
        if queue is None:
            queue = self.queues[name] = FakeQueue(name, dead_letter_to, broker=self)
        return queue

    def _declare(self, route: Route, lane: str | None) -> None:
        dead = lane_queue(ISOLATED_DEAD_LETTERS, lane) if route.isolated else None
        if dead:
            self._queue(dead)
        name = lane_queue(route.queue, lane)
        self._queue(name, dead)
        self._bindings.setdefault(_lane_rk(route.rk, lane), set()).add(name)

    def _refuses(self, rk: str) -> bool:
        for rule in self._refused:
            if rule[1] > 0 and rule[0](rk):
                rule[1] -= 1
                return True
        return False

    def _publish(self, route, body, delay_ms, headers, lane, *, confirm) -> bool:
        self._effects.alive()
        if not route.isolated and route.queue:
            # The topology (prod) or the lazy lane declare makes sure the queue exists.
            self._declare(route, lane)
        rk = _lane_rk(route.rk, lane)
        targets = sorted(self._bindings.get(rk, ()))
        event: dict[str, Any] = {"publish": targets or None, "rk": rk}
        if delay_ms is not None:
            event["delay_ms"] = delay_ms
        event["headers"] = dict(headers or {})
        event["body"] = body
        accepted = not (confirm and self._refuses(rk))
        if confirm:
            event["confirmed"] = accepted
        self._effects.append(event)
        if not accepted:
            return False
        message_headers = dict(headers or {})
        if delay_ms is not None:
            message_headers["x-delay"] = delay_ms
        for name in targets:
            if name in self._answerers:
                self._answer(name, body, message_headers)
                continue
            queue = self.queues[name]
            queue.messages.append(
                _Queued(json.dumps(body).encode(), message_headers, rk)
            )
            self._drain(queue)
        return True

    def _drain(self, queue: FakeQueue) -> None:
        """Deliver straight away to a consumer that does not acknowledge (a reply queue)."""
        consumer = next((c for c in queue.consumers if c.no_ack), None)
        while consumer is not None and queue.messages:
            incoming = FakeIncoming(queue.messages.popleft(), FakeChannel(self))
            self._spawn(consumer.callback(incoming))

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _answer(self, question_queue: str, body: dict, headers: dict) -> None:
        from app.messaging.broker import lane
        from app.messaging.message import Kind, Message, new_message
        from app.messaging.sending import REPLY_RK_HEADER

        question = Message.from_json(body)
        text = self._answerers[question_queue](question)
        if text is None:
            reply: dict[str, Any] = {
                "in_reply_to": question.message_id,
                "message": None,
                "reason": "对方没有给出回答",
            }
        else:
            answer = new_message(
                sender=question.recipient,
                recipient=question.sender,
                body=text,
                kind=Kind.ANSWER,
            )
            reply = {"in_reply_to": question.message_id, "message": answer.to_json()}
        rk = _lane_rk(headers[REPLY_RK_HEADER], lane())
        for name in self._bindings.get(rk, ()):
            queue = self.queues[name]
            queue.messages.append(_Queued(json.dumps(reply).encode(), {}, rk))
            self._drain(queue)
