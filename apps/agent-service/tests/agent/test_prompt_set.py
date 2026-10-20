"""The prompts a round renders, fetched together and checked strictly (``PromptSet``).

A segment's template must declare exactly the variables its plugin gives: one missing or one too
many is a configuration error (``PromptConfigError``), raised when the set is resolved, before any
model is called. Only what the template declares counts; ``{{...}}`` inside a value is text. Each
segment is fetched once per resolve, the lane's label first and production when the lane has
none. An agent's text segments compile into one SYSTEM message, joined by a blank line; a chat
prompt is a segment of its own and compiles into its messages.

Langfuse is a stand-in at the SDK client (``app.agent.prompts._get_client``) except in the last
test, which runs the real SDK against a local HTTP server to see how a segment id with ``/`` (a
Langfuse folder) goes on the wire. Nothing here reaches a real Langfuse.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any

import pytest
from langfuse.api import Prompt_Chat, Prompt_Text
from langfuse.model import ChatPromptClient, TextPromptClient

from app.agent.contracts import AgentSpec, Segment
from app.agent.neutral import Role
from app.agent.prompts import PromptConfigError, PromptSet
from app.agent.round import Round
from app.infra.cst_time import CST


def _text(name: str, body: str, *, version: int = 1, label: str = "production"):
    return TextPromptClient(
        Prompt_Text(
            name=name, version=version, prompt=body, config={}, labels=[label], tags=[]
        )
    )


def _chat(name: str, messages: list[dict[str, str]], *, version: int = 1):
    return ChatPromptClient(
        Prompt_Chat(
            name=name,
            version=version,
            prompt=messages,
            config={},
            labels=["production"],
            tags=[],
        )
    )


class _Langfuse:
    """Langfuse prompts by (name, label); records every fetch. A label it does not have raises,
    as the SDK does."""

    def __init__(self) -> None:
        self.prompts: dict[tuple[str, str], Any] = {}
        self.fetched: list[tuple[str, str | None]] = []

    def get_prompt(self, name: str, *, label: str | None = None, **_: Any):
        self.fetched.append((name, label))
        try:
            return self.prompts[(name, label or "production")]
        except KeyError:
            raise LookupError(f"{name} has no version labelled {label}") from None


@pytest.fixture
def langfuse(monkeypatch) -> _Langfuse:
    client = _Langfuse()
    monkeypatch.setattr("app.agent.prompts._get_client", lambda: client)
    monkeypatch.setattr("app.agent.prompts.get_lane", lambda: None)
    monkeypatch.setattr("app.agent.prompts.settings", SimpleNamespace(lane="prod"))
    return client


def _agent(*segments: str) -> AgentSpec:
    return AgentSpec(segments=segments, model="m", trace_name="t")


def _round() -> Round:
    return Round(
        app="agent-service",
        lane="coe-living",
        participant="akao",
        profile="resident",
        round_id="2026-07-25T14:00+08:00",
        now=datetime(2026, 7, 25, 14, tzinfo=CST),
        reason="test",
        due=object(),
    )


# ---------------------------------------------------------------------------
# strict variables
# ---------------------------------------------------------------------------


async def test_a_variable_the_plugin_gives_and_the_template_does_not_declare_is_an_error(
    langfuse,
):
    langfuse.prompts[("diary", "production")] = _text(
        "diary", "你是 {{persona_name}}。", version=3
    )
    segments = {
        "diary": Segment("diary", "diary", variables=("persona_name", "persona_core"))
    }

    with pytest.raises(PromptConfigError) as raised:
        await PromptSet.resolve({"diary": _agent("diary")}, segments, None)

    error = raised.value
    assert (error.segment, error.prompt_id, error.label, error.version) == (
        "diary",
        "diary",
        "production",
        3,
    )
    assert error.missing == frozenset({"persona_core"})
    assert error.unexpected == frozenset()


async def test_a_variable_the_template_declares_and_no_plugin_gives_is_an_error(
    langfuse,
):
    langfuse.prompts[("world_round", "production")] = _text(
        "world_round", "现在是 {{currDate}} {{currTime}}。"
    )
    segments = {"world_round": Segment("world_round", "world_round")}

    with pytest.raises(PromptConfigError) as raised:
        await PromptSet.resolve({"world_round": _agent("world_round")}, segments, None)

    assert raised.value.missing == frozenset()
    assert raised.value.unexpected == frozenset({"currDate", "currTime"})


async def test_braces_inside_a_value_are_text_not_a_declaration(langfuse):
    langfuse.prompts[("guard", "production")] = _chat(
        "guard",
        [
            {"role": "system", "content": "判一下。"},
            {"role": "user", "content": "{{response}}"},
        ],
    )
    segments = {"guard": Segment("guard", "guard", variables=("response",))}

    prompts = await PromptSet.resolve({"guard": _agent("guard")}, segments, None)
    messages = prompts.messages("guard", {"response": "写着 {{persona_name}} 的一句话"})

    assert [(m.role, m.content) for m in messages] == [
        (Role.SYSTEM, "判一下。"),
        (Role.USER, "写着 {{persona_name}} 的一句话"),
    ]


async def test_values_given_at_the_call_must_be_the_ones_declared(langfuse):
    langfuse.prompts[("guard", "production")] = _text("guard", "判：{{response}}")
    segments = {"guard": Segment("guard", "guard", variables=("response",))}
    prompts = await PromptSet.resolve({"guard": _agent("guard")}, segments, None)

    with pytest.raises(ValueError):
        prompts.messages("guard")
    with pytest.raises(ValueError):
        prompts.messages("guard", {"response": "x", "extra": "y"})


async def test_a_segments_values_are_worked_out_from_the_round_when_it_is_resolved(
    langfuse,
):
    langfuse.prompts[("identity", "production")] = _text(
        "identity", "你是 {{persona_name}}。"
    )
    seen: list[Round] = []

    async def persona_vars(round: Round) -> dict[str, str]:
        seen.append(round)
        return {"persona_name": "赤尾"}

    segments = {
        "identity": Segment(
            "identity", "identity", variables=("persona_name",), values=persona_vars
        )
    }
    her_round = _round()

    prompts = await PromptSet.resolve(
        {"moment": _agent("identity")}, segments, her_round
    )

    assert seen == [her_round]
    [system] = prompts.messages("moment")
    assert system.content == "你是 赤尾。"


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------


async def test_each_segment_is_fetched_once_per_resolve(langfuse):
    langfuse.prompts[("rules", "production")] = _text("rules", "规则。")
    langfuse.prompts[("voice", "production")] = _text("voice", "语气。")
    segments = {name: Segment(name, name) for name in ("rules", "voice")}

    prompts = await PromptSet.resolve(
        {"moment": _agent("rules", "voice"), "review": _agent("rules")}, segments, None
    )

    assert sorted(langfuse.fetched) == [("rules", None), ("voice", None)]
    assert prompts.versions("review") == [("rules", "production", 1)]


async def test_a_lane_takes_its_own_label_and_falls_back_to_production(
    langfuse, monkeypatch
):
    monkeypatch.setattr("app.agent.prompts.get_lane", lambda: "coe-world")
    langfuse.prompts[("rules", "coe-world")] = _text(
        "rules", "泳道的规则。", version=12
    )
    langfuse.prompts[("voice", "production")] = _text("voice", "语气。", version=9)
    segments = {name: Segment(name, name) for name in ("rules", "voice")}

    prompts = await PromptSet.resolve(
        {"moment": _agent("rules", "voice")}, segments, None
    )

    assert prompts.versions("moment") == [
        ("rules", "coe-world", 12),
        ("voice", "production", 9),
    ]


# ---------------------------------------------------------------------------
# compiling
# ---------------------------------------------------------------------------


async def test_text_segments_compile_into_one_system_message_a_blank_line_apart(
    langfuse,
):
    langfuse.prompts[("identity", "production")] = _text(
        "identity", "你是 {{persona_name}}。"
    )
    langfuse.prompts[("rules", "production")] = _text(
        "rules", "<rules>\n规则。\n</rules>\n"
    )
    segments = {
        "identity": Segment("identity", "identity", variables=("persona_name",)),
        "rules": Segment("rules", "rules"),
    }
    prompts = await PromptSet.resolve(
        {"moment": _agent("identity", "rules")}, segments, None
    )

    [system] = prompts.messages("moment", {"persona_name": "赤尾"})

    assert system.role is Role.SYSTEM
    assert system.content == "你是 赤尾。\n\n<rules>\n规则。\n</rules>\n"
    assert prompts.single_prompt("moment") is None


async def test_a_single_text_segment_is_the_prompt_the_call_links(langfuse):
    prompt = _text("diary", "写日记。")
    langfuse.prompts[("diary", "production")] = prompt
    prompts = await PromptSet.resolve(
        {"diary": _agent("diary")}, {"diary": Segment("diary", "diary")}, None
    )

    assert prompts.single_prompt("diary") is prompt
    assert [m.content for m in prompts.messages("diary")] == ["写日记。"]


async def test_a_chat_prompt_is_a_segment_of_its_own(langfuse):
    langfuse.prompts[("guard", "production")] = _chat(
        "guard", [{"role": "system", "content": "判。"}]
    )
    langfuse.prompts[("rules", "production")] = _text("rules", "规则。")
    segments = {"guard": Segment("guard", "guard"), "rules": Segment("rules", "rules")}

    with pytest.raises(ValueError, match="chat"):
        await PromptSet.resolve({"guard": _agent("guard", "rules")}, segments, None)


async def test_an_agent_listing_a_segment_nobody_registered_is_a_code_error(langfuse):
    with pytest.raises(ValueError, match="identity"):
        await PromptSet.resolve({"moment": _agent("identity")}, {}, None)


# ---------------------------------------------------------------------------
# an id with "/" on the wire (the real SDK, a local server)
# ---------------------------------------------------------------------------


class _PromptServer(BaseHTTPRequestHandler):
    paths: list[str] = []

    def do_GET(self) -> None:  # noqa: N802 - the stdlib's name
        type(self).paths.append(self.path)
        body = json.dumps(
            {
                "type": "text",
                "name": "living_life_moment/identity",
                "version": 4,
                "prompt": "你是 {{persona_name}}。",
                "config": {},
                "labels": ["production"],
                "tags": [],
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:
        pass


@pytest.fixture
def prompt_server() -> Iterator[str]:
    _PromptServer.paths = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PromptServer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


async def test_a_segment_id_with_a_slash_is_one_path_segment_on_the_wire(
    prompt_server, monkeypatch
):
    from langfuse import Langfuse

    client = Langfuse(
        public_key="pk-lf-prompt-path-test",
        secret_key="sk-lf-prompt-path-test",
        host=prompt_server,
        tracing_enabled=False,
    )
    monkeypatch.setattr("app.agent.prompts._get_client", lambda: client)
    monkeypatch.setattr("app.agent.prompts.get_lane", lambda: None)
    monkeypatch.setattr("app.agent.prompts.settings", SimpleNamespace(lane="prod"))
    segments = {
        "identity": Segment(
            "identity", "living_life_moment/identity", variables=("persona_name",)
        )
    }
    try:
        prompts = await PromptSet.resolve(
            {"moment": _agent("identity")}, segments, None
        )
    finally:
        client.shutdown()

    [path] = _PromptServer.paths
    assert path.split("?")[0] == "/api/public/v2/prompts/living_life_moment%2Fidentity"
    assert prompts.versions("moment") == [
        ("living_life_moment/identity", "production", 4)
    ]
