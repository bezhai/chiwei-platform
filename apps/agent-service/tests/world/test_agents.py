"""world 的 agent 怎么调模型：模型和工具预算从哪来、每一次调用是单独的 trace、成本怎么记。

模型换成替身（:func:`app.world.agents.build_runner` 换掉），成本记账换成替身。
"""
from __future__ import annotations

import opentelemetry.trace as otel_trace
import pytest
from opentelemetry.sdk.trace import TracerProvider

from app.agent.context import AgentContext
from app.agent.neutral import Message as Turn
from app.agent.neutral import Role
from app.world import agents
from app.world.agents import AgentKind

from .conftest import LANE

KIND = AgentKind(prompt_id="world_tides", trace_name="world-tides", model_key="world_tides_model")


@pytest.fixture
def config(monkeypatch, bare_volume):
    from inner_shared.dynamic_config import dynamic_config

    values: dict[str, str] = {}
    ints: dict[str, int] = {}
    monkeypatch.setattr(dynamic_config, "get", lambda k, default="": values.get(k, default))
    monkeypatch.setattr(dynamic_config, "get_int", lambda k, default=0: ints.get(k, default))
    return values, ints


# ---------------------------------------------------------------------------
# 模型和工具预算走 Dynamic Config
# ---------------------------------------------------------------------------


async def test_without_dynamic_config_every_kind_uses_the_code_defaults(config):
    built = await agents.agent_config(KIND)

    assert (built.prompt_id, built.trace_name) == ("world_tides", "world-tides")
    assert built.model_id == agents.DEFAULT_WORLD_MODEL
    assert built.recursion_limit == agents.DEFAULT_WORLD_RECURSION_LIMIT


async def test_a_kind_without_its_own_model_follows_the_main_agents(config):
    values, ints = config
    values[agents.WORLD_MODEL_KEY] = "main-model"
    ints[agents.WORLD_RECURSION_LIMIT_KEY] = 20

    built = await agents.agent_config(KIND)

    assert (built.model_id, built.recursion_limit) == ("main-model", 20)


async def test_a_kind_with_its_own_model_uses_it(config):
    values, _ = config
    values[agents.WORLD_MODEL_KEY] = "main-model"
    values["world_tides_model"] = " tide-model "

    assert (await agents.agent_config(KIND)).model_id == "tide-model"


async def test_a_tool_budget_that_is_not_positive_falls_back_to_the_default(config):
    _, ints = config
    ints[agents.WORLD_RECURSION_LIMIT_KEY] = 0

    built = await agents.agent_config(KIND)

    assert built.recursion_limit == agents.DEFAULT_WORLD_RECURSION_LIMIT


# ---------------------------------------------------------------------------
# 一次调用
# ---------------------------------------------------------------------------


class Recorder:
    """替身 runner：记下它被怎样调、调的那一刻在不在别的 trace 里。"""

    def __init__(self, reply: str = "好。"):
        self.reply = reply
        self.calls: list[dict] = []

    async def run(self, messages, *, context, max_retries, transcript_sink, **_):
        span = otel_trace.get_current_span()
        self.calls.append(
            {
                "messages": list(messages),
                "context": context,
                "inside_another_trace": span.get_span_context().is_valid,
            }
        )
        reply = Turn(role=Role.ASSISTANT, content=self.reply)
        if transcript_sink is not None:
            transcript_sink.append(reply)
        return reply


@pytest.fixture
def calling(config, monkeypatch):
    runner = Recorder()
    built: list[tuple] = []
    costs: list[dict] = []

    def build_runner(cfg, tools):
        built.append((cfg, list(tools)))
        return runner

    async def record_round_cost(**kw):
        costs.append(kw)

    monkeypatch.setattr(agents, "build_runner", build_runner)
    monkeypatch.setattr(agents, "record_round_cost", record_round_cost)
    return runner, built, costs


async def test_a_call_runs_its_kinds_prompt_with_the_tools_it_was_given(calling):
    runner, built, _ = calling
    question = Turn(role=Role.USER, content="潮水到哪了？")
    context = AgentContext(session_id=agents.session_key())

    reply = await agents.run_agent(
        KIND, [question], tools=["t1", "t2"], context=context, call_id="c1"
    )

    assert reply.content == "好。"
    [(cfg, tools)] = built
    assert cfg.prompt_id == "world_tides" and cfg.trace_name == "world-tides"
    assert tools == ["t1", "t2"]
    assert runner.calls[0]["messages"] == [question]
    assert runner.calls[0]["context"] is context


async def test_a_call_made_inside_another_agents_trace_starts_a_trace_of_its_own(calling):
    runner, _, _ = calling
    tracer = TracerProvider().get_tracer("test")

    with tracer.start_as_current_span("world-round tool.report_change"):
        await agents.run_agent(
            KIND, [Turn(role=Role.USER, content="x")], tools=[], context=AgentContext(), call_id="c1"
        )

    assert runner.calls[0]["inside_another_trace"] is False


async def test_each_call_records_its_cost_under_world_by_kind(calling):
    _, _, costs = calling

    await agents.run_agent(
        KIND, [Turn(role=Role.USER, content="x")], tools=[], context=AgentContext(), call_id="c1"
    )

    [cost] = costs
    assert (cost["lane"], cost["actor"], cost["round_id"]) == (LANE, "world", "world-tides:c1")
    assert set(cost["usage"]) >= {"input", "output", "calls"}


def test_every_world_trace_groups_under_one_session(bare_volume):
    assert agents.session_key() == f"world:{LANE}"
