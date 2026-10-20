"""The scope of a call: the caller opens a ``Call`` and holds it, the agent binds it while it runs and
restores the one before when it returns; what a call's tools noted is read from the call object.

The model is a scripted stand-in (``app.agent.core.build_model_client`` replaced), the prompt is a
local Langfuse prompt object (``app.agent.prompts._get_client`` replaced). Everything between them,
the loop, tool dispatch and the retry, runs for real.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from langfuse.api import Prompt_Text
from langfuse.model import TextPromptClient
from openai import InternalServerError

from app.agent.client import ModelClient
from app.agent.contracts import AgentSpec, Segment, ToolSpec
from app.agent.core import Agent
from app.agent.neutral import Message, Role, ToolCall, ToolDef
from app.agent.prompts import PromptSet
from app.agent.round import (
    Round,
    current_call,
    current_round,
    in_call,
    in_round,
    open_call,
)
from app.agent.tooling import tool
from app.infra.cst_time import CST
from app.infra.propagation import Context

OUTER = AgentSpec(segments=("outer",), model="outer-model", trace_name="outer")
INNER = AgentSpec(segments=("inner",), model="inner-model", trace_name="inner")
DIARY = AgentSpec(
    segments=("diary",),
    model="diary-model",
    trace_name="diary",
    session="diary:{lane}:{participant}",
)
READING = AgentSpec(
    segments=("reading",),
    model="reading-model",
    trace_name="reading",
    session="{lane}:{participant}:{date}",
    max_retries=2,
)


@dataclass
class Noted:
    """A call's state in these tests: what its tools noted, in order."""

    items: list[str] = field(default_factory=list)


def _server_error() -> InternalServerError:
    request = httpx.Request("POST", "https://model.invalid/v1/chat/completions")
    return InternalServerError(
        "Error code: 500", response=httpx.Response(500, request=request), body=None
    )


def _calls(name: str, **arguments: Any) -> Message:
    return Message(
        role=Role.ASSISTANT,
        content="",
        tool_calls=[ToolCall(id=f"{name}-1", name=name, arguments=arguments)],
    )


def _says(text: str) -> Message:
    return Message(role=Role.ASSISTANT, content=text)


class _Scripted(ModelClient):
    """Answers from a script; records the session each call was made with."""

    def __init__(self, *turns: Message | BaseException) -> None:
        self._turns = list(turns)
        self.sessions: list[str | None] = []

    async def complete(
        self, messages: list[Message], *, tools: list[ToolDef] | None = None, **kw: Any
    ) -> Message:
        self.sessions.append(kw.get("session_id"))
        await asyncio.sleep(0)
        turn = self._turns.pop(0)
        if isinstance(turn, BaseException):
            raise turn
        return turn

    async def structured(self, *_a: Any, **_k: Any):  # pragma: no cover - not used
        raise NotImplementedError


@pytest.fixture
def models(monkeypatch) -> dict[str, ModelClient]:
    by_model: dict[str, ModelClient] = {}

    async def build(model_id: str) -> ModelClient:
        return by_model[model_id]

    def prompt(name: str) -> TextPromptClient:
        return TextPromptClient(
            Prompt_Text(
                name=name,
                version=1,
                prompt=f"你是 {name}。",
                config={},
                labels=[],
                tags=[],
            )
        )

    monkeypatch.setattr("app.agent.core.build_model_client", build)
    langfuse = SimpleNamespace(get_prompt=lambda name, **_: prompt(name))
    monkeypatch.setattr("app.agent.prompts._get_client", lambda: langfuse)
    monkeypatch.setattr(
        "app.agent.prompts.current_context",
        lambda: Context(trace_id=None, lane=None),
    )
    monkeypatch.setattr("app.agent.core._get_trace_client", _no_tracing)
    monkeypatch.setattr("app.agent.trace._get_client", _no_tracing)
    return by_model


async def _set(call) -> PromptSet:
    """The one prompt (no variables) the agent of ``call`` renders, resolved as a call would."""
    spec = {"outer": OUTER, "inner": INNER, "diary": DIARY, "reading": READING}[
        call.agent
    ]
    return await PromptSet.resolve(
        {call.agent: spec}, {call.agent: Segment(call.agent, call.agent)}, None
    )


def _no_tracing():
    raise RuntimeError("tracing is not part of these tests")


def _user(text: str) -> list[Message]:
    return [Message(role=Role.USER, content=text)]


# ---------------------------------------------------------------------------
# open_call
# ---------------------------------------------------------------------------


def test_a_session_is_formatted_from_what_the_caller_gives():
    at = datetime(2026, 7, 25, 23, 30, tzinfo=CST)
    call = open_call("reading", READING, lane="coe-living", participant="akao", at=at)
    assert (call.agent, call.session, call.round) == (
        "reading",
        "coe-living:akao:2026-07-25",
        None,
    )


def test_the_date_of_a_session_is_the_date_in_beijing():
    utc_evening = datetime.fromisoformat("2026-07-25T17:30:00+00:00")
    call = open_call(
        "reading", READING, lane="prod", participant="akao", at=utc_evening
    )
    assert call.session == "prod:akao:2026-07-26"


def test_a_session_variable_nobody_gives_is_a_code_error():
    with pytest.raises(ValueError, match="participant"):
        open_call("diary", DIARY, lane="prod")
    with pytest.raises(ValueError, match="date"):
        open_call("reading", READING, lane="prod", participant="akao")


def test_an_agent_without_a_session_has_none():
    assert open_call("outer", OUTER, lane="prod").session is None


def _round(**overrides: Any) -> Round:
    values: dict[str, Any] = {
        "app": "agent-service",
        "lane": "coe-living",
        "participant": "akao",
        "profile": "resident",
        "round_id": "2026-07-25T14:00+08:00",
        "now": datetime(2026, 7, 25, 14, 0, 30, tzinfo=CST),
        "reason": "常规节奏",
        "due": object(),
    }
    values.update(overrides)
    return Round(**values)


def test_a_call_in_a_round_takes_its_participant_and_time():
    her_round = _round()
    call = open_call("reading", READING, lane=her_round.lane, round=her_round)
    assert call.session == "coe-living:akao:2026-07-25"
    assert call.round is her_round


# ---------------------------------------------------------------------------
# outside a round, outside a call
# ---------------------------------------------------------------------------


def test_outside_a_round_there_is_no_current_round():
    with pytest.raises(LookupError):
        current_round()


def test_outside_a_call_there_is_no_current_call():
    with pytest.raises(LookupError):
        current_call()


def test_a_bound_round_is_current_inside_and_gone_after():
    her_round = _round()
    with in_round(her_round):
        assert current_round() is her_round
    with pytest.raises(LookupError):
        current_round()


def test_a_rounds_state_is_kept_by_type_and_made_on_first_ask():
    her_round = _round()
    her_round.state(Noted).items.append("a")
    assert her_round.state(Noted).items == ["a"]


# ---------------------------------------------------------------------------
# Agent.run binds the call
# ---------------------------------------------------------------------------


@tool
async def note(what: str) -> str:
    """记一笔。"""
    current_call().state(Noted).items.append(what)
    return "记下了。"


async def test_concurrent_calls_each_see_their_own_call_and_session(
    models, monkeypatch
):
    seen: dict[str, str | None] = {}

    @tool
    async def who() -> str:
        """我是谁。"""
        call = current_call()
        seen[call.session or ""] = call.agent
        await asyncio.sleep(0)
        assert current_call() is call
        return "嗯。"

    def diary_model() -> _Scripted:
        return _Scripted(_calls("who"), _says("写完了。"))

    akao_model, ayana_model = diary_model(), diary_model()
    by_participant = {"akao": akao_model, "ayana": ayana_model}

    async def build(model_id: str) -> ModelClient:
        # Built inside the run, so the call being run is the current one.
        return by_participant[current_call().session.rsplit(":", 1)[-1]]

    monkeypatch.setattr("app.agent.core.build_model_client", build)
    akao = open_call("diary", DIARY, lane="prod", participant="akao")
    ayana = open_call("diary", DIARY, lane="prod", participant="ayana")
    await asyncio.gather(
        Agent(DIARY, tools=[ToolSpec(who, trim="kept")]).run(
            _user("写日记"), call=akao, prompts=await _set(akao)
        ),
        Agent(DIARY, tools=[ToolSpec(who, trim="kept")]).run(
            _user("写日记"), call=ayana, prompts=await _set(ayana)
        ),
    )

    assert seen == {"diary:prod:akao": "diary", "diary:prod:ayana": "diary"}
    assert akao_model.sessions == ["diary:prod:akao"] * 2
    assert ayana_model.sessions == ["diary:prod:ayana"] * 2


async def test_after_a_sub_call_returns_its_caller_reads_its_state_and_is_current_again(
    models,
):
    models["inner-model"] = _Scripted(
        _calls("note", what="千凪听见了"), _says("判断完了。")
    )
    models["outer-model"] = _Scripted(_calls("ask"), _says("这一轮看完了。"))
    read_back: list[list[str]] = []
    current_after: list[str] = []

    @tool
    async def ask() -> str:
        """问一下。"""
        outer = current_call()
        inner = open_call("inner", INNER, lane="prod")
        await Agent(INNER, tools=[ToolSpec(note, trim="kept")]).run(
            _user("谁听见了"), call=inner, prompts=await _set(inner)
        )
        read_back.append(inner.state(Noted).items)
        current_after.append(current_call().agent)
        assert current_call() is outer
        assert outer.state(Noted).items == []
        return "问过了。"

    outer = open_call("outer", OUTER, lane="prod")
    await Agent(OUTER, tools=[ToolSpec(ask, trim="kept")]).run(
        _user("醒了"), call=outer, prompts=await _set(outer)
    )

    assert read_back == [["千凪听见了"]]
    assert current_after == ["outer"]
    with pytest.raises(LookupError):
        current_call()


async def test_each_attempt_starts_with_the_calls_state_empty(models, monkeypatch):
    models["reading-model"] = _Scripted(
        _calls("note", what="第一次读到的"),
        _server_error(),
        _calls("note", what="第二次读到的"),
        _says("读完了。"),
    )
    monkeypatch.setattr("app.agent.core._BACKOFF_BASE", 0)
    call = open_call(
        "reading",
        READING,
        lane="prod",
        participant="akao",
        at=datetime(2026, 7, 25, 14, tzinfo=CST),
    )
    await Agent(READING, tools=[ToolSpec(note, trim="kept")]).run(
        _user("读"), call=call, prompts=await _set(call)
    )

    assert call.state(Noted).items == ["第二次读到的"]


async def test_a_call_that_raises_restores_the_call_before_it(models):
    models["inner-model"] = _Scripted(_server_error())
    outer = open_call("outer", OUTER, lane="prod")
    with in_call(outer):
        with pytest.raises(InternalServerError):
            inner = open_call("inner", INNER, lane="prod")
            await Agent(INNER).run(_user("x"), call=inner, prompts=await _set(inner))
        assert current_call() is outer
