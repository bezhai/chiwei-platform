"""world 的 agent 怎么调模型：模型和工具预算从哪来、每一次调用是单独的 trace、成本怎么记。

模型换成替身（:func:`app.world.agents.run_agent` 用的 ``Agent`` 换掉），成本记账换成替身。
"""
from __future__ import annotations

import pytest

from app.agent.neutral import Message as Turn
from app.agent.neutral import Role
from app.agent.round import open_call
from app.world import agents

from .conftest import LANE

KIND = agents.world_agent("world_tides", "world-tides", "world_tides_model")


@pytest.fixture
def config(monkeypatch, bare_volume):
    from inner_shared.dynamic_config import dynamic_config

    values: dict[str, str] = {}
    monkeypatch.setattr(dynamic_config, "get", lambda k, default="": values.get(k, default))
    return values


# ---------------------------------------------------------------------------
# 模型和工具预算走 Dynamic Config
# ---------------------------------------------------------------------------


async def test_without_dynamic_config_every_kind_uses_the_code_defaults(config):
    assert (KIND.segments, KIND.trace_name) == (("world_tides",), "world-tides")
    assert await KIND.model.read() == agents.DEFAULT_WORLD_MODEL
    assert await KIND.recursion_limit.read() == agents.DEFAULT_WORLD_RECURSION_LIMIT


async def test_a_kind_without_its_own_model_follows_the_main_agents(config):
    config[agents.WORLD_MODEL_KEY] = "main-model"
    config[agents.WORLD_RECURSION_LIMIT_KEY] = "20"

    assert await KIND.model.read() == "main-model"
    assert await KIND.recursion_limit.read() == 20


async def test_a_kind_with_its_own_model_uses_it(config):
    config[agents.WORLD_MODEL_KEY] = "main-model"
    config["world_tides_model"] = " tide-model "

    assert await KIND.model.read() == "tide-model"


async def test_a_tool_budget_that_is_not_positive_falls_back_to_the_default(config):
    config[agents.WORLD_RECURSION_LIMIT_KEY] = "0"

    assert await KIND.recursion_limit.read() == agents.DEFAULT_WORLD_RECURSION_LIMIT


def test_every_world_call_is_a_trace_of_its_own_in_worlds_session(bare_volume):
    """trace 名就是这一类的；感知判断和 NPC 在主 agent 的工具里调，不另起一条就会挂进主 agent
    那条 trace、还把它的名字改掉。四类在同一个 session 里按时间顺着看。"""
    assert KIND.separate_trace is True
    assert KIND.update_trace is True
    assert open_call("world_tides", KIND, lane=agents.lane()).session == f"world:{LANE}"


def test_a_temporary_model_failure_is_not_retried_by_the_call(bare_volume):
    assert KIND.max_retries == 1


# ---------------------------------------------------------------------------
# 一次调用
# ---------------------------------------------------------------------------


class Recorder:
    """替身 runner：记下它被怎样调。"""

    def __init__(self, reply: str = "好。"):
        self.reply = reply
        self.calls: list[dict] = []

    async def run(self, messages, *, call, prompts, transcript_sink, **_):
        self.calls.append({"messages": list(messages), "call": call, "prompts": prompts})
        reply = Turn(role=Role.ASSISTANT, content=self.reply)
        if transcript_sink is not None:
            transcript_sink.append(reply)
        return reply


@pytest.fixture
def calling(config, monkeypatch):
    runner = Recorder()
    built: list[tuple] = []
    costs: list[dict] = []

    def build_agent(spec, *, tools=()):
        built.append((spec, list(tools)))
        return runner

    async def record_round_cost(**kw):
        costs.append(kw)

    monkeypatch.setattr(agents, "Agent", build_agent)
    monkeypatch.setattr(agents, "record_round_cost", record_round_cost)
    return runner, built, costs


async def test_a_call_runs_its_kinds_spec_with_the_tools_it_was_given(calling):
    runner, built, _ = calling
    question = Turn(role=Role.USER, content="潮水到哪了？")
    call = open_call("world_tides", KIND, lane=agents.lane())

    prompts = object()
    reply = await agents.run_agent(
        call, KIND, [question], prompts=prompts, tools=["t1", "t2"], call_id="c1"
    )

    assert reply.content == "好。"
    [(spec, tools)] = built
    assert spec is KIND
    assert tools == ["t1", "t2"]
    assert runner.calls[0]["messages"] == [question]
    assert runner.calls[0]["call"] is call
    assert runner.calls[0]["prompts"] is prompts


async def test_each_call_records_its_cost_under_world_by_kind(calling):
    _, _, costs = calling

    await agents.run_agent(
        open_call("world_tides", KIND, lane=agents.lane()),
        KIND,
        [Turn(role=Role.USER, content="x")],
        prompts=object(),
        tools=[],
        call_id="c1",
    )

    [cost] = costs
    assert (cost["lane"], cost["actor"], cost["round_id"]) == (LANE, "world", "world-tides:c1")
    assert set(cost["usage"]) >= {"input", "output", "calls"}


def test_the_main_agents_history_is_kept_under_the_session_every_world_trace_groups_under(
    bare_volume,
):
    from app.agent.round import open_call
    from app.world.main_agent import ROUND, ROUND_AGENT

    session = open_call(ROUND, ROUND_AGENT, lane=LANE).session
    assert session == f"world:{LANE}"
    assert agents.HISTORY_KEY.format(lane=LANE) == session
