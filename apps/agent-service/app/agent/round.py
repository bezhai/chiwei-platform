"""A participant's round and each agent call: who they are for, and where a call's state lives.

**A round** (:class:`Round`) is one turn of a participant: its identity and the time it runs at
(decided by the wake source that started it), why it runs, and what it read and decided on the
way. Whoever runs the round binds it (:func:`in_round`) for everything the round does, before the
model and after it; tools and the steps around the model read it with :func:`current_round`.

**A call** (:class:`Call`) is one run of one agent: its name (the agent's spec name, which is the
original prompt id), its Langfuse session already formatted, and the round it was made in, if
any. The caller opens it (:func:`open_call`), hands it to ``Agent.run`` / ``Agent.extract`` and
keeps holding it: the agent binds it as the current call while it runs (a tool reads it with
:func:`current_call`) and restores the call that was current before when it returns, raises or is
cancelled. So after a sub-agent's call returns inside a tool, the tool is back in its own call,
and what the sub-agent's tools noted is read from the call object the tool held
(``call.state(...)``), not from the context.

**State is by type.** A plugin keeps what its tools note during a round (or during one call) in a
class of its own and asks for it by that class: ``current_round().state(Glances)``,
``call.state(Judgments)``. The first ask creates it with ``kind()``. A round's state lives as long
as the round; a call's own state is emptied at the start of each attempt the agent makes, so a
failed attempt leaves nothing behind for the one that replaces it.

The bindings are context variables: every asyncio task sees its own, so three sisters' diaries
or readings running at once never see each other's call or round.

**The fixed loop** (:func:`run_round`) runs a participant's round from a profile the host assembled
(:class:`AssembledProfile`: the manifest's names replaced by what the plugins registered under
them). In order: the steps before the round; the round before it (:class:`RoundLog`) and the
history; the state sections and the input sections, joined in the profile's order (a section
that gives ``None`` is left out); the segments of the main agent and its sub-agents, fixed for
the round; the main agent with the profile's tools; the cost; the checks; the commit steps in one
transaction (none when there is none); the steps after the commit, each of which says whether its
failure fails the round (:class:`~app.agent.contracts.FailRound`) or is logged and counted
(:class:`~app.agent.contracts.Continue`). Storing the history is one of those steps
(:func:`store_history`), which the plugin that keeps it registers with its own failure handling.

**Switches.** A profile may name plugins its rounds can leave out
(:class:`~app.agent.contracts.Switches`): the loop reads the Dynamic Config key once, before
anything else in the round, and leaves every tool, section and step of a plugin that is switched
off out of that round. Trimming still counts the tools of a plugin that is switched off as
material: what they returned earlier is still in the history.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Generic, TypeVar

from inner_shared.dynamic_config import dynamic_config

from app.agent.continuity import (
    HistoryView,
    commit_transcript,
    continues_unbroken,
    next_transcript,
    trim_for_round,
)
from app.agent.contracts import (
    AgentSpec,
    Continue,
    Due,
    FailRound,
    Profile,
    Segment,
    Switches,
    ToolSpec,
)
from app.agent.neutral import Message, Role
from app.agent.prompts import PromptSet
from app.agent.session import load_session
from app.agent.tools._common import get_or_create_counter
from app.agent.trace import collect_usage
from app.data.tx import tx
from app.domain.thinking_cost import record_round_cost
from app.infra.cst_time import CST

logger = logging.getLogger(__name__)

S = TypeVar("S")
T = TypeVar("T")


@dataclass
class Round:
    """One round of one participant. ``round_id`` and ``now`` are decided by the wake source
    (hers when her queue gives her the turn; world's when its round starts); ``due`` is what the
    source decided, with its own fields (the message that woke her, the messages world takes).
    ``previous_at``, ``history``, ``prompts`` (the segments of the main agent and of the sub-agents
    it may call, at the versions fixed for this round) and ``reply`` are filled in as the round gets
    to them."""

    app: str
    lane: str
    participant: str
    profile: str
    round_id: str
    now: datetime
    reason: str
    due: Due
    previous_at: datetime | None = None
    history: HistoryView | None = None
    prompts: PromptSet | None = None
    reply: Message | None = None
    _states: dict[type, Any] = field(default_factory=dict, repr=False, compare=False)

    def state(self, kind: type[S]) -> S:
        """This round's ``kind`` state; created with ``kind()`` the first time it is asked for."""
        if kind not in self._states:
            self._states[kind] = kind()
        return self._states[kind]


@dataclass
class Call:
    """One run of one agent, opened and held by its caller (:func:`open_call`)."""

    agent: str
    session: str | None
    round: Round | None
    _states: dict[type, Any] = field(default_factory=dict, repr=False, compare=False)

    def state(self, kind: type[S]) -> S:
        """This call's ``kind`` state; created with ``kind()`` the first time it is asked for."""
        if kind not in self._states:
            self._states[kind] = kind()
        return self._states[kind]

    def clear_state(self) -> None:
        """Forget every state of this call. The agent does it at the start of each attempt."""
        self._states.clear()


def open_call(
    agent: str,
    spec: AgentSpec,
    *,
    lane: str,
    participant: str | None = None,
    at: datetime | None = None,
    round: Round | None = None,
) -> Call:
    """A call of ``agent`` (the name ``spec`` is listed under), with its session formatted.

    The session's ``{lane}``, ``{participant}`` and ``{date}`` (the Beijing-time date of ``at``)
    come from the arguments; a call made in a round takes the round's participant and time where
    they are not given. A variable the session uses and nothing gives is a code error
    (``ValueError``).
    """
    if round is not None:
        participant = round.participant if participant is None else participant
        at = round.now if at is None else at
    session = None
    if spec.session is not None:
        values = {"lane": lane}
        if participant is not None:
            values["participant"] = participant
        if at is not None:
            values["date"] = at.astimezone(CST).strftime("%Y-%m-%d")
        try:
            session = spec.session.format(**values)
        except KeyError as missing:
            raise ValueError(
                f"agent {agent!r}: its session {spec.session!r} needs {missing.args[0]!r}, "
                f"which this call does not give"
            ) from None
    return Call(agent=agent, session=session, round=round)


_current_round: ContextVar[Round] = ContextVar("agent_current_round")
_current_call: ContextVar[Call] = ContextVar("agent_current_call")
_wake_source: ContextVar[str | None] = ContextVar("agent_wake_source", default=None)


def current_round() -> Round:
    """The round being run. ``LookupError`` outside one: a tool that needs its round and finds
    none is a wiring error, and a guess (an empty lane, the wall clock) would write where nobody
    reads."""
    return _current_round.get()


def current_call() -> Call:
    """The call whose tools are running. ``LookupError`` outside one."""
    return _current_call.get()


@contextmanager
def in_round(round: Round) -> Iterator[Round]:
    """``round`` is the current round inside the block; the one before is restored after it."""
    token = _current_round.set(round)
    try:
        yield round
    finally:
        _current_round.reset(token)


@contextmanager
def in_call(call: Call) -> Iterator[Call]:
    """``call`` is the current call inside the block; the one before is restored after it, also
    when the block raises or is cancelled. ``Agent.run`` / ``Agent.extract`` bind their call
    with it."""
    token = _current_call.set(call)
    try:
        yield call
    finally:
        _current_call.reset(token)


@contextmanager
def woken_by(source: str) -> Iterator[None]:
    """Inside the block, what runs was woken by the wake source named ``source`` ("regular",
    "world-inbox" ...). The participant's queue binds it around the turn it runs, so the trace of
    a round can say what woke it; the round itself is not given it."""
    token = _wake_source.set(source)
    try:
        yield
    finally:
        _wake_source.reset(token)


def wake_source() -> str | None:
    """The wake source whose turn is running (:func:`woken_by`); ``None`` outside a turn."""
    return _wake_source.get()


# ---------------------------------------------------------------------------
# A participant's round: the fixed loop
# ---------------------------------------------------------------------------

# A state or an input section: its text this round, or ``None`` to leave it out.
Section = Callable[[Round], Awaitable["str | None"]]
# A step before the round, a check, or a step of the commit phase.
Step = Callable[[Round], Awaitable[None]]


@dataclass(frozen=True)
class Placed(Generic[T]):
    """What one plugin registered under ``name``, at the place a profile lists it."""

    name: str
    plugin: str
    value: T


@dataclass(frozen=True)
class AfterCommit:
    """A step after the commit, and what its failure does to the round."""

    run: Step
    on_failure: FailRound | Continue


@dataclass(frozen=True)
class RoundLog:
    """How a profile's rounds read the round before this one: when it was (``previous_at``, ``None``
    when there was none), and whether it lost the write of its history (``lost_last_round``, given
    the version the history was just read at)."""

    previous_at: Callable[[Round], Awaitable[datetime | None]]
    lost_last_round: Callable[[Round, int], Awaitable[bool]]


@dataclass
class AssembledProfile:
    """A profile as the host assembled it: each name the manifest lists replaced by what a plugin
    registered under it, in the profile's order (``profile`` keeps the names). ``agents`` are the
    main agent and its sub-agents, ``segments`` every segment they list, ``lane`` the process's.

    The host creates it when a plugin asks for the profile in its setup (``ctx.profile``) and fills
    it in once every plugin has registered, before any IO; :func:`run_round` refuses one it has not
    filled in (``assembled``)."""

    app: str
    name: str
    profile: Profile
    lane: str = ""
    agents: Mapping[str, AgentSpec] = field(default_factory=dict)
    segments: Mapping[str, Segment] = field(default_factory=dict)
    tools: tuple[Placed[ToolSpec], ...] = ()
    round_start: tuple[Placed[Step], ...] = ()
    state: tuple[Placed[Section], ...] = ()
    input: tuple[Placed[Section], ...] = ()
    checks: tuple[Placed[Step], ...] = ()
    commit: tuple[Placed[Step], ...] = ()
    after_commit: tuple[Placed[AfterCommit], ...] = ()
    round_log: RoundLog | None = None
    assembled: bool = False


@dataclass(frozen=True)
class RoundResult:
    """A round that ran to its end: the round (with what it read and decided) and the main agent's
    last message."""

    round: Round
    reply: Message


ROUND_STEP_FAILURES = get_or_create_counter(
    "round_step_failures_total",
    "Steps after a round's commit that failed and were let go (Continue): the round still counts",
    ["app", "participant", "step"],
)


@dataclass
class _NextHistory:
    """The history this round stores, worked out before its commit (:func:`store_history`)."""

    key: str = ""
    version: int = 0
    messages: list[Message] | None = None


async def run_round(
    profile: AssembledProfile,
    *,
    participant: str,
    round_id: str,
    now: datetime,
    reason: str,
    due: Due,
) -> RoundResult:
    """Run one round of ``participant`` as ``profile`` says (see the module docstring). The round's
    identity, time and reason are the wake source's. A step before the round, a check, a commit step
    or a step after the commit that fails the round raises out of here unchanged. The profile's
    switches are read first; a plugin switched off has no tool, section or step in this round."""
    if not profile.assembled:
        raise RuntimeError(
            f"profile {profile.name!r} of app {profile.app!r} is not assembled; "
            f"the host fills it in when it starts"
        )
    spec = profile.agents[profile.profile.agent]
    round = Round(
        app=profile.app,
        lane=profile.lane,
        participant=participant,
        profile=profile.name,
        round_id=round_id,
        now=now,
        reason=reason,
        due=due,
    )
    call = open_call(profile.profile.agent, spec, lane=round.lane, round=round)
    history = profile.profile.history
    off = await _switched_off(profile.profile.switches)
    with in_round(round):
        for step in _on(profile.round_start, off):
            await step.value(round)
        if profile.round_log is not None:
            round.previous_at = await profile.round_log.previous_at(round)
        if history is not None:
            key = history.key.format(lane=round.lane, participant=participant)
            await _read_history(profile, round, key)
        state = await _joined(profile.profile.join, _on(profile.state, off), round)
        stimulus = Message(
            role=Role.USER,
            content=await _joined(profile.profile.join, _on(profile.input, off), round),
        )
        if history is not None:
            read = round.history
            round.history = replace(
                read,
                messages=trim_for_round(
                    read.messages,
                    now=round.now,
                    state=state,
                    policy=history.policy,
                    texts=history.texts,
                    material_tools=frozenset(
                        t.value.tool.name
                        for t in profile.tools
                        if t.value.trim == "material"
                    ),
                    lost_last_round=read.lost_last_round,
                ),
            )
        earlier = round.history.messages if round.history is not None else []
        round.prompts = await PromptSet.resolve(
            {
                name: profile.agents[name]
                for name in (profile.profile.agent, *profile.profile.sub_agents)
            },
            profile.segments,
            round,
        )
        # Imported here: app.agent.core imports this module (``Call``, ``in_call``).
        from app.agent.core import Agent

        produced: list[Message] = []
        with collect_usage() as usage:
            reply = await Agent(
                spec,
                tools=[t.value for t in _on(profile.tools, off)],
                never_ran=history.texts.never_ran if history is not None else None,
            ).run(
                [*earlier, stimulus],
                call=call,
                prompts=round.prompts,
                # Kept only when the round keeps a history (what it produced is stored there).
                transcript_sink=produced if history is not None else None,
            )
        cost = profile.profile.cost
        if cost is not None:
            await record_round_cost(
                lane=round.lane,
                actor=cost.actor.format(participant=participant),
                round_id=cost.round_id.format(round_id=round_id, trace=spec.trace_name),
                usage=usage,
                observed_at=round.now.isoformat(),
            )
        round.reply = reply
        for check in _on(profile.checks, off):
            await check.value(round)
        if history is not None:
            # Worked out before the commit: it is pure, but anything that fails between the commit
            # and the write would leave a round that landed and a history never even tried.
            pending = round.state(_NextHistory)
            pending.key, pending.version = key, round.history.version
            pending.messages = next_transcript(
                earlier, [stimulus, *produced], policy=history.policy
            )
        commit = _on(profile.commit, off)
        if commit:
            async with tx():
                for step in commit:
                    await step.value(round)
        for step in _on(profile.after_commit, off):
            await _after_commit(step, round)
    return RoundResult(round=round, reply=reply)


async def store_history(round: Round) -> None:
    """Store the history ``round`` worked out before its commit as the next version (compare and
    set on the version it read; :class:`~app.agent.continuity.TranscriptConflict` when someone wrote
    it meanwhile). A step after the commit: the plugin that keeps the history registers it, with
    what its failure does."""
    pending = round.state(_NextHistory)
    if pending.messages is None:
        raise RuntimeError(
            f"round {round.round_id} of {round.participant} has no history to store; "
            f"its profile keeps none"
        )
    await commit_transcript(pending.key, pending.messages, expected_ver=pending.version)


async def _switched_off(switches: Switches | None) -> frozenset[str]:
    """The plugins ``switches`` leaves out of this round: the ones it lists that its Dynamic Config
    key does not name. A blank key leaves every plugin on; a name it does not list is logged and
    skipped."""
    if switches is None:
        return frozenset()
    raw = await asyncio.to_thread(dynamic_config.get, switches.key, default="")
    if not raw.strip():
        return frozenset()
    wanted = {name.strip() for name in raw.split(",") if name.strip()}
    unknown = wanted - set(switches.plugins)
    if unknown:
        logger.warning(
            "dynamic config %s names %s, which are not plugins it switches; skipped",
            switches.key,
            sorted(unknown),
        )
    return frozenset(switches.plugins) - wanted


def _on(placed: tuple[Placed[T], ...], off: frozenset[str]) -> tuple[Placed[T], ...]:
    """What of ``placed`` belongs to a plugin that is on this round."""
    return tuple(p for p in placed if p.plugin not in off)


async def _read_history(profile: AssembledProfile, round: Round, key: str) -> None:
    messages, version = await load_session(key)
    lost = (
        await profile.round_log.lost_last_round(round, version)
        if profile.round_log is not None
        else False
    )
    round.history = HistoryView(
        messages=messages,
        version=version,
        lost_last_round=lost,
        continues_unbroken=continues_unbroken(
            messages, lost_last_round=lost, texts=profile.profile.history.texts
        ),
    )


async def _joined(
    join: str, sections: tuple[Placed[Section], ...], round: Round
) -> str:
    texts = [await section.value(round) for section in sections]
    return join.join(text for text in texts if text is not None)


async def _after_commit(step: Placed[AfterCommit], round: Round) -> None:
    try:
        await step.value.run(round)
    except Exception:
        if isinstance(step.value.on_failure, FailRound):
            raise
        ROUND_STEP_FAILURES.labels(
            app=round.app, participant=round.participant, step=step.name
        ).inc()
        logger.error(
            "%s round %s of %s: step %s after the commit failed; the round still counts",
            round.app,
            round.round_id,
            round.participant,
            step.name,
            exc_info=True,
        )
