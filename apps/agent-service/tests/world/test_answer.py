"""应答：别人问 world"某处现在什么样、谁在哪"，应答 agent 依据各知识来源回答，只读，不叫醒主 agent。

模型换成替身（应答的替身在它那次调用的 context 里真调来源的工具），通信机制的 ``send`` 换成替身。
"""
from __future__ import annotations

from app.messaging.message import Kind, new_message
from app.world import answer, records
from app.world.sources import query_tools, told
from app.world.sources import records as records_source

from .conftest import LANE, ScriptedAgent, tools_built_for


def _question(body: str, sender: str = "operator"):
    return new_message(sender=sender, recipient="world", body=body, kind=Kind.QUESTION)


def _volume_state(volume) -> dict[str, tuple[str, int]]:
    return {
        p.relative_to(volume).as_posix(): (p.read_text(encoding="utf-8"), p.stat().st_mtime_ns)
        for p in volume.rglob("*")
        if p.is_file() and p.name != "writer.lock"
    }


def looks_up_the_kitchen():
    async def plan(_input):
        shown = await records_source.read_record.invoke({"path": "地方/厨房.md"})
        return f"厨房现在是这样：{shown.split(chr(10), 2)[-1]}"

    return ScriptedAgent(plan)


async def test_a_question_is_answered_from_the_sources(world):
    records.write("地方/厨房.md", "灶上炖着汤，窗开着一条缝。", expected=None)
    world.agents[answer.ANSWER.prompt_id] = looks_up_the_kitchen()

    reply = await answer.answer_question(_question("厨房现在什么样？"))

    assert reply == "厨房现在是这样：灶上炖着汤，窗开着一条缝。"
    assert "厨房现在什么样？" in world.agents[answer.ANSWER.prompt_id].inputs[0]
    assert "operator" in world.agents[answer.ANSWER.prompt_id].inputs[0]


async def test_answering_writes_nothing_tells_nobody_and_does_not_wake_the_main_agent(world, volume):
    records.write("地方/厨房.md", "灶上炖着汤。", expected=None)
    await told.take_in(
        new_message(sender="akao", recipient="world", body="我在厨房。", kind=Kind.MESSAGE)
    )
    world.agents[answer.ANSWER.prompt_id] = looks_up_the_kitchen()
    before = _volume_state(volume)

    await answer.answer_question(_question("厨房现在什么样？", sender="ayana"))

    assert _volume_state(volume) == before
    assert world.sent == [] and world.scheduled == []
    assert world.runner.runs == []


async def test_the_answer_agent_has_its_own_prompt_trace_cost_and_only_the_sources_tools(world):
    world.agents[answer.ANSWER.prompt_id] = looks_up_the_kitchen()
    records.write("地方/厨房.md", "x", expected=None)

    await answer.answer_question(_question("厨房现在什么样？"))

    [config] = [c for c, _ in world.built if c.prompt_id == "world_answer"]
    assert config.trace_name == "world-answer"
    assert tools_built_for(world, "world_answer") == [t.name for t in await query_tools()]
    assert len([c for c in world.costs if c["round_id"].startswith("world-answer:")]) == 1
    assert all(c["actor"] == "world" and c["lane"] == LANE for c in world.costs)


async def test_saying_nothing_is_no_answer(world):
    async def silent(_input):
        return "  "

    world.agents[answer.ANSWER.prompt_id] = ScriptedAgent(silent)

    assert await answer.answer_question(_question("赤尾在哪？")) is None


async def test_the_world_inbox_answers_questions_with_the_answer_agent(app_host):
    from app.messaging.receiving import INBOX_REGISTRY

    await app_host("world")

    assert INBOX_REGISTRY["world"].on_question is answer.answer_question
