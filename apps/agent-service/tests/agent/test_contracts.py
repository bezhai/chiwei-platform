"""The contract types a round is built from refuse, when they are built, what would only fail at the
first round: a format string using a variable nobody fills, a Dynamic Config fallback with no
usable default, a tool with no trim class. ``FromConfig`` reads its keys in order and falls back.

That the agent layer never imports the host is checked on the real tree by
``tests/unit/scripts/test_check_package_cycles.py``.
"""

from __future__ import annotations

import dataclasses

import pytest
from inner_shared.dynamic_config import dynamic_config

from app.agent.continuity import TrimPolicy
from app.agent.contracts import (
    AgentSpec,
    CostSpec,
    FromConfig,
    HistorySpec,
    HistoryTexts,
    Segment,
    Switches,
    ToolSpec,
)
from app.agent.tooling import tool


@tool
async def look() -> str:
    """Look."""
    return "seen"


POLICY = TrimPolicy(
    material_minutes=60,
    own_minutes=240,
    cleanup_minutes=60,
    hard_cap_tokens=200_000,
    trim_target_tokens=100_000,
)
TEXTS = HistoryTexts(
    checkpoint_head="【清理 {at}】",
    checkpoint_note="往前的不在了。",
    gap_head="【缺口 {at}】",
    gap_note="上一轮没存下来。",
    material_trimmed="（不在眼前了）",
    picture_trimmed="（图不在眼前了）",
    never_ran="（没有执行）",
)


# ---------------------------------------------------------------------------
# format strings
# ---------------------------------------------------------------------------


def test_a_session_can_use_the_lane_the_participant_and_the_date():
    spec = AgentSpec(
        segments=("reading",),
        model="m",
        trace_name="book-reading",
        session="{lane}:{participant}:{date}",
    )
    assert spec.session == "{lane}:{participant}:{date}"


@pytest.mark.parametrize(
    "session", ["living-life:{lane}:{persona_id}", "{round_id}", "{}"]
)
def test_a_session_using_anything_else_is_refused(session):
    with pytest.raises(ValueError):
        AgentSpec(segments=("s",), model="m", trace_name="t", session=session)


def test_a_history_key_can_use_the_lane_and_the_participant_only():
    assert (
        HistorySpec("{lane}:{participant}", POLICY, TEXTS).key == "{lane}:{participant}"
    )
    with pytest.raises(ValueError, match="date"):
        HistorySpec("{lane}:{participant}:{date}", POLICY, TEXTS)


def test_a_cost_row_names_its_actor_by_participant_and_its_round_by_id_and_trace():
    cost = CostSpec(actor="{participant}", round_id="{trace}:{round_id}")
    assert (cost.actor, cost.round_id) == ("{participant}", "{trace}:{round_id}")
    assert CostSpec(actor="world", round_id="world-round:{round_id}").actor == "world"
    with pytest.raises(ValueError, match="round_id"):
        CostSpec(actor="{round_id}", round_id="{round_id}")
    with pytest.raises(ValueError, match="lane"):
        CostSpec(actor="{participant}", round_id="{lane}:{round_id}")


# ---------------------------------------------------------------------------
# agents, segments, tools, switches
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "head", ["【清理 ", "【清理 {at}", "{at}】", "【清理 {at} {who}】", "【{at}】{at}】"]
)
def test_a_marker_head_puts_its_time_once_between_text_before_and_after(head):
    """A marker is found again by its head: the text before the time, the time, and the text
    that ends it (a folded marker keeps exactly that much)."""
    with pytest.raises(ValueError):
        dataclasses.replace(TEXTS, checkpoint_head=head)
    with pytest.raises(ValueError):
        dataclasses.replace(TEXTS, gap_head=head)


def test_the_two_marker_heads_differ():
    with pytest.raises(ValueError):
        dataclasses.replace(TEXTS, gap_head=TEXTS.checkpoint_head)


def test_an_agent_answers_arguments_that_do_not_fit_with_the_tool_and_the_error():
    spec = AgentSpec(
        segments=("world_round",),
        model="m",
        trace_name="t",
        bad_arguments="工具 {tool} 参数不对：{error}",
    )
    assert spec.bad_arguments == "工具 {tool} 参数不对：{error}"
    assert AgentSpec(segments=("diary",), model="m", trace_name="t").bad_arguments is None
    with pytest.raises(ValueError):
        AgentSpec(segments=("x",), model="m", trace_name="t", bad_arguments="{who}")


def test_an_agent_has_at_least_one_segment_and_each_once():
    with pytest.raises(ValueError):
        AgentSpec(segments=(), model="m", trace_name="t")
    with pytest.raises(ValueError):
        AgentSpec(segments=("a", "a"), model="m", trace_name="t")


def test_an_agent_makes_at_least_one_model_call_and_one_try():
    with pytest.raises(ValueError):
        AgentSpec(segments=("s",), model="m", trace_name="t", recursion_limit=0)
    with pytest.raises(ValueError):
        AgentSpec(segments=("s",), model="m", trace_name="t", max_retries=0)


def test_an_agents_defaults_are_todays():
    spec = AgentSpec(segments=("s",), model="m", trace_name="t")
    assert spec.recursion_limit == 6
    assert spec.max_retries == 1
    assert spec.session is None
    assert spec.update_trace is True
    assert spec.separate_trace is False


def test_a_segment_lists_each_variable_once():
    assert (
        Segment("identity", "p", variables=("persona_name", "persona_core")).values
        is None
    )
    with pytest.raises(ValueError):
        Segment("identity", "p", variables=("persona_name", "persona_name"))
    with pytest.raises(ValueError):
        Segment("", "p")


def test_a_tool_is_material_or_kept():
    assert ToolSpec(look, trim="material").ends_round is False
    assert ToolSpec(look, trim="kept", ends_round=True).ends_round is True
    with pytest.raises(ValueError, match="trim"):
        ToolSpec(look, trim="sometimes")  # type: ignore[arg-type]


def test_switches_name_a_dynamic_config_key():
    with pytest.raises(ValueError):
        Switches(key=" ", plugins=("records",))


# ---------------------------------------------------------------------------
# FromConfig
# ---------------------------------------------------------------------------


def test_a_value_from_config_has_a_usable_default():
    with pytest.raises(ValueError):
        FromConfig(keys=(), default="m")
    with pytest.raises(ValueError):
        FromConfig(keys=("k",), default=0)
    with pytest.raises(ValueError):
        FromConfig(keys=("k",), default=" ")
    with pytest.raises(ValueError):
        FromConfig(keys=("k",), default=True)  # type: ignore[arg-type]


@pytest.fixture
def config(monkeypatch) -> dict[str, str]:
    values: dict[str, str] = {}
    monkeypatch.setattr(
        dynamic_config, "get", lambda key, default="": values.get(key, default)
    )
    return values


async def test_the_first_key_set_wins(config):
    model = FromConfig(keys=("world_npc_model", "world_model"), default="offline-model")
    assert await model.read() == "offline-model"
    config["world_model"] = " main-model "
    assert await model.read() == "main-model"
    config["world_npc_model"] = "npc-model"
    assert await model.read() == "npc-model"


async def test_a_blank_key_falls_through_to_the_next(config):
    config["world_npc_model"] = "   "
    config["world_model"] = "main-model"
    model = FromConfig(keys=("world_npc_model", "world_model"), default="offline-model")
    assert await model.read() == "main-model"


async def test_an_integer_key_set_to_something_unusable_falls_back(config):
    limit = FromConfig(keys=("world_recursion_limit",), default=12)
    config["world_recursion_limit"] = "0"
    assert await limit.read() == 12
    config["world_recursion_limit"] = "lots"
    assert await limit.read() == 12
    config["world_recursion_limit"] = "8"
    assert await limit.read() == 8
