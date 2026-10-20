"""Langfuse prompts, fetched together for a round and rendered strictly.

**A set of prompts is resolved once** (:meth:`PromptSet.resolve`): every segment the given agents
are made of is fetched (the lane's own label first outside prod, production when the lane has
none), each segment once however many agents list it, and checked: the variables its template
declares must be exactly the ones its plugin gives (:attr:`app.agent.contracts.Segment.variables`).
One missing or one too many is a configuration error (:class:`PromptConfigError`), raised before
any model is called; only what the template declares counts, ``{{...}}`` inside a value is text.
A segment whose values are worked out from the round (``Segment.values``) gets them here.

**Rendering** (:meth:`PromptSet.messages`): an agent's text segments are compiled in its order
and joined by a blank line into one SYSTEM message, so splitting a prompt at a blank line between
two sections and rendering the pieces gives back the same text byte for byte. A chat prompt is a
segment of its own and compiles into its messages by role. Values known only at the call (what the
output check judges) are given there; they must be exactly the variables of the segments that do
not work theirs out from the round.

**Where a set is resolved.** A participant's round resolves its main agent's and its sub-agents'
segments once, after its input is put together and before its first model call (the round keeps
them, ``Round.prompts``); a call made outside any round (a diary page, a look back, a stretch of
reading, an answer) resolves its own when it starts. A prompt released while a round runs is
rendered from the next round on.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from langfuse import Langfuse
from langfuse.model import ChatPromptClient, PromptClient, TextPromptClient

from app.agent.contracts import AgentSpec, Segment
from app.agent.neutral import Message, Role
from app.api.middleware import get_lane
from app.infra.config import settings

if TYPE_CHECKING:
    from app.agent.round import Round

logger = logging.getLogger(__name__)

_client: Langfuse | None = None
_PROMPT_CACHE_TTL_SECONDS: int = 10

# The label Langfuse serves when none is asked for.
PRODUCTION = "production"


def _get_client() -> Langfuse:
    """Lazily initialise and return the Langfuse singleton client."""
    global _client
    if _client is None:
        _client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
    return _client


def _fetch(prompt_id: str) -> tuple[PromptClient, str]:
    """The prompt ``prompt_id`` this process renders, and the label it was served under.

    Outside prod the lane's own label is tried first; a lane with no label of its own falls back
    to production.
    """
    lane = get_lane() or settings.lane
    if lane and lane != "prod":
        try:
            prompt = _get_client().get_prompt(
                prompt_id, label=lane, cache_ttl_seconds=_PROMPT_CACHE_TTL_SECONDS
            )
            return prompt, lane
        except Exception:
            logger.debug(
                "prompt %s has no lane label=%s, fallback to production",
                prompt_id,
                lane,
            )
    prompt = _get_client().get_prompt(
        prompt_id, label=None, cache_ttl_seconds=_PROMPT_CACHE_TTL_SECONDS
    )
    return prompt, PRODUCTION


_ROLE_MAP: dict[str, Role] = {
    "system": Role.SYSTEM,
    "user": Role.USER,
    "assistant": Role.ASSISTANT,
}


def compile_to_messages(prompt: Any, **variables: Any) -> list[Message]:
    """Compile a Langfuse prompt into neutral ``Message`` list.

    Text prompts become a single SYSTEM message.
    Chat prompts become a list of messages matching each role (unknown roles
    fall back to SYSTEM, mirroring the prior langchain behaviour).
    """
    if isinstance(prompt, TextPromptClient):
        return [Message(role=Role.SYSTEM, content=prompt.compile(**variables))]
    return [
        Message(
            role=_ROLE_MAP.get(m.get("role", ""), Role.SYSTEM),
            content=m.get("content", ""),
        )
        for m in prompt.compile(**variables)
    ]


class PromptConfigError(Exception):
    """A segment's template does not declare exactly the variables its plugin gives.

    ``missing`` are the ones the plugin gives and the template does not declare; ``unexpected``
    the ones the template declares and no plugin gives. Raised by :meth:`PromptSet.resolve`, so a
    round that meets it calls no model, runs no tool, records no cost and commits nothing.
    """

    def __init__(
        self,
        *,
        segment: str,
        prompt_id: str,
        label: str,
        version: int,
        missing: frozenset[str],
        unexpected: frozenset[str],
    ) -> None:
        self.segment = segment
        self.prompt_id = prompt_id
        self.label = label
        self.version = version
        self.missing = missing
        self.unexpected = unexpected
        super().__init__(
            f"segment {segment!r}: prompt {prompt_id!r} (label {label!r}, version {version}) "
            f"does not declare {sorted(missing)} and declares {sorted(unexpected)} that no "
            f"plugin gives"
        )


@dataclass(frozen=True)
class _Resolved:
    """One segment as this set renders it: the prompt fetched for it, the label it was served
    under, and its values when they were worked out from the round."""

    segment: Segment
    prompt: PromptClient
    label: str
    values: Mapping[str, str] | None


def _declared(prompt: PromptClient) -> frozenset[str]:
    return frozenset(prompt.variables)


class PromptSet:
    """The prompts some agents render, at one fixed version each (see the module docstring)."""

    def __init__(self, by_agent: Mapping[str, tuple[_Resolved, ...]]) -> None:
        self._by_agent = dict(by_agent)

    @classmethod
    async def resolve(
        cls,
        agents: Mapping[str, AgentSpec],
        segments: Mapping[str, Segment],
        round: Round | None,
    ) -> PromptSet:
        """Fetch and check every segment of ``agents`` (by name). A template that does not declare
        exactly its segment's variables raises :class:`PromptConfigError`. A segment nobody
        registered, a chat prompt listed with other segments, or values worked out from the round
        when there is no round, is a code error (``ValueError``)."""
        resolved: dict[str, _Resolved] = {}
        for agent, spec in agents.items():
            for name in spec.segments:
                if name in resolved:
                    continue
                try:
                    segment = segments[name]
                except KeyError:
                    raise ValueError(
                        f"agent {agent!r} lists segment {name!r}, which nobody registered"
                    ) from None
                prompt, label = _fetch(segment.prompt_id)
                wanted, declared = frozenset(segment.variables), _declared(prompt)
                if declared != wanted:
                    raise PromptConfigError(
                        segment=name,
                        prompt_id=segment.prompt_id,
                        label=label,
                        version=prompt.version,
                        missing=wanted - declared,
                        unexpected=declared - wanted,
                    )
                values = None
                if segment.values is not None:
                    if round is None:
                        raise ValueError(
                            f"segment {name!r} works its values out from a round; "
                            f"agent {agent!r} is resolved outside one"
                        )
                    values = dict(await segment.values(round))
                    if frozenset(values) != wanted:
                        raise ValueError(
                            f"segment {name!r} worked out {sorted(values)}, "
                            f"not its variables {sorted(wanted)}"
                        )
                resolved[name] = _Resolved(segment, prompt, label, values)
            parts = [resolved[name] for name in spec.segments]
            if len(parts) > 1 and any(
                isinstance(p.prompt, ChatPromptClient) for p in parts
            ):
                raise ValueError(
                    f"agent {agent!r}: a chat prompt is a segment of its own, "
                    f"not one of {list(spec.segments)}"
                )
        return cls(
            {
                agent: tuple(resolved[n] for n in spec.segments)
                for agent, spec in agents.items()
            }
        )

    def messages(
        self, agent: str, values: Mapping[str, str] | None = None
    ) -> list[Message]:
        """``agent``'s prompt, compiled. ``values`` are the variables known only at the call; they
        must be exactly those of the segments that did not work theirs out from the round."""
        parts = self._by_agent[agent]
        given = dict(values or {})
        expected = {v for p in parts if p.values is None for v in p.segment.variables}
        if set(given) != expected:
            raise ValueError(
                f"agent {agent!r} is given {sorted(given)} at the call; "
                f"its segments need {sorted(expected)}"
            )
        if len(parts) == 1 and isinstance(parts[0].prompt, ChatPromptClient):
            (chat,) = parts
            return compile_to_messages(chat.prompt, **self._values_of(chat, given))
        texts = [p.prompt.compile(**self._values_of(p, given)) for p in parts]
        return [Message(role=Role.SYSTEM, content="\n\n".join(texts))]

    @staticmethod
    def _values_of(part: _Resolved, given: Mapping[str, str]) -> dict[str, str]:
        if part.values is not None:
            return dict(part.values)
        return {name: given[name] for name in part.segment.variables}

    def versions(self, agent: str) -> list[tuple[str, str, int]]:
        """``(prompt_id, label, version)`` of each of ``agent``'s segments, in its order."""
        return [
            (p.segment.prompt_id, p.label, p.prompt.version)
            for p in self._by_agent[agent]
        ]

    def single_prompt(self, agent: str) -> PromptClient | None:
        """The one prompt ``agent`` is rendered from, which its model calls link in Langfuse;
        ``None`` when it is rendered from several segments (a model call links one prompt, and
        linking one segment would count that segment's use as the whole prompt's)."""
        parts = self._by_agent[agent]
        return parts[0].prompt if len(parts) == 1 else None
