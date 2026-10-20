"""The contract types a round is built from, which the host and the plugins hand to the agent layer.

They live here, under the agent layer, because the agent layer uses them and must not import the
host (``scripts/check_package_cycles.py`` forbids ``agent -> host``): the host imports them from
here and does the assembling; an app's manifest (``App``, ``Participant``) is the host's own.

* :class:`Due` — a round a wake source decided should run (:mod:`app.host.turns`).
* :class:`AgentSpec` — one agent: the segments its prompt is made of, its model, its trace name,
  its budget of model calls, its Langfuse session. Its name is the key it is listed under (the
  original prompt id: ``living_life_moment``, ``world_perception`` …), which is also what a call
  of it is named (:class:`app.agent.round.Call`).
* :class:`Profile` — what a participant's round is made of, in one place and in order: the main
  agent and the sub-agents it may call, the tools in the order the model is given them, the state
  and input sections, the steps before the round, the checks, and the two phases at its end.
* :class:`HistorySpec`, :class:`CostSpec` — where a round's history is kept and how it is trimmed,
  and how its cost row is named.
* :class:`Switches` — the plugins a participant's round can switch off, and the Dynamic Config
  key that says which are on.
* :class:`ToolSpec` — a tool as a plugin declares it: how trimming treats what it returns, and
  whether calling it ends the round.
* :class:`Segment` — one Langfuse prompt a plugin contributes to an agent's prompt, and the
  variables its template must declare.
* :class:`FailRound` / :class:`Continue` — what a failing step after the round's commit does.
* :class:`FromConfig` — a value read from Dynamic Config when a call starts, with a fallback.

Format strings (a session, a history key, a cost row's names) are checked when the type is built:
a variable the string uses and the type does not provide is a code error, raised there rather
than at the first round.
"""

from __future__ import annotations

import asyncio
import logging
import string
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

from inner_shared.dynamic_config import dynamic_config

from app.agent.continuity import TrimPolicy
from app.agent.tooling import Tool

if TYPE_CHECKING:
    from app.agent.round import Round

logger = logging.getLogger(__name__)


class Due(Protocol):
    """A round a wake source's ``due`` decided should run (:class:`app.host.turns.WakeSource`).

    Each source implements it with a dataclass of its own: the round's identity, the time it runs
    at and what it carries along are that source's fields (hers: the round id, the time she got
    her turn, the message that woke her; world's: the messages the round takes). The queue reads
    only ``reason``.
    """

    # Why this round runs: written to the log line the queue writes when it runs a round.
    reason: str


def _format_variables(template: str) -> set[str]:
    """The variables ``template`` uses (``{name}``); a positional ``{}`` is refused."""
    used: set[str] = set()
    for _literal, name, _spec, _conversion in string.Formatter().parse(template):
        if name is None:
            continue
        base = name.split(".", 1)[0].split("[", 1)[0]
        if not base:
            raise ValueError(
                f"{template!r}: name every variable, a bare {{}} has nothing to fill it"
            )
        used.add(base)
    return used


def _check_format(what: str, template: str, allowed: frozenset[str]) -> None:
    unknown = _format_variables(template) - allowed
    if unknown:
        raise ValueError(
            f"{what} {template!r} uses {sorted(unknown)}; it can use only {sorted(allowed)}"
        )


@dataclass(frozen=True)
class FromConfig:
    """A value read from Dynamic Config each time it is needed: the keys in order, the first one
    set to a usable value wins, otherwise ``default``.

    Usable means non-blank for a text value, and a positive integer for an integer one (the type
    of ``default`` says which). A key set to something unusable is logged and skipped, so a typo
    in one key falls back rather than failing the call.
    """

    keys: tuple[str, ...]
    default: str | int

    def __post_init__(self) -> None:
        if not self.keys or not all(
            isinstance(k, str) and k.strip() for k in self.keys
        ):
            raise ValueError(
                f"FromConfig needs at least one non-blank key, got {self.keys!r}"
            )
        if isinstance(self.default, bool) or not isinstance(self.default, (str, int)):
            raise ValueError(
                f"FromConfig default is text or an integer, got {self.default!r}"
            )
        if isinstance(self.default, int) and self.default <= 0:
            raise ValueError(
                f"FromConfig integer default must be positive, got {self.default}"
            )
        if isinstance(self.default, str) and not self.default.strip():
            raise ValueError("FromConfig text default must not be blank")

    def _usable(self, raw: str) -> str | int | None:
        if isinstance(self.default, str):
            return raw
        try:
            number = int(raw)
        except ValueError:
            return None
        return number if number > 0 else None

    async def read(self) -> str | int:
        """The value now: the first key set to a usable value, else ``default``.

        Dynamic Config's fetch is synchronous HTTP on a cache miss, so it runs in a thread rather
        than stalling the event loop."""
        for key in self.keys:
            raw = (await asyncio.to_thread(dynamic_config.get, key, default="")).strip()
            if not raw:
                continue
            value = self._usable(raw)
            if value is not None:
                return value
            logger.warning(
                "dynamic config %s = %r is not a usable %s; trying the next key",
                key,
                raw,
                "positive integer" if isinstance(self.default, int) else "value",
            )
        return self.default


# The variables an agent's Langfuse session can use: the lane, the participant the call is for,
# and the date (Beijing time) of the moment the call is about.
SESSION_VARIABLES = frozenset({"lane", "participant", "date"})
# The variables an agent's answer to arguments that do not fit a tool can use.
BAD_ARGUMENTS_VARIABLES = frozenset({"tool", "error"})


@dataclass(frozen=True)
class AgentSpec:
    """One agent. Its name is the key it is listed under, not a field: the original prompt id.

    ``segments`` are the names of the segments its prompt is made of, in order
    (:class:`Segment`). ``model`` and ``recursion_limit`` (the most model calls with tools one
    call makes; the loop closes with one call without tools after it) may come from Dynamic
    Config. ``max_retries`` is how many times the whole call is tried on a temporary model
    failure. ``session`` is the Langfuse session its calls are grouped into, a format string over
    :data:`SESSION_VARIABLES`, or ``None`` for none. ``update_trace`` False leaves the trace it
    runs in named after its caller; ``separate_trace`` puts every call in a trace of its own.
    ``tools`` are the tools of an agent that is not a participant's main agent (a main agent's
    are its profile's). ``bad_arguments`` is what a tool call whose arguments do not fit its tool
    is answered with, a format string over :data:`BAD_ARGUMENTS_VARIABLES` (the tool's name, the
    error); ``None`` answers with the tool's name and the error alone.
    """

    segments: tuple[str, ...]
    model: str | FromConfig
    trace_name: str
    recursion_limit: int | FromConfig = 6
    max_retries: int = 1
    session: str | None = None
    model_kwargs: Mapping[str, Any] = field(default_factory=dict)
    update_trace: bool = True
    separate_trace: bool = False
    tools: tuple[str, ...] = ()
    bad_arguments: str | None = None

    def __post_init__(self) -> None:
        if not self.segments:
            raise ValueError(f"agent {self.trace_name!r} has no segments")
        if len(set(self.segments)) != len(self.segments):
            raise ValueError(
                f"agent {self.trace_name!r} lists a segment twice: {self.segments}"
            )
        if isinstance(self.recursion_limit, int) and self.recursion_limit < 1:
            raise ValueError(
                f"agent {self.trace_name!r}: recursion_limit must be at least 1"
            )
        if self.max_retries < 1:
            raise ValueError(
                f"agent {self.trace_name!r}: max_retries must be at least 1"
            )
        if self.session is not None:
            _check_format(
                f"agent {self.trace_name!r} session", self.session, SESSION_VARIABLES
            )
        if self.bad_arguments is not None:
            _check_format(
                f"agent {self.trace_name!r} bad_arguments",
                self.bad_arguments,
                BAD_ARGUMENTS_VARIABLES,
            )


@dataclass(frozen=True)
class HistoryTexts:
    """The words the history of a participant's rounds is trimmed and marked with: the head and
    the explanation of the two marker messages (a cleanup, a round that was not stored), what a
    trimmed tool result and a trimmed picture are replaced with, and what a tool call that never
    ran because the round ended before it is answered with.

    A marker message is its head, a line break, its note and the state laid out again. A head is a
    format string with ``{at}`` (the marker's time) once, between text before it and text that
    ends it: that is how a marker is found again in a stored history, and all a folded marker
    keeps. The two heads differ, so the two kinds of marker are told apart."""

    checkpoint_head: str
    checkpoint_note: str
    gap_head: str
    gap_note: str
    material_trimmed: str
    picture_trimmed: str
    never_ran: str

    def __post_init__(self) -> None:
        for name in ("checkpoint_head", "gap_head"):
            head = getattr(self, name)
            before, _, after = head.partition("{at}")
            if (
                _format_variables(head) != {"at"}
                or head.count("{at}") != 1
                or not before
                or not after
            ):
                raise ValueError(
                    f"history texts {name} {head!r}: put {{at}} once, between text before it "
                    f"and text that ends the head"
                )
        if self.checkpoint_head == self.gap_head:
            raise ValueError("history texts: the two marker heads must differ")


# The variables a history key can use.
HISTORY_KEY_VARIABLES = frozenset({"lane", "participant"})


@dataclass(frozen=True)
class HistorySpec:
    """Where a participant's history is kept (``key``, a format string over
    :data:`HISTORY_KEY_VARIABLES`), how it is trimmed, and the words it is trimmed with."""

    key: str
    policy: TrimPolicy
    texts: HistoryTexts

    def __post_init__(self) -> None:
        _check_format("history key", self.key, HISTORY_KEY_VARIABLES)


# The variables a cost row's actor and round id can use.
COST_ACTOR_VARIABLES = frozenset({"participant"})
COST_ROUND_VARIABLES = frozenset({"round_id", "trace"})


@dataclass(frozen=True)
class CostSpec:
    """How a round's cost row is named: ``actor`` over :data:`COST_ACTOR_VARIABLES`, ``round_id``
    over :data:`COST_ROUND_VARIABLES` (``trace`` is the main agent's trace name)."""

    actor: str
    round_id: str

    def __post_init__(self) -> None:
        _check_format("cost actor", self.actor, COST_ACTOR_VARIABLES)
        _check_format("cost round id", self.round_id, COST_ROUND_VARIABLES)


@dataclass(frozen=True)
class Switches:
    """The plugins a participant's round can leave out, and the Dynamic Config ``key`` naming the
    ones that are on (comma-separated; blank means all of them). Read once when a round starts."""

    key: str
    plugins: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValueError("Switches needs a Dynamic Config key")


@dataclass(frozen=True)
class Profile:
    """What a participant's round is made of. Every name here is one some plugin registered;
    the order of each tuple is the order the round uses it in. ``tools`` is also the order the
    model is given the main agent's tools in. ``join`` goes between two state sections and
    between two input sections."""

    agent: str
    sub_agents: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    state: tuple[str, ...] = ()
    input: tuple[str, ...] = ()
    join: str = "\n\n"
    round_start: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()
    commit: tuple[str, ...] = ()
    after_commit: tuple[str, ...] = ()
    history: HistorySpec | None = None
    round_log: str | None = None
    cost: CostSpec | None = None
    switches: Switches | None = None


# How trimming treats what a tool returned: "material" (something she read) is replaced with a
# short line once it is older than the policy's material minutes; "kept" stays as long as her own
# words do.
Trim = Literal["material", "kept"]
_TRIMS = ("material", "kept")


@dataclass(frozen=True)
class ToolSpec:
    """A tool as a plugin declares it. Its schema is reflected from ``tool`` unchanged."""

    tool: Tool
    trim: Trim
    ends_round: bool = False

    def __post_init__(self) -> None:
        if self.trim not in _TRIMS:
            raise ValueError(
                f"tool {self.tool.name!r}: trim is one of {_TRIMS}, got {self.trim!r}"
            )


# What a segment's variables are worked out with when a round starts.
VarsFn = Callable[["Round"], Awaitable[Mapping[str, str]]]


@dataclass(frozen=True)
class Segment:
    """One Langfuse prompt a plugin contributes. ``variables`` are the ones its template must
    declare, no more and no fewer; ``values`` works them out when a round starts, or is ``None``
    when the caller gives them at the call."""

    name: str
    prompt_id: str
    variables: tuple[str, ...] = ()
    values: VarsFn | None = None

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.prompt_id.strip():
            raise ValueError(f"a segment needs a name and a prompt id, got {self!r}")
        if len(set(self.variables)) != len(self.variables):
            raise ValueError(
                f"segment {self.name!r} lists a variable twice: {self.variables}"
            )


@dataclass(frozen=True)
class FailRound:
    """A step after the commit that fails fails the round."""


@dataclass(frozen=True)
class Continue:
    """A step after the commit that fails is logged as an error and counted; the round goes on."""
