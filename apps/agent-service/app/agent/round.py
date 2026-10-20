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
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, TypeVar

from app.agent.contracts import AgentSpec, Due
from app.infra.cst_time import CST

if TYPE_CHECKING:
    from app.agent.continuity import HistoryView
    from app.agent.neutral import Message
    from app.agent.prompts import PromptSet

S = TypeVar("S")


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
