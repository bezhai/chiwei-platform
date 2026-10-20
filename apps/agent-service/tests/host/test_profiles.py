"""A participant's round is assembled from the manifest: plugins register tools, segments, sections
and steps under names, the manifest's profile places those names, and the host checks the two
against each other before any IO and fills in the profile the round runs from.

Adding a tool, an input section or a step after the commit is a change to a plugin and to the
manifest only: the round shows it where the manifest lists it. A name nobody registered, a
registration nobody places, and one name registered twice are refused when the host starts, each
kind in one message that lists them all.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from langfuse.api import Prompt_Text
from langfuse.model import TextPromptClient

from app.agent.client import ModelClient
from app.agent.contracts import AgentSpec, Continue, Profile, Segment
from app.agent.neutral import Message, Role, ToolDef
from app.agent.round import AssembledProfile, Round, run_round
from app.agent.tooling import tool
from app.host import (
    App,
    DuplicateContribution,
    Host,
    HostError,
    Participant,
    UnknownContribution,
    UnplacedContribution,
)
from app.infra.cst_time import CST
from app.infra.propagation import Context
from tests.hosting import start_without_io

from .conftest import plugin

MAIN = AgentSpec(segments=("main",), model="main-model", trace_name="main-round")


@tool
async def look_around() -> str:
    """看一眼周围。"""
    return "客厅里没人。"


@tool
async def check_weather() -> str:
    """看看天气。"""
    return "晴。"


@tool
async def stop_for_now() -> str:
    """这一轮到此为止。"""
    return "就到这儿。"


class _Scripted(ModelClient):
    def __init__(self) -> None:
        self.requests: list[tuple[list[Message], list[str]]] = []

    async def complete(
        self, messages: list[Message], *, tools: list[ToolDef] | None = None, **kw: Any
    ) -> Message:
        self.requests.append((list(messages), [t.name for t in tools or []]))
        await asyncio.sleep(0)
        return Message(role=Role.ASSISTANT, content="嗯。")

    async def structured(self, *_a: Any, **_k: Any):  # pragma: no cover - not used
        raise NotImplementedError


def _no_tracing():
    raise RuntimeError("tracing is not part of these tests")


@pytest.fixture
def model(monkeypatch) -> _Scripted:
    scripted = _Scripted()

    async def build(_model_id: str) -> ModelClient:
        return scripted

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
    return scripted


def _says(text: str):
    async def section(_round: Round) -> str:
        return text

    return section


def _manifest(profile: Profile, **agents: AgentSpec) -> App:
    return App(
        plugins=(),
        agents={"main": MAIN, **agents},
        profiles={"resident": profile},
        participants=(Participant("akao", "resident"),),
    )


def _life(profiles: dict[str, AssembledProfile]):
    """Her life: the main agent's segment, two tools, the time now."""

    def setup(ctx) -> None:
        profiles["resident"] = ctx.profile("resident")
        ctx.segment(Segment("main", "main"))
        ctx.tool(stop_for_now, trim="kept", ends_round=True)
        ctx.tool(look_around, trim="material")
        ctx.section("now", _says("现在 14:03。"))

    return plugin("life", setup)


async def _one_round(host: Host, profiles: dict[str, AssembledProfile]) -> None:
    await start_without_io(host)
    try:
        await run_round(
            profiles["resident"],
            participant="akao",
            round_id="r-1",
            now=datetime(2026, 7, 25, 14, 3, tzinfo=CST),
            reason="test",
            due=SimpleNamespace(reason="test"),
        )
    finally:
        await host.stop()


async def test_a_tool_an_input_section_and_a_step_are_added_by_a_plugin_and_the_manifest(
    model,
):
    profiles: dict[str, AssembledProfile] = {}
    before = Profile(
        agent="main", tools=("look_around", "stop_for_now"), input=("now",)
    )
    await _one_round(
        Host("agent-service", [_life(profiles)], manifest=_manifest(before)), profiles
    )

    ran: list[str] = []

    def weather(ctx) -> None:
        ctx.tool(check_weather, trim="material")
        ctx.section("forecast", _says("天气：晴。"))

        async def note(round: Round) -> None:
            ran.append(round.reply.text())

        ctx.after_commit("weather.note", note, on_failure=Continue())

    after = Profile(
        agent="main",
        tools=("look_around", "check_weather", "stop_for_now"),
        input=("forecast", "now"),
        after_commit=("weather.note",),
    )
    await _one_round(
        Host(
            "agent-service",
            [_life(profiles), plugin("weather", weather)],
            manifest=_manifest(after),
        ),
        profiles,
    )

    (first, first_tools), (second, second_tools) = model.requests
    assert first_tools == ["look_around", "stop_for_now"]
    assert first[-1].content == "现在 14:03。"
    assert second_tools == ["look_around", "check_weather", "stop_for_now"]
    assert second[-1].content == "天气：晴。\n\n现在 14:03。"
    assert ran == ["嗯。"]


async def test_the_profile_a_plugin_asks_for_is_filled_in_when_the_host_starts():
    profiles: dict[str, AssembledProfile] = {}
    profile = Profile(
        agent="main", tools=("look_around", "stop_for_now"), input=("now",)
    )
    host = Host("agent-service", [_life(profiles)], manifest=_manifest(profile))

    await start_without_io(host)
    try:
        assembled = profiles["resident"]
        assert assembled.assembled
        assert assembled.profile is profile
        assert [(t.name, t.plugin) for t in assembled.tools] == [
            ("look_around", "life"),
            ("stop_for_now", "life"),
        ]
        assert assembled.agents == {"main": MAIN}
        assert assembled.segments == {"main": Segment("main", "main")}
        assert assembled.lane == "prod"
    finally:
        await host.stop()


async def test_a_profile_the_manifest_does_not_list_is_refused_in_setup():
    host = Host(
        "agent-service",
        [plugin("life", lambda ctx: ctx.profile("tourist"))],
        manifest=_manifest(Profile(agent="main")),
    )

    with pytest.raises(HostError, match="'tourist'"):
        await start_without_io(host)


def _registers(**names: list[str]):
    """A plugin registering sections, and the main agent's segment when ``segment`` is set."""

    def setup(ctx) -> None:
        for name in names.get("sections", []):
            ctx.section(name, _says(name))
        for name in names.get("segments", []):
            ctx.segment(Segment(name, name))

    return setup


async def test_names_nobody_registered_are_refused_in_one_message(recorded_phases):
    profile = Profile(
        agent="main",
        sub_agents=("guard",),
        tools=("check_weather",),
        input=("now", "forecast"),
        checks=("wake.chosen",),
    )
    host = Host(
        "agent-service",
        [plugin("life", _registers(sections=["now"], segments=["main"]))],
        manifest=_manifest(profile),
    )

    with pytest.raises(UnknownContribution) as refused:
        await host.start(http=None, schema=True, mq=True, clocks=True, tasks=True)

    message = str(refused.value)
    for named in ("'guard'", "'check_weather'", "'forecast'", "'wake.chosen'"):
        assert named in message
    assert "'now'" not in message
    assert {"ensure_business_schema", "mq.connect", "migrate_schema"}.isdisjoint(
        recorded_phases.calls
    )


async def test_a_segment_an_agent_lists_and_nobody_registered_is_refused():
    host = Host(
        "agent-service",
        [plugin("life", _registers())],
        manifest=_manifest(Profile(agent="main")),
    )

    with pytest.raises(UnknownContribution, match="segment 'main'"):
        await start_without_io(host)


async def test_a_participant_of_a_profile_the_manifest_does_not_list_is_refused():
    manifest = App(
        plugins=(),
        agents={},
        profiles={},
        participants=(Participant("akao", "resident"),),
    )

    with pytest.raises(UnknownContribution, match="'resident'"):
        await start_without_io(Host("agent-service", [], manifest=manifest))


async def test_registrations_no_profile_or_agent_places_are_refused_in_one_message():
    def setup(ctx) -> None:
        ctx.segment(Segment("main", "main"))
        ctx.section("now", _says("now"))
        ctx.section("forecast", _says("forecast"))
        ctx.tool(check_weather, trim="material")

    host = Host(
        "agent-service",
        [plugin("life", setup)],
        manifest=_manifest(Profile(agent="main", input=("now",))),
    )

    with pytest.raises(UnplacedContribution) as refused:
        await start_without_io(host)

    message = str(refused.value)
    assert "section 'forecast'" in message and "tool 'check_weather'" in message
    assert "'now'" not in message


async def test_one_name_registered_twice_is_refused_with_every_plugin_that_did():
    host = Host(
        "agent-service",
        [
            plugin("life", _registers(sections=["now"], segments=["main"])),
            plugin("phone", _registers(sections=["now"])),
        ],
        manifest=_manifest(Profile(agent="main", input=("now",))),
    )

    with pytest.raises(DuplicateContribution) as refused:
        await start_without_io(host)

    message = str(refused.value)
    assert "section 'now'" in message and "'life'" in message and "'phone'" in message


async def test_one_name_listed_twice_in_a_profile_is_refused():
    host = Host(
        "agent-service",
        [plugin("life", _registers(sections=["now"], segments=["main"]))],
        manifest=_manifest(Profile(agent="main", input=("now", "now"))),
    )

    with pytest.raises(DuplicateContribution, match="'now'"):
        await start_without_io(host)
