"""应答：别人问 world"某处现在什么样、谁在哪"，应答 agent 依据各知识来源回答，只读，不叫醒主 agent。

模型换成替身（应答的替身在它那次调用的 context 里真调来源的工具），通信机制的 ``send`` 换成替身。
"""
from __future__ import annotations

from app.manifests.world import QUERY_TOOLS
from app.messaging.message import Kind, new_message
from app.world import answer, records, told
from app.world.records import reading

from .conftest import LANE, ScriptedAgent, ask, spec_called, tools_built_for


def _question(body: str, sender: str = "operator"):
    return new_message(sender=sender, recipient="world", body=body, kind=Kind.QUESTION)


def _volume_state(volume) -> dict[str, tuple[str, int]]:
    return {
        p.relative_to(volume).as_posix(): (p.read_text(encoding="utf-8"), p.stat().st_mtime_ns)
        for p in volume.rglob("*")
        if p.is_file() and p.name != "writer.lock"
    }


class _Setup:
    """answer 的 setup 拿到的东西的替身：``world_intake`` 服务记下登记的应答，宿主给出组好的应答 agent
    和 world 一轮的开关。"""

    def __init__(self) -> None:
        self.answers: list[tuple[str, object]] = []
        self.segments: list[str] = []
        self.on_stops: list[object] = []

    # world_intake
    def answer_with(self, plugin, answer):
        self.answers.append((plugin, answer))

        async def dispose() -> None:
            self.answers.remove((plugin, answer))

        return dispose

    # the plugin's context
    def service(self, name):
        assert name == "world_intake"
        return self

    def agent(self, name):
        from app.agent.round import AssembledAgent

        return AssembledAgent(app="world", name=name, spec=answer.ANSWER_AGENT)

    def profile(self, name):
        from app.agent.contracts import Profile, Switches
        from app.agent.round import AssembledProfile

        switches = Switches("world_sources", ("records",))
        return AssembledProfile(
            app="world", name=name, profile=Profile(agent="world_round", switches=switches)
        )

    def segment(self, segment):
        self.segments.append(segment.name)

    def on_stop(self, undo):
        self.on_stops.append(undo)


async def test_answer_registers_its_answer_with_world_intake_and_its_prompt():
    setup = _Setup()

    answer.PLUGIN.setup(setup)

    assert answer.PLUGIN.requires == ("world_intake",)
    [(plugin, answered_by)] = setup.answers
    assert plugin == "answer"
    assert answered_by.__func__ is answer.Answering.answer_question
    assert setup.segments == ["world_answer"]
    for undo in setup.on_stops:
        await undo()
    assert setup.answers == []


def test_the_world_core_does_not_require_the_answer_plugin():
    from app.plugins.world import PLUGIN as WORLD_CORE

    assert WORLD_CORE.requires == ()


def looks_up_the_kitchen():
    async def plan(_input):
        shown = await reading.read_record.invoke({"path": "地方/厨房.md"})
        return f"厨房现在是这样：{shown.split(chr(10), 2)[-1]}"

    return ScriptedAgent(plan)


async def test_a_question_is_answered_from_the_sources(world):
    records.write("地方/厨房.md", "灶上炖着汤，窗开着一条缝。", expected=None)
    world.agents[answer.ANSWER] = looks_up_the_kitchen()

    reply = await ask(world, _question("厨房现在什么样？"))

    assert reply == "厨房现在是这样：灶上炖着汤，窗开着一条缝。"
    assert "厨房现在什么样？" in world.agents[answer.ANSWER].inputs[0]
    assert "operator" in world.agents[answer.ANSWER].inputs[0]


async def test_answering_writes_nothing_tells_nobody_and_does_not_wake_the_main_agent(world, volume):
    records.write("地方/厨房.md", "灶上炖着汤。", expected=None)
    await told.take_in(
        new_message(sender="akao", recipient="world", body="我在厨房。", kind=Kind.MESSAGE)
    )
    world.agents[answer.ANSWER] = looks_up_the_kitchen()
    before = _volume_state(volume)

    await ask(world, _question("厨房现在什么样？", sender="ayana"))

    assert _volume_state(volume) == before
    assert world.sent == [] and world.scheduled == []
    assert world.runner.runs == []


async def test_the_answer_agent_has_its_own_prompt_trace_cost_and_only_the_sources_tools(world):
    world.agents[answer.ANSWER] = looks_up_the_kitchen()
    records.write("地方/厨房.md", "x", expected=None)

    await ask(world, _question("厨房现在什么样？"))

    config = spec_called(world, "world_answer")
    assert config.trace_name == "world-answer"
    assert tools_built_for(world, "world_answer") == list(QUERY_TOOLS)
    assert len([c for c in world.costs if c["round_id"].startswith("world-answer:")]) == 1
    assert all(c["actor"] == "world" and c["lane"] == LANE for c in world.costs)


async def test_saying_nothing_is_no_answer(world):
    async def silent(_input):
        return "  "

    world.agents[answer.ANSWER] = ScriptedAgent(silent)

    assert await ask(world, _question("赤尾在哪？")) is None


async def test_the_world_inbox_answers_questions_with_the_answer_registered_on_world_intake(
    app_host,
):
    from app.messaging.receiving import INBOX_REGISTRY
    from app.world.intake import Intakes

    await app_host("world")

    # 收件箱把提问交给 world_intake，它交给登记的应答；上面几条经 ``ask`` 走的就是这条路。
    assert INBOX_REGISTRY["world"].on_question.__func__ is Intakes.answer
