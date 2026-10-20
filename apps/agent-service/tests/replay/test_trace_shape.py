"""What each kind of round leaves in Langfuse: which spans are the roots of their own traces, what
the traces are named, which session they are grouped into, and what hangs under what.

The behaviour replays switch tracing off (``harness/replay.py``), so none of this is in a
baseline. These scenarios run the same rounds through the replay's boundaries with the real
Langfuse SDK exporting to memory (``tests/conftest.py``), and pin the shape the code gives
the trace today:

* her round is one trace named after it, in her session ``living-life:<lane>:<persona>``; each
  model call hangs under the round's root and is linked to the prompt it was rendered from; each
  tool hangs under the model call that asked for it. The output check on what she sends is a root
  span of its own hanging under ``tool.send_message``, in the same trace, and renames nothing;
* every model call world makes (its round, a perception judgement, an NPC, an answer) is a trace
  of its own, all in the one session ``world:<lane>``;
* a perception judgement that fails on every attempt leaves one ERROR trace per attempt;
* her diary, her weekly look back and a stretch of reading are each a trace of its own, in the
  sessions ``living-day-page:<lane>:<persona>``, ``living-persona-review:<lane>:<persona>`` and
  ``<lane>:<persona>:<date>``.

What the server makes of these spans is not tested here; only what the SDK exports.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import httpx
import pytest
from langfuse._client.attributes import LangfuseOtelSpanAttributes as Attr
from openai import InternalServerError

import app.world.perception as perception
from app.data.persist import insert_idempotent
from app.infra.cst_time import CST
from app.infra.rabbitmq import lane_queue
from app.living.day_page import LivingDayPage
from app.living.happening import record_happening
from app.living.records import KIND_ACT, MEDIUM_IN_PERSON
from app.messaging.broker import headers, question_route, reply_route
from app.messaging.message import Kind, new_message
from app.messaging.sending import ANSWER_BY_HEADER, REPLY_RK_HEADER
from tests.replay import seeds
from tests.replay.harness import Fail, Reply, ToolUse

pytestmark = pytest.mark.integration

LANE = "coe-replay"


@pytest.fixture
def spans(replay, _in_memory_langfuse, monkeypatch):
    """Tracing on for this replay: every span the agent layer opens is exported to memory.

    Taken after ``replay`` so it replaces the replay's tracing-off switch."""
    client, exporter = _in_memory_langfuse
    exporter.clear()
    monkeypatch.setattr("app.agent.trace._get_client", lambda: client)
    monkeypatch.setattr("app.agent.core._get_trace_client", lambda: client)
    return exporter


@dataclass(frozen=True)
class Span:
    name: str
    trace: int
    id: int
    parent: int | None
    attrs: Mapping[str, Any]

    @property
    def generation(self) -> bool:
        return self.attrs.get(Attr.OBSERVATION_TYPE) == "generation"


def _exported(exporter) -> list[Span]:
    """Every exported span, in the order they ended (the replay freezes the clock, so every span
    starts at the same instant; a span ends after the ones under it, and one model call of a loop
    ends before the next one)."""
    return [
        Span(
            name=s.name,
            trace=s.context.trace_id,
            id=s.context.span_id,
            parent=s.parent.span_id if s.parent is not None else None,
            attrs=dict(s.attributes),
        )
        for s in exporter.get_finished_spans()
    ]


def _roots(found: list[Span]) -> list[Span]:
    return [s for s in found if s.parent is None]


def _named(found: list[Span], name: str) -> list[Span]:
    return [s for s in found if s.name == name]


def _one(found: list[Span], name: str) -> Span:
    (span,) = _named(found, name)
    return span


def _calls_under(found: list[Span], parent: Span) -> list[Span]:
    """The model calls (generations) hanging right under ``parent``."""
    return [s for s in found if s.generation and s.parent == parent.id]


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, day, hour, minute, tzinfo=CST)


def _server_error() -> InternalServerError:
    request = httpx.Request("POST", "https://model-gateway.replay/v1/chat/completions")
    return InternalServerError(
        "Error code: 500 - upstream model unavailable",
        response=httpx.Response(500, request=request),
        body=None,
    )


def _agent_service_tags() -> set[str]:
    return {"app:agent-service", f"lane:{LANE}", "lane_class:coe"}


# ---------------------------------------------------------------------------
# her round
# ---------------------------------------------------------------------------


async def test_her_round_is_one_trace_with_each_tool_under_the_call_that_asked_for_it(
    replay, spans
):
    await seeds.seed_household()
    await seeds.seed_akaos_phone()
    replay.broker.declare_inbox("world")
    await replay.start("agent-service")
    await seeds.bezhai_says("在吗？", at=_at(25, 13, 55), name="dm-1")
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
    replay.model.script(
        "guard_output_safety", Reply(data={"is_unsafe": False, "confidence": 0.1})
    )
    await replay.step(
        "her round sends a message and stops",
        lambda: replay.wake("akao", "regular"),
        at=_at(25, 14),
    )

    found = _exported(spans)
    (root,) = _roots(found)
    assert root.name == "living-life-moment"
    assert root.attrs[Attr.TRACE_NAME] == "living-life-moment"
    assert root.attrs[Attr.TRACE_SESSION_ID] == f"living-life:{LANE}:akao"
    assert set(root.attrs[Attr.TRACE_TAGS]) == _agent_service_tags()
    assert {s.trace for s in found} == {root.trace}, "one trace for the whole round"

    first, second = _calls_under(found, root)
    for call in (first, second):
        assert call.attrs[Attr.OBSERVATION_PROMPT_NAME] == "living_life_moment"
    assert _one(found, "tool.send_message").parent == first.id
    assert _one(found, "tool.stop_for_now").parent == second.id

    check = _one(found, "post-safety-check")
    assert check.parent == _one(found, "tool.send_message").id
    assert Attr.TRACE_NAME not in check.attrs, "the check does not rename her trace"
    assert Attr.TRACE_SESSION_ID not in check.attrs
    (judged,) = _calls_under(found, check)
    assert judged.attrs[Attr.OBSERVATION_PROMPT_NAME] == "guard_output_safety"


# ---------------------------------------------------------------------------
# world
# ---------------------------------------------------------------------------

AKAOS_DIGEST = (
    "我做了这些（按先后）：\n"
    "- 14:00 CST 改做 买抹茶，在 抹茶店\n"
    "做完这些，我在 抹茶店，正在 等店员做抹茶。"
)
CHANGE = "14:01，抹茶店门口的风铃响了一声。"
CLERK = "小林，楼下抹茶店的店员"
SITUATION = "14:01，抹茶店里。赤尾 刚在柜台点了一杯宇治抹茶。"
CLERK_ACTS = "14:01，小林 对赤尾说：「宇治抹茶粉刚好用完了，换成焙茶拿铁可以吗？」"
WAKE_AT_FOUR = Reply(
    tools=(ToolUse("wake_me_at", {"at": "2026-07-25T16:00", "reason": "看看抹茶店"}),)
)


async def _world_is_up(replay) -> None:
    for name in ("赤尾", "绫奈", "千凪"):
        replay.broker.declare_inbox(name)
    await replay.start("world")


def _akaos_digest_arrives(replay) -> str:
    return replay.message_arrives(
        sender="赤尾",
        recipient="world",
        body=AKAOS_DIGEST,
        message_id="akao-digest-1",
        time=_at(25, 14),
    )


async def test_every_call_world_makes_is_a_trace_of_its_own_in_one_session(
    replay, spans
):
    await _world_is_up(replay)
    inbox = _akaos_digest_arrives(replay)
    replay.model.script(
        "world_round",
        Reply(tools=(ToolUse("report_change", {"change": CHANGE}),)),
        Reply(
            tools=(ToolUse("let_npc_appear", {"npc": CLERK, "situation": SITUATION}),)
        ),
        WAKE_AT_FOUR,
        Reply(text="这一轮看完了。"),
    )
    replay.model.script(
        "world_perception",
        Reply(
            tools=(
                ToolUse(
                    "someone_notices",
                    {
                        "who": "赤尾",
                        "what": "14:01，风铃响了一声。",
                        "right_away": False,
                    },
                ),
            )
        ),
        Reply(text="判断完了。"),
        Reply(text="没有人会察觉。"),
    )
    replay.model.script("world_npc", Reply(text=CLERK_ACTS))
    await replay.step(
        "the round reports a change and lets the clerk appear",
        lambda: replay.broker.deliver(inbox),
        at=_at(25, 14, 1),
    )

    found = _exported(spans)
    roots = _roots(found)
    assert sorted(r.name for r in roots) == [
        "world-npc",
        "world-perception",
        "world-perception",
        "world-round",
    ]
    assert len({r.trace for r in roots}) == len(roots), (
        "each call is a trace of its own"
    )
    for r in roots:
        assert r.attrs[Attr.TRACE_NAME] == r.name
        assert r.attrs[Attr.TRACE_SESSION_ID] == f"world:{LANE}"
        assert "app:world" in r.attrs[Attr.TRACE_TAGS]
        for call in _calls_under(found, r):
            assert call.attrs[Attr.OBSERVATION_PROMPT_NAME] == r.name.replace("-", "_")

    round_root = _one(roots, "world-round")
    calls = _calls_under(found, round_root)
    assert _one(found, "tool.report_change").parent == calls[0].id
    assert _one(found, "tool.let_npc_appear").parent == calls[1].id
    assert _one(found, "tool.wake_me_at").parent == calls[2].id
    noticed = _one(found, "tool.someone_notices")
    assert noticed.trace != round_root.trace
    assert noticed.trace in {r.trace for r in _named(roots, "world-perception")}


async def test_a_perception_that_fails_every_attempt_leaves_one_error_trace_per_attempt(
    replay, spans, monkeypatch
):
    monkeypatch.setattr(perception, "RETRY_BASE_SECONDS", 0.0)
    monkeypatch.setattr(perception, "RETRY_MAX_SECONDS", 0.0)
    await _world_is_up(replay)
    inbox = _akaos_digest_arrives(replay)
    replay.model.script(
        "world_round",
        Reply(tools=(ToolUse("report_change", {"change": CHANGE}),)),
        WAKE_AT_FOUR,
        Reply(text="这一轮看完了。"),
    )
    replay.model.script(
        "world_perception",
        *(Fail(_server_error) for _ in range(perception.PERCEPTION_ATTEMPTS)),
    )
    await replay.step(
        "every attempt of the judgement fails",
        lambda: replay.broker.deliver(inbox),
        at=_at(25, 14, 1),
    )

    found = _exported(spans)
    attempts = [r for r in _roots(found) if r.name == "world-perception"]
    assert len(attempts) == perception.PERCEPTION_ATTEMPTS > 1
    assert len({a.trace for a in attempts}) == len(attempts)
    for attempt in attempts:
        assert attempt.attrs[Attr.OBSERVATION_LEVEL] == "ERROR"
        assert attempt.attrs[Attr.OBSERVATION_STATUS_MESSAGE].startswith(
            "InternalServerError: Error code: 500"
        )
        assert attempt.attrs[Attr.TRACE_SESSION_ID] == f"world:{LANE}"
    (round_root,) = [r for r in _roots(found) if r.name == "world-round"]
    assert Attr.OBSERVATION_LEVEL not in round_root.attrs, "the round itself went on"


async def test_an_answer_is_a_trace_of_its_own_in_worlds_session(replay, spans):
    await replay.start("world")
    asks = "messaging.reply.ayana-asks"
    await replay.broker.declare_private_queue(None, reply_route(asks), replay.lane)
    question = new_message(
        sender="绫奈",
        recipient="world",
        body="家里客厅现在什么样？",
        kind=Kind.QUESTION,
        time=_at(25, 15),
        message_id="ayana-question-1",
    )
    queue = lane_queue(question_route("world").queue, replay.lane)
    replay.broker.inject(
        queue,
        question.to_json(),
        headers(
            {
                REPLY_RK_HEADER: asks,
                ANSWER_BY_HEADER: (_at(25, 15) + timedelta(seconds=60))
                .astimezone(UTC)
                .isoformat(),
            }
        ),
    )
    replay.model.script("world_answer", Reply(text="客厅朝南，下午有太阳。"))
    await replay.step(
        "绫奈's question is answered",
        lambda: replay.broker.deliver(queue),
        at=_at(25, 15),
    )

    found = _exported(spans)
    (root,) = _roots(found)
    assert root.name == root.attrs[Attr.TRACE_NAME] == "world-answer"
    assert root.attrs[Attr.TRACE_SESSION_ID] == f"world:{LANE}"


# ---------------------------------------------------------------------------
# her diary, her weekly look back, a stretch of reading
# ---------------------------------------------------------------------------


async def test_her_diary_is_a_trace_of_its_own_in_her_diary_session(replay, spans):
    await seeds.seed_household()
    await seeds.seed_akaos_phone()
    await replay.start("agent-service")
    await record_happening(
        lane=replay.lane,
        happening_id=f"moment:{seeds.fixed_id('happening:darkroom').hex}",
        actor="akao",
        kind=KIND_ACT,
        content="去暗房冲了两卷胶片",
        occurred_at=_at(25, 15),
        audience=(),
        medium=MEDIUM_IN_PERSON,
        channel_id=None,
    )
    replay.model.script("living_day_page", Reply(text="今天冲了两卷胶片。"))
    await replay.step(
        "the diary tick in the window", lambda: replay.tick("day-page"), at=_at(26, 4)
    )

    found = _exported(spans)
    (root,) = _roots(found)
    assert root.name == root.attrs[Attr.TRACE_NAME] == "living-day-page"
    assert root.attrs[Attr.TRACE_SESSION_ID] == f"living-day-page:{LANE}:akao"
    (call,) = _calls_under(found, root)
    assert call.attrs[Attr.OBSERVATION_PROMPT_NAME] == "living_day_page"


async def test_her_weekly_look_back_is_a_trace_of_its_own_in_her_review_session(
    replay, spans
):
    await seeds.seed_household()
    await replay.start("agent-service")
    for day, words in {
        date(2026, 7, 21): "周二：第一次自己冲胶卷。",
        date(2026, 7, 23): "周四：写了一篇角色分析。",
    }.items():
        await insert_idempotent(
            LivingDayPage(
                lane=replay.lane,
                persona_id="akao",
                day=day,
                text=words,
                written_at=datetime.combine(
                    day + timedelta(days=1), time(4), tzinfo=CST
                ),
                happenings=1,
            )
        )
    replay.model.script("living_persona_review", Reply(text="她这阵子开始自己冲胶卷。"))
    await replay.step(
        "the look-back tick in the window",
        lambda: replay.tick("persona-review"),
        at=_at(27, 6),
    )

    found = _exported(spans)
    (root,) = _roots(found)
    assert root.name == root.attrs[Attr.TRACE_NAME] == "living-persona-review"
    assert root.attrs[Attr.TRACE_SESSION_ID] == f"living-persona-review:{LANE}:akao"


async def test_a_stretch_of_reading_is_a_trace_of_its_own_in_her_day_session(
    replay, spans
):
    await seeds.seed_household()
    await seeds.seed_akaos_phone()
    replay.broker.declare_inbox("world")
    await seeds.bezhai_sends(
        [{"kind": "file", "key": "book-key", "meta": {"file_name": "小王子.txt"}}],
        summary="[file]",
        at=_at(25, 13, 30),
        name="book",
    )
    replay.objects.put(
        "files/book-key",
        "小王子说：「重要的东西，用眼睛是看不见的。」\n".encode(),
        "text/plain",
    )
    await replay.start("agent-service")
    replay.model.script(
        "living_life_moment",
        Reply(tools=(ToolUse("read_a_bit", {"which": "小王子"}),)),
        Reply(tools=(ToolUse("stop_for_now", {}),)),
    )
    await replay.step(
        "her round picks the book up",
        lambda: replay.wake("akao", "regular"),
        at=_at(25, 14),
    )
    spans.clear()
    replay.model.script(
        "book_reading_impression",
        Reply(tools=(ToolUse("read", {"page_num": 0}),)),
        Reply(text="最后那句话我想了很久。"),
    )
    await replay.step(
        "the stretch is read",
        lambda: replay.broker.deliver(replay.inbox("reading")),
        at=_at(25, 14, 2),
    )

    found = _exported(spans)
    (root,) = _roots(found)
    assert root.name == root.attrs[Attr.TRACE_NAME] == "book-reading"
    assert root.attrs[Attr.TRACE_SESSION_ID] == f"{LANE}:akao:2026-07-25"
    assert _one(found, "tool.read").parent == _calls_under(found, root)[0].id
