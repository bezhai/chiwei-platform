"""Self-tests of the replay harness: guarantees a baseline relies on that no scenario would notice
breaking, because a broken one still passes.

* ``script_runs_out`` — a model call finds its agent's script empty. The harness raises
  ``ScriptExhausted``, but product code may swallow it (here the output check treats it as "the
  check failed" and sends anyway), so the step passes. The replay must still fail, naming the
  agent and call number, and must neither compare nor record a baseline.
* ``redis_is_per_replay`` — what one scenario puts in Redis is not there in the next. fakeredis
  picks its server by a random host name from ``uuid4``, which the replay makes deterministic.
* ``answer_missing`` — a tool reaches an outside service the scenario gave no answer for (her
  web search). The request is refused with ``UnservedRequest``, which the tool's ``@tool_error``
  does not turn into a tool result, so the round fails; and the replay fails at ``check()`` even
  for a step that expected the failure, without comparing or recording a baseline.
* ``unserved_host`` — a request to a host the replay does not serve never goes out: async and
  sync clients are both refused, and a refusal the code swallowed
  (``gather(return_exceptions=True)``) still fails the replay.
* ``scripted_by_the_call`` — a model call takes its reply from the script of the agent whose call
  is running (``current_call().agent``), not from the script named after the prompt it was
  rendered from: once an agent's prompt is several segments, no one prompt is named after it.
  The call record keeps both, the agent and the prompt.
* ``agents_interleave`` — an agent whose tool runs another agent: each model call of the chain
  takes the next reply of its own agent's script, the outer agent's again after the inner one
  returned.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime

import httpx
import pytest

from app.agent.contracts import AgentSpec, Segment, ToolSpec
from app.agent.core import Agent
from app.agent.neutral import Message, Role
from app.agent.prompts import PromptSet
from app.agent.round import Call, in_call, open_call
from app.agent.tooling import tool
from app.agent.trace import rendered_from
from app.capabilities import banned_words
from app.infra.cst_time import CST
from tests.replay import seeds
from tests.replay.conftest import DEFAULT_START, LANE
from tests.replay.harness import Replay, Reply, ToolUse
from tests.replay.harness import baseline as baseline_mod
from tests.replay.harness.model import ModelScript
from tests.replay.harness.network import UnservedRequest
from tests.replay.harness.prompts import load_fixture
from tests.replay.harness.timeline import Timeline

pytestmark = pytest.mark.integration


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, 25, hour, minute, tzinfo=CST)


async def test_script_runs_out(replay, monkeypatch):
    compared: list[str] = []
    monkeypatch.setattr(
        baseline_mod, "compare_or_record", lambda name, document: compared.append(name)
    )
    await seeds.seed_household()
    await seeds.seed_akaos_phone()
    replay.broker.declare_inbox(
        "world", answers=lambda question: "客厅里只有你一个人。"
    )
    await replay.start("agent-service")
    await seeds.bezhai_says("在吗？", at=_at(13, 55), name="dm-1")

    replay.model.script(
        "living_life_moment",
        Reply(
            tools=(
                ToolUse(
                    "send_message",
                    {"what": "在呢。", "channel_id": str(seeds.DM_WITH_BEZHAI)},
                ),
            )
        ),
        Reply(tools=(ToolUse("stop_for_now", {}),)),
    )
    # No reply for the output check: its model call finds the script empty.
    await replay.step(
        "she sends; the output check's model call has no scripted reply",
        lambda: replay.wake("akao", "regular"),
        at=_at(14, 0),
    )

    with pytest.raises(AssertionError, match=r"'guard_output_safety' call #1\b"):
        replay.check("harness/script_runs_out")
    assert compared == [], "a replay whose script ran out was compared or recorded"


async def test_answer_missing(replay, monkeypatch):
    compared: list[str] = []
    monkeypatch.setattr(
        baseline_mod, "compare_or_record", lambda name, document: compared.append(name)
    )
    await seeds.seed_household()
    await seeds.seed_akaos_phone()
    replay.broker.declare_inbox("world")
    await replay.start("agent-service")

    # No ``replay.web.results(...)`` for her question.
    searches = Reply(tools=(ToolUse("search_online", {"question": "明天会下雨吗"}),))
    replay.model.script("living_life_moment", searches, searches)
    with pytest.raises(
        UnservedRequest, match="web search no answer for '明天会下雨吗'"
    ):
        await replay.step(
            "she searches the web; the scenario gave the search no answer",
            lambda: replay.wake("akao", "regular"),
            at=_at(14, 0),
        )
    # Even a step that expected the failure does not make the replay pass.
    await replay.step(
        "the same search, expected to fail",
        lambda: replay.wake("akao", "regular"),
        raises=UnservedRequest,
    )

    with pytest.raises(
        AssertionError, match=r"requests were refused: GET https://web-search"
    ):
        replay.check("harness/answer_missing")
    assert compared == [], "a replay with a refused request was compared or recorded"


async def test_unserved_host(replay, monkeypatch):
    compared: list[str] = []
    monkeypatch.setattr(
        baseline_mod, "compare_or_record", lambda name, document: compared.append(name)
    )

    async def swallowed() -> list:
        async with httpx.AsyncClient() as client:
            return await asyncio.gather(
                client.get("https://example.com/async"), return_exceptions=True
            )

    outcome = await replay.step("a request the code swallows", swallowed)
    assert isinstance(outcome[0], UnservedRequest)
    with pytest.raises(UnservedRequest), httpx.Client() as client:
        client.get("https://example.com/sync")

    with pytest.raises(AssertionError, match=r"example\.com/async.*example\.com/sync"):
        replay.check("harness/unserved_host")
    assert compared == [], "a replay with a refused request was compared or recorded"


_DROP_TABLES = """
DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' LOOP
        EXECUTE 'DROP TABLE IF EXISTS public.' || quote_ident(r.tablename) || ' CASCADE';
    END LOOP;
END $$;
"""


@contextlib.asynccontextmanager
async def _scenario(engine, root):
    """One replay, opened and closed the way the ``replay`` fixture does for each scenario, and
    on an empty database afterwards, as the next scenario gets it (``test_db``'s teardown)."""
    from sqlalchemy import text

    with pytest.MonkeyPatch.context() as mp:
        r = Replay(
            engine=engine, monkeypatch=mp, root=root, lane=LANE, start=DEFAULT_START
        )
        await r.open()
        try:
            yield r
        finally:
            await r.close()
    async with engine.begin() as conn:
        await conn.execute(text(_DROP_TABLES))


async def test_redis_is_per_replay(real_pg_required, test_db, tmp_path):
    async with _scenario(test_db, tmp_path / "first") as first:
        await first.redis.sadd("banned_words", "darkroom")
        assert await banned_words.contains("在 Dark Room 里") == "darkroom"

    async with _scenario(test_db, tmp_path / "second"):
        assert await banned_words.contains("在 Dark Room 里") is None


async def test_scripted_by_the_call():
    script = ModelScript(Timeline())
    script.script("world_perception", Reply(text="判断完了。"))
    rendered = load_fixture("world_round")

    with in_call(Call(agent="world_perception", session=None, round=None)):
        with rendered_from((rendered,)):
            reply = await script.complete(
                "perception-model", [Message(role=Role.USER, content="x")], None, {}
            )

    assert reply.text() == "判断完了。"
    [call] = script.calls
    assert call["agent"] == "world_perception"
    assert call["prompt"]["name"] == "world_round"
    assert script.unused() == {}


async def test_agents_interleave(replay):
    outer = AgentSpec(segments=("world_round",), model="m", trace_name="world-round")
    inner = AgentSpec(segments=("world_npc",), model="m", trace_name="world-npc")

    prompts = await PromptSet.resolve(
        {"world_round": outer, "world_npc": inner},
        {name: Segment(name, name) for name in ("world_round", "world_npc")},
        None,
    )

    @tool
    async def ask() -> str:
        """问一下 NPC。"""
        call = open_call("world_npc", inner, lane=LANE)
        reply = await Agent(inner).run(
            [Message(role=Role.USER, content="说吧")], call=call, prompts=prompts
        )
        return reply.text()

    replay.model.script(
        "world_round", Reply(tools=(ToolUse("ask", {}),)), Reply(text="这一轮完了。")
    )
    replay.model.script("world_npc", Reply(text="老板：早。"))

    reply = await Agent(outer, tools=[ToolSpec(ask, trim="kept")]).run(
        [Message(role=Role.USER, content="醒了")],
        call=open_call("world_round", outer, lane=LANE),
        prompts=prompts,
    )

    assert reply.text() == "这一轮完了。"
    assert [(c["agent"], c["number"]) for c in replay.model.calls] == [
        ("world_round", 1),
        ("world_npc", 1),
        ("world_round", 2),
    ]
    assert replay.model.unused() == {}
