"""The model-call boundary: every ``ModelClient`` the code builds answers from a script.

Interception point: ``app.agent.client.build_model_client``. Model resolution
(``resolve_model_info``) is replaced so every model id resolves to the ``replay`` client type,
and that client type is registered as :class:`ScriptedModel`. Everything above it runs for real:
the agent loop, tool dispatch, retries, transcript collection, prompt compilation.

A script is a queue of replies per *agent*, where the agent is the Langfuse prompt the call was
rendered from (``living_life_moment``, ``world_round``, ``guard_output_safety``…). Calls from
different agents can interleave (a perception judgement runs inside a world tool) without the
scenario having to predict the interleaving. A reply is a :class:`Reply`, a :class:`Fail`, or a
callable taking the :class:`Request` and returning one of those (for replies that copy something
out of the conversation, the way a model would).

Each call is recorded in full: the agent, the prompt version and the variables it was compiled
with, the model id, call options, the tool definitions offered, every message sent, and what the
script answered. An image block that points into the replay's object store also says what
fetching it at the time of the call returns (``"fetched"``; see
:mod:`tests.replay.harness.objects`), since that is what the real adapter would send. Usage is
reported through the same generation span the real adapters use, so cost rows are produced by
the code under test, not by the harness.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.agent.client import ModelClient
from app.agent.neutral import Message, ToolCall, ToolDef, TurnPart
from app.agent.trace import _current_prompt, generation_span
from tests.replay.harness.errors import describe_error

DEFAULT_USAGE = {
    "input": 1000,
    "output": 100,
    "total": 1100,
    "cache_read_input_tokens": 400,
}


@dataclass(frozen=True)
class ToolUse:
    """One tool call in a scripted reply. ``id`` defaults to ``<agent>:<call>:<n>``."""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    id: str | None = None
    signature: bytes | None = None


@dataclass(frozen=True)
class Reply:
    """One scripted model turn.

    ``thought`` / ``thought_signature`` put a THOUGHT part first in the turn (the way gemini
    returns one), so a replay checks that the signature survives storage and is sent back.
    ``data`` is the answer to a structured call (``Agent.extract``).
    """

    text: str = ""
    tools: tuple[ToolUse, ...] = ()
    thought: str | None = None
    thought_signature: bytes | None = None
    usage: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_USAGE))
    data: dict[str, Any] | None = None


@dataclass(frozen=True)
class Fail:
    """The provider call fails with ``make()``'s exception."""

    make: Callable[[], BaseException]


@dataclass
class Request:
    """What a scripted reply can look at: the call as the provider would receive it."""

    agent: str | None
    number: int
    messages: list[Message]
    tools: list[ToolDef]

    def tool_results(self) -> list[str]:
        """Text of every tool-result turn in this request, oldest first."""
        return [m.text() for m in self.messages if m.tool_call_id is not None]


Scripted = Reply | Fail | Callable[[Request], "Reply | Fail"]


class ScriptExhausted(AssertionError):
    """A model call found its agent's script empty.

    Raising it is not enough: product code can swallow it (an ``except Exception`` around a
    tool, a fail-open check, ``gather(return_exceptions=True)`` in a tick), and then the step
    passes with a call that has neither a reply nor an error. :attr:`ModelScript.exhausted`
    remembers every such call, and :meth:`tests.replay.harness.replay.Replay.check` fails on it.
    """


class ModelScript:
    """The scripted replies, and the record of every call made against them."""

    def __init__(self, effects, objects=None) -> None:
        self._effects = effects
        # The object store (:mod:`tests.replay.harness.objects`): image blocks that point into
        # it are recorded with what fetching them at the time of the call returns.
        self._objects = objects
        self._queues: dict[str | None, deque[Scripted]] = defaultdict(deque)
        self._numbers: dict[str | None, int] = defaultdict(int)
        # Raw call records, in the order the calls were made. The step that is open when a call
        # happens takes them (see :class:`tests.replay.harness.replay.Replay`).
        self.calls: list[dict[str, Any]] = []
        # Every call that found its agent's script empty, as (agent, call number), whatever the
        # code under test then did with the ScriptExhausted.
        self.exhausted: list[tuple[str | None, int]] = []

    def script(self, agent: str, *replies: Scripted) -> None:
        self._queues[agent].extend(replies)

    def unused(self) -> dict[str, int]:
        return {str(a): len(q) for a, q in self._queues.items() if q}

    def _next(self, agent: str | None, request: Request) -> Reply | Fail:
        queue = self._queues.get(agent)
        if not queue:
            self.exhausted.append((agent, request.number))
            raise ScriptExhausted(
                f"replay: agent {agent!r} made model call #{request.number} but its script is "
                f"empty; add a reply for it"
            )
        step = queue.popleft()
        if not isinstance(step, (Reply, Fail)):
            step = step(request)
        return step

    def _recorded(self, message: Message) -> dict[str, Any]:
        recorded = message.to_replay_dict()
        return recorded if self._objects is None else self._objects.annotate(recorded)

    def _open(
        self, kind: str, model: str, messages, tools, kwargs
    ) -> tuple[dict, Request]:
        self._effects.alive()
        prompt = _current_prompt.get()
        agent = getattr(prompt, "name", None)
        self._numbers[agent] += 1
        number = self._numbers[agent]
        record: dict[str, Any] = {
            "agent": agent,
            "number": number,
            "call": kind,
            "model": model,
            "prompt": None
            if prompt is None
            else {
                "name": prompt.name,
                "version": prompt.version,
                "variables": getattr(prompt, "replay_variables", None),
            },
            "options": dict(kwargs),
            "tool_defs": [
                {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                }
                for t in tools or []
            ],
            "messages": [self._recorded(m) for m in messages],
        }
        self.calls.append(record)
        return record, Request(agent, number, list(messages), list(tools or []))

    async def complete(self, model: str, messages, tools, kwargs) -> Message:
        record, request = self._open("complete", model, messages, tools, kwargs)
        with generation_span(name=model, model=model, input=record["messages"]) as span:
            step = self._next(request.agent, request)
            if isinstance(step, Fail):
                error = step.make()
                record["raised"] = describe_error(error)
                raise error
            reply = _turn(step, tag=f"{request.agent}:{request.number}")
            span.update(output=reply.to_dict(), usage_details=dict(step.usage))
        record["reply"] = reply.to_replay_dict()
        return reply

    async def structured(self, model: str, messages, schema, kwargs) -> dict[str, Any]:
        record, request = self._open("structured", model, messages, None, kwargs)
        record["schema"] = schema
        with generation_span(name=model, model=model, input=record["messages"]) as span:
            step = self._next(request.agent, request)
            if isinstance(step, Fail):
                error = step.make()
                record["raised"] = describe_error(error)
                raise error
            if step.data is None:
                raise AssertionError(
                    f"replay: {request.agent!r} call #{request.number} is structured; script a "
                    f"Reply(data=...)"
                )
            span.update(output=step.data, usage_details=dict(step.usage))
        record["reply"] = {"data": step.data}
        return dict(step.data)


def _turn(reply: Reply, *, tag: str) -> Message:
    parts: list[TurnPart] = []
    calls: list[ToolCall] = []
    if reply.thought is not None:
        parts.append(
            TurnPart.from_thought(reply.thought, signature=reply.thought_signature)
        )
    if reply.text:
        parts.append(TurnPart.from_text(reply.text))
    for n, use in enumerate(reply.tools, 1):
        call = ToolCall(
            id=use.id or f"{tag}:{n}",
            name=use.name,
            arguments=dict(use.arguments),
            signature=use.signature,
        )
        calls.append(call)
        parts.append(TurnPart.from_tool_call(call))
    return Message.from_model_turn(parts, calls)


class ScriptedModel(ModelClient):
    """The ``replay`` client type: one instance per ``build_model_client`` call."""

    def __init__(self, script: ModelScript, model: str) -> None:
        self._script = script
        self._model = model

    async def complete(self, messages, *, tools=None, **kwargs) -> Message:
        return await self._script.complete(self._model, messages, tools, kwargs)

    def stream(self, messages, *, tools=None, **kwargs):
        raise NotImplementedError(
            "replay: no round kind streams; script complete() instead"
        )

    async def structured(self, messages, *, schema, **kwargs) -> dict[str, Any]:
        return await self._script.structured(self._model, messages, schema, kwargs)


def install(script: ModelScript, monkeypatch) -> None:
    import app.agent.client as client_mod

    async def resolve(model_id: str, **_: Any) -> dict[str, Any]:
        return {
            "client_type": "replay",
            "model_name": model_id,
            "api_key": "replay",
            "base_url": "replay",
        }

    def factory(*, model_name: str, **_: Any) -> ScriptedModel:
        return ScriptedModel(script, model_name)

    client_mod._ensure_adapters_loaded()
    monkeypatch.setattr(client_mod, "resolve_model_info", resolve)
    monkeypatch.setitem(client_mod._ADAPTERS, "replay", factory)
