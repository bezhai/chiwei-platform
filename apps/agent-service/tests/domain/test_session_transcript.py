"""Agent session transcript 持久化契约 — PG durable Data（替代 Redis）.

session transcript 是 ``Agent.run(..., session_id=...)`` 续接用的可回放对话流：
存的是整条 Message 序列（含 tool call / result / 各 provider 私有 blob 如 gemini
``thought_signature``），按 ``session_id`` 取最新一版。

从 Redis cache 换成 PG durable Data 的动机：开发机不能直连 Redis 清不掉、pod 重启
意识流就丢、Redis 黑盒不可查。换成 PG：ops-db 可清、durable 不丢、可 SQL 直查她
这一天怎么想过来的。

契约（不变于存储后端）：
  - ``load_session`` 读最新一版 transcript，losslessly 反序列化；无记录（首次唤醒 /
    清库后冷启）→ ``[]``，绝不抛错。corrupt transcript_json → log.warning + ``[]``。
  - ``append_session`` 读改写：load 现有 + 追加本轮 → ``_cap_transcript`` 两轴截断
    → ``json.dumps`` → ``insert_append`` 一版。多轮 append → ver 累积、读最新全文。
  - serialise → PG → deserialise → model 必须逐字节 lossless（signature 不丢）。
  - 泳道 / session 隔离：不同 session_id 不串（session_id 含 lane 前缀，本就是天然键）。

集成测试（真实 Postgres，testcontainers）：整个正确性故事是 append 出新版本、
select_latest 取最新全文、跨 session 不串——mock pg 等于什么都没测。
"""

from __future__ import annotations

import json
import logging

import pytest

from app.agent.neutral import ContentBlock, Message, Role, ToolCall
from app.agent.session import (
    TRANSCRIPT_MAX_BYTES,
    TRANSCRIPT_MAX_MESSAGES,
    append_session,
    load_session,
)
from app.domain.session_transcript import SessionTranscript
from app.runtime.persist import select_all_versions, select_latest
from tests.runtime.conftest import migrate

pytestmark = pytest.mark.integration


@pytest.fixture
async def session_db(test_db):
    """Build the SessionTranscript table on the test db."""
    await migrate(SessionTranscript, test_db)
    yield test_db


# ---------------------------------------------------------------------------
# Cold start
# ---------------------------------------------------------------------------


async def test_load_missing_session_returns_empty(session_db):
    # first time / cleared db → cold start, no error.
    assert await load_session("coe-x:akao:2026-06-04") == []


# ---------------------------------------------------------------------------
# Round-trip: append then load
# ---------------------------------------------------------------------------


async def test_append_then_load_roundtrips_messages(session_db):
    sid = "coe-x:akao:2026-06-04"
    msgs = [
        Message(role=Role.USER, content="你醒了，现在是晚餐时间"),
        Message(role=Role.ASSISTANT, content="我看看餐桌"),
    ]
    await append_session(sid, msgs)

    loaded = await load_session(sid)
    assert [m.role for m in loaded] == [Role.USER, Role.ASSISTANT]
    assert loaded[0].text() == "你醒了，现在是晚餐时间"
    assert loaded[1].text() == "我看看餐桌"


async def test_append_accumulates_across_rounds(session_db):
    sid = "coe-x:akao:2026-06-04"
    await append_session(sid, [Message(role=Role.USER, content="round1")])
    await append_session(sid, [Message(role=Role.ASSISTANT, content="round2")])
    loaded = await load_session(sid)
    assert [m.text() for m in loaded] == ["round1", "round2"]


async def test_empty_new_messages_is_noop(session_db):
    # appending nothing must not create a row (load still cold-starts).
    await append_session("coe-x:akao:2026-06-04", [])
    assert await load_session("coe-x:akao:2026-06-04") == []


# ---------------------------------------------------------------------------
# Versioning: each append is a new version; select_latest reads the newest full
# transcript; old versions are retained (durable history, queryable).
# ---------------------------------------------------------------------------


async def test_each_append_bumps_version_latest_carries_full_transcript(session_db):
    sid = "coe-x:akao:2026-06-04"
    await append_session(sid, [Message(role=Role.USER, content="a")])
    await append_session(sid, [Message(role=Role.ASSISTANT, content="b")])
    await append_session(sid, [Message(role=Role.USER, content="c")])

    versions = await select_all_versions(SessionTranscript, {"session_id": sid})
    assert [v.ver for v in versions] == [1, 2, 3]

    latest = await select_latest(SessionTranscript, {"session_id": sid})
    payload = json.loads(latest.transcript_json)
    assert [d["content"] for d in payload] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Lossless replay: tool calls + results + provider signature survive PG
# ---------------------------------------------------------------------------


async def test_tool_call_and_result_with_signature_survive_roundtrip(session_db):
    sid = "coe-x:akao:2026-06-04"
    msgs = [
        Message(role=Role.USER, content="该广播了吗"),
        Message(
            role=Role.ASSISTANT,
            content="",
            reasoning_content="想了想",
            tool_calls=[
                ToolCall(
                    id="c1",
                    name="emit_event",
                    arguments={"summary": "晚餐进行中"},
                    signature=b"\x00\xff gemini-thought",
                )
            ],
        ),
        Message(role=Role.TOOL, content="emitted", tool_call_id="c1"),
    ]
    await append_session(sid, msgs)

    loaded = await load_session(sid)
    assistant = next(m for m in loaded if m.role == Role.ASSISTANT)
    assert assistant.reasoning_content == "想了想"
    assert assistant.tool_calls[0].arguments == {"summary": "晚餐进行中"}
    # the provider-private blob must NOT be lost — replay would drift otherwise.
    assert assistant.tool_calls[0].signature == b"\x00\xff gemini-thought"
    tool_msg = next(m for m in loaded if m.role == Role.TOOL)
    assert tool_msg.tool_call_id == "c1"


async def test_multimodal_content_survives_roundtrip(session_db):
    sid = "coe-x:akao:2026-06-04"
    msgs = [
        Message(
            role=Role.TOOL,
            content=[
                ContentBlock.from_text("@3.png:"),
                ContentBlock.from_image_url({"url": "https://x/3.png"}),
            ],
            tool_call_id="c1",
        ),
    ]
    await append_session(sid, msgs)
    loaded = await load_session(sid)
    assert isinstance(loaded[0].content, list)
    assert loaded[0].content[1].image_url == {"url": "https://x/3.png"}


# ---------------------------------------------------------------------------
# Corrupt transcript_json → log + cold start (never crash the run)
# ---------------------------------------------------------------------------


async def test_corrupt_transcript_json_cold_starts_with_warning(session_db, caplog):
    sid = "coe-x:akao:2026-06-04"
    # write a row whose transcript_json is not valid replay JSON.
    from app.runtime.persist import insert_append

    await insert_append(
        SessionTranscript(session_id=sid, transcript_json="{not json at all")
    )
    with caplog.at_level(logging.WARNING):
        loaded = await load_session(sid)
    assert loaded == []
    assert any("transcript" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# Safety valve: transcript cap drops oldest + logs, never grows unbounded.
# (The cap is pure logic, but verify it still fires end-to-end through PG.)
# ---------------------------------------------------------------------------


async def test_transcript_count_cap_drops_oldest_and_logs(session_db, caplog):
    sid = "coe-x:akao:2026-06-04"
    overflow = TRANSCRIPT_MAX_MESSAGES + 20
    with caplog.at_level(logging.WARNING):
        await append_session(
            sid,
            [Message(role=Role.USER, content=f"m{i}") for i in range(overflow)],
        )
    loaded = await load_session(sid)
    assert len(loaded) <= TRANSCRIPT_MAX_MESSAGES
    # the OLDEST messages were dropped, the newest kept
    assert loaded[-1].text() == f"m{overflow - 1}"
    assert loaded[0].text() != "m0"
    assert any("transcript" in r.message.lower() for r in caplog.records)


def _transcript_bytes(messages: list[Message]) -> int:
    return len(
        json.dumps(
            [m.to_replay_dict() for m in messages], ensure_ascii=False
        ).encode("utf-8")
    )


async def test_transcript_byte_cap_drops_oldest_and_logs(session_db, caplog):
    # Few messages, but each carries a huge payload → far over the byte cap
    # while well under the message-count cap. The byte cap must still fire.
    sid = "coe-x:akao:2026-06-04"
    big = "x" * (TRANSCRIPT_MAX_BYTES // 4)
    msgs = [Message(role=Role.USER, content=f"{i}-{big}") for i in range(5)]
    assert len(msgs) <= TRANSCRIPT_MAX_MESSAGES
    assert _transcript_bytes(msgs) > TRANSCRIPT_MAX_BYTES

    with caplog.at_level(logging.WARNING):
        await append_session(sid, msgs)

    loaded = await load_session(sid)
    assert _transcript_bytes(loaded) <= TRANSCRIPT_MAX_BYTES
    assert loaded[-1].text() == f"4-{big}"
    assert loaded[0].text() != f"0-{big}"
    assert any("transcript" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# Lane / session isolation: session_id already carries lane (lane:actor:date),
# so different lanes are different session_ids → different keys → never串.
# This is the framework 三步检查第 3 步的端到端隔离断言.
# ---------------------------------------------------------------------------


async def test_different_session_ids_do_not_cross_contaminate(session_db):
    prod_sid = "prod:akao:2026-06-04"
    coe_sid = "coe-x:akao:2026-06-04"
    await append_session(prod_sid, [Message(role=Role.USER, content="prod-意识流")])
    await append_session(coe_sid, [Message(role=Role.USER, content="coe-意识流")])

    prod_loaded = await load_session(prod_sid)
    coe_loaded = await load_session(coe_sid)
    assert [m.text() for m in prod_loaded] == ["prod-意识流"]
    assert [m.text() for m in coe_loaded] == ["coe-意识流"]


async def test_different_actors_same_lane_do_not_cross_contaminate(session_db):
    akao_sid = "coe-x:akao:2026-06-04"
    world_sid = "coe-x:world:2026-06-04"
    await append_session(akao_sid, [Message(role=Role.USER, content="akao 的")])
    await append_session(world_sid, [Message(role=Role.USER, content="world 的")])

    assert (await load_session(akao_sid))[0].text() == "akao 的"
    assert (await load_session(world_sid))[0].text() == "world 的"
