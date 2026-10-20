"""A participant's round, run by the fixed loop (:func:`app.agent.round.run_round`) from a profile
the host assembled: the steps before the round, the state and input sections joined in the
profile's order, the main agent with the profile's tools, the checks, the commit phase in one
transaction and the phase after it, each step of which says what its failure does.

The profile is built here the way the host builds it (``AssembledProfile`` filled in). The model is
a scripted stand-in (``app.agent.core.build_model_client`` replaced), the prompt a local Langfuse
prompt object; the transaction and the history store are in-memory stand-ins.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from langfuse.api import Prompt_Text
from langfuse.model import TextPromptClient

from app.agent.client import ModelClient
from app.agent.continuity import TrimPolicy
from app.agent.contracts import (
    AgentSpec,
    Continue,
    CostSpec,
    FailRound,
    HistorySpec,
    HistoryTexts,
    Profile,
    Segment,
    Switches,
    ToolSpec,
)
from app.agent.neutral import Message, Role, ToolCall, ToolDef
from app.agent.round import (
    AfterCommit,
    AssembledProfile,
    Placed,
    Round,
    RoundLog,
    current_round,
    run_round,
    store_history,
)
from app.agent.tooling import tool
from app.infra.cst_time import CST
from app.infra.propagation import Context

NOW = datetime(2026, 7, 25, 14, 3, tzinfo=CST)
MAIN = AgentSpec(segments=("main",), model="main-model", trace_name="main-round")


@tool
async def look_around() -> str:
    """看一眼周围。"""
    return "客厅里没人。"


@tool
async def stop_for_now() -> str:
    """这一轮到此为止。"""
    return "就到这儿。"


class _Scripted(ModelClient):
    """Answers from a script; keeps every request it was sent."""

    def __init__(self, *turns: Message) -> None:
        self._turns = list(turns)
        self.requests: list[tuple[list[Message], list[str]]] = []

    async def complete(
        self, messages: list[Message], *, tools: list[ToolDef] | None = None, **kw: Any
    ) -> Message:
        self.requests.append((list(messages), [t.name for t in tools or []]))
        await asyncio.sleep(0)
        return self._turns.pop(0)

    async def structured(self, *_a: Any, **_k: Any):  # pragma: no cover - not used
        raise NotImplementedError


def _says(text: str) -> Message:
    return Message(role=Role.ASSISTANT, content=text)


def _calls(name: str) -> Message:
    return Message(
        role=Role.ASSISTANT,
        content="",
        tool_calls=[ToolCall(id=f"{name}-1", name=name, arguments={})],
    )


def _no_tracing():
    raise RuntimeError("tracing is not part of these tests")


@pytest.fixture
def model(monkeypatch) -> _Scripted:
    scripted = _Scripted(_says("嗯。"))

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


@pytest.fixture
def transactions(monkeypatch) -> list[str]:
    """What happened in which transaction: ``begin``, each commit step's name, ``end``."""
    log: list[str] = []

    @asynccontextmanager
    async def tx():
        log.append("begin")
        yield
        log.append("end")

    monkeypatch.setattr("app.agent.round.tx", tx)
    return log


def _text(value: str | None):
    async def section(_round: Round) -> str | None:
        return value

    return section


def _step(log: list[str], name: str, *, fails: bool = False):
    async def step(_round: Round) -> None:
        log.append(name)
        if fails:
            raise RuntimeError(f"{name} broke")

    return step


def _profile(**parts: Any) -> AssembledProfile:
    profile = parts.pop("profile", Profile(agent="main"))
    return AssembledProfile(
        app="agent-service",
        name="resident",
        profile=profile,
        lane="coe-test",
        agents={"main": MAIN},
        segments={"main": Segment("main", "main")},
        assembled=True,
        **parts,
    )


def _placed(name: str, value: Any, plugin: str = "life") -> Placed:
    return Placed(name=name, plugin=plugin, value=value)


async def _run(profile: AssembledProfile, **kw: Any):
    return await run_round(
        profile,
        participant="akao",
        round_id=kw.pop("round_id", "2026-07-25T14:00+08:00"),
        now=NOW,
        reason="test",
        due=SimpleNamespace(reason="test"),
    )


async def test_the_input_sections_are_joined_in_the_profile_order_and_none_is_left_out(
    model,
):
    profile = _profile(
        input=(
            _placed("now", _text("现在 14:03。")),
            _placed("due", _text(None)),
            _placed("inbox", _text("姐姐说：回来吃饭。")),
        ),
    )

    await _run(profile)

    ((messages, _tools),) = model.requests
    assert messages[-1].role is Role.USER
    assert messages[-1].content == "现在 14:03。\n\n姐姐说：回来吃饭。"


async def test_the_profile_join_goes_between_two_sections(model):
    profile = _profile(
        profile=Profile(agent="main", join="\n"),
        input=(_placed("now", _text("a")), _placed("woken", _text("b"))),
    )

    await _run(profile)

    assert model.requests[0][0][-1].content == "a\nb"


async def test_the_model_is_given_the_tools_in_the_profile_order(model):
    profile = _profile(
        tools=(
            _placed(
                "stop_for_now", ToolSpec(stop_for_now, trim="kept", ends_round=True)
            ),
            _placed("look_around", ToolSpec(look_around, trim="material")),
        ),
    )

    await _run(profile)

    assert model.requests[0][1] == ["stop_for_now", "look_around"]


async def test_the_round_is_current_for_the_steps_and_the_sections(model):
    seen: list[str] = []

    async def section(round: Round) -> str:
        seen.append(f"section:{current_round().round_id}")
        return "x"

    async def step(round: Round) -> None:
        seen.append(
            f"{round.participant}:{current_round().round_id}:{round.reply.text()}"
        )

    profile = _profile(
        input=(_placed("now", section),),
        after_commit=(_placed("note", AfterCommit(step, FailRound())),),
    )

    result = await _run(profile, round_id="r-1")

    assert seen == ["section:r-1", "akao:r-1:嗯。"]
    assert result.reply.text() == "嗯。"
    assert result.round.lane == "coe-test" and result.round.profile == "resident"


async def test_a_step_before_the_round_that_fails_fails_it_before_the_model(model):
    log: list[str] = []
    profile = _profile(
        round_start=(_placed("outgoing.before", _step(log, "before", fails=True)),)
    )

    with pytest.raises(RuntimeError, match="before broke"):
        await _run(profile)

    assert model.requests == []


async def test_the_commit_steps_run_in_one_transaction_and_the_steps_after_it_outside(
    model, transactions
):
    profile = _profile(
        commit=(
            _placed("life.moment", _step(transactions, "life.moment")),
            _placed("phone.glances", _step(transactions, "phone.glances")),
        ),
        after_commit=(
            _placed(
                "outgoing.after", AfterCommit(_step(transactions, "after"), Continue())
            ),
        ),
    )

    await _run(profile)

    assert transactions == ["begin", "life.moment", "phone.glances", "end", "after"]


async def test_a_failing_check_fails_the_round_and_opens_no_transaction(
    model, transactions
):
    log: list[str] = []
    profile = _profile(
        checks=(_placed("wake.chosen", _step(log, "check", fails=True)),),
        commit=(_placed("life.moment", _step(transactions, "life.moment")),),
        after_commit=(
            _placed("history", AfterCommit(_step(log, "history"), FailRound())),
        ),
    )

    with pytest.raises(RuntimeError, match="check broke"):
        await _run(profile)

    assert transactions == []
    assert log == ["check"]


async def test_no_commit_step_no_transaction(model, transactions):
    await _run(_profile())

    assert transactions == []


async def test_a_continue_step_that_fails_is_logged_and_counted_and_the_round_goes_on(
    model, caplog
):
    from app.agent.round import ROUND_STEP_FAILURES

    log: list[str] = []
    counted = ROUND_STEP_FAILURES.labels(
        app="agent-service", participant="akao", step="outgoing.after"
    )
    before = counted._value.get()
    profile = _profile(
        after_commit=(
            _placed(
                "outgoing.after",
                AfterCommit(_step(log, "after", fails=True), Continue()),
            ),
            _placed("last", AfterCommit(_step(log, "last"), FailRound())),
        ),
    )

    result = await _run(profile)

    assert result.reply.text() == "嗯。"
    assert log == ["after", "last"]
    assert counted._value.get() == before + 1
    [failed] = [r for r in caplog.records if r.levelname == "ERROR"]
    assert "outgoing.after" in failed.getMessage() and failed.exc_info is not None


async def test_a_fail_round_step_that_fails_fails_the_round_and_the_steps_after_it_do_not_run(
    model,
):
    log: list[str] = []
    profile = _profile(
        after_commit=(
            _placed(
                "history", AfterCommit(_step(log, "history", fails=True), FailRound())
            ),
            _placed("wake.next", AfterCommit(_step(log, "wake.next"), FailRound())),
        ),
    )

    with pytest.raises(RuntimeError, match="history broke"):
        await _run(profile)

    assert log == ["history"]


async def test_a_profile_the_host_has_not_assembled_runs_nothing(model):
    profile = AssembledProfile(
        app="agent-service", name="resident", profile=Profile(agent="main")
    )

    with pytest.raises(RuntimeError, match="not assembled"):
        await _run(profile)

    assert model.requests == []


async def test_the_cost_row_is_named_by_the_profile(model, monkeypatch):
    rows: list[dict[str, Any]] = []

    async def record(**row: Any) -> None:
        rows.append(row)

    monkeypatch.setattr("app.agent.round.record_round_cost", record)
    profile = _profile(
        profile=Profile(
            agent="main",
            cost=CostSpec(actor="{participant}", round_id="{trace}:{round_id}"),
        )
    )

    await _run(profile, round_id="r-7")

    [row] = rows
    assert row["lane"] == "coe-test"
    assert row["actor"] == "akao"
    assert row["round_id"] == "main-round:r-7"
    assert row["observed_at"] == NOW.isoformat()
    assert {"input", "output", "total", "calls"} <= set(row["usage"])


# --- history ------------------------------------------------------------------------------------

POLICY = TrimPolicy(
    material_minutes=60,
    own_minutes=240,
    cleanup_minutes=60,
    hard_cap_tokens=200_000,
    trim_target_tokens=100_000,
)
TEXTS = HistoryTexts(
    checkpoint_head="【上下文清理 {at}】",
    checkpoint_note="",
    gap_head="【上一轮没存下来 {at}】",
    gap_note="",
    material_trimmed="",
    picture_trimmed="",
    never_ran="",
)


@pytest.fixture
def histories(monkeypatch) -> dict[str, tuple[list[Message], int]]:
    """The stored histories by key, each with its version."""
    stored: dict[str, tuple[list[Message], int]] = {}

    async def load(key: str) -> tuple[list[Message], int]:
        return stored.get(key, ([], 0))

    async def commit(key: str, messages: list[Message], *, expected_ver: int) -> None:
        assert stored.get(key, ([], 0))[1] == expected_ver
        stored[key] = (list(messages), expected_ver + 1)

    monkeypatch.setattr("app.agent.round.load_session", load)
    monkeypatch.setattr("app.agent.round.commit_transcript", commit)
    return stored


def _with_history(**parts: Any) -> AssembledProfile:
    return _profile(
        profile=Profile(
            agent="main",
            history=HistorySpec(key="{lane}:{participant}", policy=POLICY, texts=TEXTS),
        ),
        after_commit=(_placed("history", AfterCommit(store_history, FailRound())),),
        **parts,
    )


async def test_an_empty_history_starts_with_a_marker_carrying_the_state_sections(
    model, histories
):
    profile = _with_history(
        state=(
            _placed("day_page", _text("昨天：去了海边。")),
            _placed("ends", _text("没有。")),
        ),
        input=(_placed("now", _text("现在 14:03。")),),
    )

    await _run(profile)

    ((messages, _tools),) = model.requests
    marker, stimulus = messages[-2], messages[-1]
    assert marker.text().startswith("【上下文清理 ")
    assert "昨天：去了海边。\n\n没有。" in marker.text()
    assert stimulus.content == "现在 14:03。"
    stored, version = histories["coe-test:akao"]
    assert version == 1
    assert [m.content for m in stored[-2:]] == ["现在 14:03。", "嗯。"]


async def test_the_round_log_says_when_the_round_before_was_and_whether_it_lost_its_history(
    model, histories
):
    histories["coe-test:akao"] = ([], 3)
    asked: list[int] = []

    async def previous_at(round: Round) -> datetime:
        return datetime(2026, 7, 25, 13, 50, tzinfo=CST)

    async def lost_last_round(round: Round, version: int) -> bool:
        asked.append(version)
        return True

    seen: list[Any] = []

    async def section(round: Round) -> str:
        seen.append(
            (round.previous_at, round.history.version, round.history.lost_last_round)
        )
        return "x"

    profile = _with_history(
        round_log=RoundLog(previous_at=previous_at, lost_last_round=lost_last_round),
        input=(_placed("now", section),),
    )

    result = await _run(profile)

    assert asked == [3]
    assert seen == [(datetime(2026, 7, 25, 13, 50, tzinfo=CST), 3, True)]
    assert result.round.history.version == 3
    assert histories["coe-test:akao"][1] == 4


# --- switches -----------------------------------------------------------------------------------

SWITCHES = Switches(key="round_sources", plugins=("tides", "weather"))


@pytest.fixture
def config(monkeypatch) -> SimpleNamespace:
    """Dynamic Config as the round reads it: ``values`` by key (a key not given is unset), and
    ``log``, which each read appends ``config:<key>`` to (the steps of :func:`_switchable` append
    to the same log)."""
    from inner_shared.dynamic_config import dynamic_config

    seen = SimpleNamespace(values={}, log=[])

    def get(key: str, default: str = "") -> str:
        seen.log.append(f"config:{key}")
        return seen.values.get(key, default)

    monkeypatch.setattr(dynamic_config, "get", get)
    return seen


def _noted(log: list[str], name: str, value: str):
    async def section(_round: Round) -> str:
        log.append(name)
        return value

    return section


def _switchable(log: list[str], committed: list[str], **parts: Any) -> AssembledProfile:
    """Two plugins the profile can switch off (``weather``, ``tides``) and one it cannot
    (``life``), each with something in every place of the round. The commit steps note
    themselves in ``committed``, the rest in ``log``."""
    return _profile(
        profile=parts.pop(
            "profile", Profile(agent="main", join="\n", switches=SWITCHES)
        ),
        tools=(
            _placed(
                "look_around",
                ToolSpec(look_around, trim="material"),
                plugin="weather",
            ),
            _placed(
                "stop_for_now",
                ToolSpec(stop_for_now, trim="kept", ends_round=True),
                plugin="life",
            ),
        ),
        round_start=(
            _placed("weather.before", _step(log, "weather.before"), plugin="weather"),
            _placed("life.before", _step(log, "life.before")),
        ),
        state=(
            _placed(
                "weather.state",
                _noted(log, "weather.state", "多云。"),
                plugin="weather",
            ),
            _placed("life.state", _noted(log, "life.state", "在家。")),
        ),
        input=(
            _placed("weather.now", _text("晴。"), plugin="weather"),
            _placed("tides.now", _text("涨潮。"), plugin="tides"),
            _placed("life.now", _text("14:03。")),
        ),
        checks=(
            _placed("weather.check", _step(log, "weather.check"), plugin="weather"),
        ),
        commit=(
            _placed(
                "weather.commit", _step(committed, "weather.commit"), plugin="weather"
            ),
            _placed("life.commit", _step(committed, "life.commit")),
        ),
        after_commit=(
            _placed(
                "weather.after",
                AfterCommit(_step(log, "weather.after"), FailRound()),
                plugin="weather",
            ),
            _placed("life.after", AfterCommit(_step(log, "life.after"), FailRound())),
        ),
        **parts,
    )


async def test_a_switched_off_plugin_has_no_tool_section_or_step_in_the_round(
    model, transactions, config
):
    config.values["round_sources"] = "tides"

    await _run(_switchable(config.log, transactions))

    ((messages, tools),) = model.requests
    assert tools == ["stop_for_now"]
    assert messages[-1].content == "涨潮。\n14:03。"
    assert config.log == [
        "config:round_sources",
        "life.before",
        "life.state",
        "life.after",
    ]
    assert transactions == ["begin", "life.commit", "end"]


async def test_a_blank_switch_key_leaves_every_plugin_on(model, transactions, config):
    await _run(_switchable(config.log, transactions))

    ((messages, tools),) = model.requests
    assert tools == ["look_around", "stop_for_now"]
    assert messages[-1].content == "晴。\n涨潮。\n14:03。"
    assert config.log == [
        "config:round_sources",
        "weather.before",
        "life.before",
        "weather.state",
        "life.state",
        "weather.check",
        "weather.after",
        "life.after",
    ]
    assert transactions == ["begin", "weather.commit", "life.commit", "end"]


async def test_a_name_the_switches_do_not_list_is_logged_and_skipped(
    model, transactions, config, caplog
):
    config.values["round_sources"] = " weather , surf,"

    with caplog.at_level(logging.WARNING, logger="app.agent.round"):
        await _run(_switchable(config.log, transactions))

    assert model.requests[0][0][-1].content == "晴。\n14:03。"
    [warning] = [
        r
        for r in caplog.records
        if r.name == "app.agent.round" and r.levelno == logging.WARNING
    ]
    assert "round_sources" in warning.getMessage()
    assert "surf" in warning.getMessage()


async def test_only_names_the_switches_do_not_list_switch_every_plugin_off(
    model, transactions, config
):
    config.values["round_sources"] = "surf"

    await _run(_switchable(config.log, transactions))

    assert model.requests[0][0][-1].content == "14:03。"


async def test_a_profile_without_switches_reads_no_switch(model, transactions, config):
    await _run(
        _switchable(config.log, transactions, profile=Profile(agent="main", join="\n"))
    )

    assert not [entry for entry in config.log if entry.startswith("config:")]
    assert model.requests[0][0][-1].content == "晴。\n涨潮。\n14:03。"


async def test_trimming_counts_a_switched_off_plugins_tools_as_material(
    model, transactions, config, histories, monkeypatch
):
    import app.agent.round as round_module

    config.values["round_sources"] = "tides"
    seen: dict[str, Any] = {}
    real = round_module.trim_for_round

    def trim(history, **kw):
        seen.update(kw)
        return real(history, **kw)

    monkeypatch.setattr(round_module, "trim_for_round", trim)
    profile = _switchable(
        config.log,
        transactions,
        profile=Profile(
            agent="main",
            join="\n",
            switches=SWITCHES,
            history=HistorySpec(key="{lane}:{participant}", policy=POLICY, texts=TEXTS),
        ),
    )

    await _run(profile)

    assert model.requests[0][1] == ["stop_for_now"]
    assert seen["material_tools"] == frozenset({"look_around"})
