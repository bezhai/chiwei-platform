"""Tests for ``app.agent.session`` — the agent's session续接 context store.

The session store holds a *replay-able* transcript (full Message sequence,
including tool calls + results + provider-private blobs) keyed by a caller-given
``session_id``, in Redis, with a 24h TTL.

Contract (spec Task 1 / decisions 2 & 4):
  - ``load_session`` reads the stored transcript, deserialising losslessly;
    a missing key (expired / first time) is a cold start → ``[]``, never raises.
  - ``append_session`` reads-modifies-writes: appends this round's new messages,
    enforces the turn cap (drop oldest + log.warning), writes back with a
    refreshed 24h TTL.
  - serialise → Redis → deserialise → model must be lossless (signature kept).

These use ``fakeredis`` (the project's established redis test pattern) wrapped in
a real ``RedisCapability`` so the Redis round-trip is genuinely exercised.
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest

from app.agent.neutral import ContentBlock, Message, Role, ToolCall
from app.agent.session import (
    SESSION_TTL_SECONDS,
    TRANSCRIPT_MAX_BYTES,
    TRANSCRIPT_MAX_MESSAGES,
    append_session,
    load_session,
    session_key,
)
from app.capabilities.redis import RedisCapability

pytestmark = pytest.mark.unit


@pytest.fixture
def cap() -> RedisCapability:
    return RedisCapability(fakeredis.aioredis.FakeRedis(decode_responses=True))


# ---------------------------------------------------------------------------
# Cold start
# ---------------------------------------------------------------------------


async def test_load_missing_session_returns_empty(cap):
    # first time / expired key → cold start, no error.
    assert await load_session("world:akao:2026-06-04", cap=cap) == []


# ---------------------------------------------------------------------------
# Round-trip: append then load
# ---------------------------------------------------------------------------


async def test_append_then_load_roundtrips_messages(cap):
    sid = "world:akao:2026-06-04"
    msgs = [
        Message(role=Role.USER, content="你醒了，现在是晚餐时间"),
        Message(role=Role.ASSISTANT, content="我看看餐桌"),
    ]
    await append_session(sid, msgs, cap=cap)

    loaded = await load_session(sid, cap=cap)
    assert [m.role for m in loaded] == [Role.USER, Role.ASSISTANT]
    assert loaded[0].text() == "你醒了，现在是晚餐时间"
    assert loaded[1].text() == "我看看餐桌"


async def test_append_accumulates_across_rounds(cap):
    sid = "world:akao:2026-06-04"
    await append_session(sid, [Message(role=Role.USER, content="round1")], cap=cap)
    await append_session(
        sid, [Message(role=Role.ASSISTANT, content="round2")], cap=cap
    )
    loaded = await load_session(sid, cap=cap)
    assert [m.text() for m in loaded] == ["round1", "round2"]


# ---------------------------------------------------------------------------
# Lossless replay: tool calls + results + provider signature survive
# ---------------------------------------------------------------------------


async def test_tool_call_and_result_with_signature_survive_roundtrip(cap):
    sid = "world:akao:2026-06-04"
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
    await append_session(sid, msgs, cap=cap)

    loaded = await load_session(sid, cap=cap)
    assistant = next(m for m in loaded if m.role == Role.ASSISTANT)
    assert assistant.reasoning_content == "想了想"
    assert assistant.tool_calls[0].arguments == {"summary": "晚餐进行中"}
    # the provider-private blob must NOT be lost — replay would drift otherwise.
    assert assistant.tool_calls[0].signature == b"\x00\xff gemini-thought"
    tool_msg = next(m for m in loaded if m.role == Role.TOOL)
    assert tool_msg.tool_call_id == "c1"


async def test_multimodal_content_survives_roundtrip(cap):
    sid = "world:akao:2026-06-04"
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
    await append_session(sid, msgs, cap=cap)
    loaded = await load_session(sid, cap=cap)
    assert isinstance(loaded[0].content, list)
    assert loaded[0].content[1].image_url == {"url": "https://x/3.png"}


# ---------------------------------------------------------------------------
# TTL refresh
# ---------------------------------------------------------------------------


async def test_append_sets_and_refreshes_ttl(cap):
    sid = "world:akao:2026-06-04"
    raw = cap._client  # the fakeredis client behind the capability
    await append_session(sid, [Message(role=Role.USER, content="a")], cap=cap)
    ttl1 = await raw.ttl(session_key(sid))
    assert 0 < ttl1 <= SESSION_TTL_SECONDS

    # shrink the TTL artificially, then append again → it must be refreshed back
    await raw.expire(session_key(sid), 5)
    await append_session(sid, [Message(role=Role.USER, content="b")], cap=cap)
    ttl2 = await raw.ttl(session_key(sid))
    assert ttl2 > 5


# ---------------------------------------------------------------------------
# Safety valve: transcript cap drops oldest + logs, never grows unbounded
# ---------------------------------------------------------------------------


async def test_transcript_cap_drops_oldest_and_logs(cap, caplog):
    sid = "world:akao:2026-06-04"
    # write well past the cap as plain user turns
    overflow = TRANSCRIPT_MAX_MESSAGES + 20
    import logging

    with caplog.at_level(logging.WARNING):
        await append_session(
            sid,
            [Message(role=Role.USER, content=f"m{i}") for i in range(overflow)],
            cap=cap,
        )
    loaded = await load_session(sid, cap=cap)
    # never exceeds the cap
    assert len(loaded) <= TRANSCRIPT_MAX_MESSAGES
    # the OLDEST messages were dropped, the newest kept
    assert loaded[-1].text() == f"m{overflow - 1}"
    assert loaded[0].text() != "m0"
    # it warned rather than silently truncating
    assert any("transcript" in r.message.lower() for r in caplog.records)


async def test_cap_does_not_start_transcript_on_orphan_tool_result(cap):
    # When trimming the front, never start the kept transcript on a TOOL message
    # whose ASSISTANT tool-call request got dropped — providers reject an
    # orphaned tool result. The cut advances to the next non-TOOL boundary.
    sid = "world:akao:2026-06-04"
    msgs: list[Message] = []
    # build > cap messages, every "round" = assistant(tool_call) + tool(result)
    rounds = TRANSCRIPT_MAX_MESSAGES  # 2 msgs/round → ~2x the cap
    for i in range(rounds):
        msgs.append(
            Message(
                role=Role.ASSISTANT,
                content="",
                tool_calls=[ToolCall(id=f"c{i}", name="x", arguments={})],
            )
        )
        msgs.append(Message(role=Role.TOOL, content="r", tool_call_id=f"c{i}"))
    await append_session(sid, msgs, cap=cap)
    loaded = await load_session(sid, cap=cap)
    assert len(loaded) <= TRANSCRIPT_MAX_MESSAGES
    # the kept transcript must NOT begin with an orphan tool result
    assert loaded[0].role != Role.TOOL


# ---------------------------------------------------------------------------
# Safety valve part 2: byte cap (a long tool result can blow the Redis value /
# replay context even when the message COUNT is tiny). Count cap and byte cap
# fire whichever-first; the byte cap drops the oldest, advances past orphan tool
# results, and logs (never silent).
# ---------------------------------------------------------------------------


def _transcript_bytes(messages: list[Message]) -> int:
    import json

    return len(
        json.dumps(
            [m.to_replay_dict() for m in messages], ensure_ascii=False
        ).encode("utf-8")
    )


async def test_transcript_byte_cap_drops_oldest_and_logs(cap, caplog):
    # Few messages, but each carries a huge tool result → far over the byte cap
    # while well under the message-count cap. The byte cap must still fire.
    sid = "world:akao:2026-06-04"
    big = "x" * (TRANSCRIPT_MAX_BYTES // 4)  # 4 such msgs ≈ 1x over the byte cap
    msgs = [Message(role=Role.USER, content=f"{i}-{big}") for i in range(5)]
    assert len(msgs) <= TRANSCRIPT_MAX_MESSAGES  # count cap does NOT fire here
    assert _transcript_bytes(msgs) > TRANSCRIPT_MAX_BYTES  # byte cap WOULD fire

    import logging

    with caplog.at_level(logging.WARNING):
        await append_session(sid, msgs, cap=cap)

    loaded = await load_session(sid, cap=cap)
    # the stored transcript is bounded by the byte cap
    assert _transcript_bytes(loaded) <= TRANSCRIPT_MAX_BYTES
    # the OLDEST messages were dropped, the newest kept
    assert loaded[-1].text() == f"4-{big}"
    assert loaded[0].text() != f"0-{big}"
    # it warned rather than silently truncating
    assert any("transcript" in r.message.lower() for r in caplog.records)


async def test_byte_cap_does_not_start_transcript_on_orphan_tool_result(cap):
    # The byte-driven cut must obey the same orphan rule as the count-driven cut:
    # never begin the kept transcript on a TOOL result whose ASSISTANT request
    # was dropped. Build oversized rounds of assistant(tool_call) + big tool
    # result so the byte cap (not the count cap) drives the trim.
    sid = "world:akao:2026-06-04"
    big = "y" * (TRANSCRIPT_MAX_BYTES // 3)
    msgs: list[Message] = []
    for i in range(4):  # 8 messages: count cap does not fire, byte cap does
        msgs.append(
            Message(
                role=Role.ASSISTANT,
                content="",
                tool_calls=[ToolCall(id=f"c{i}", name="x", arguments={})],
            )
        )
        msgs.append(
            Message(role=Role.TOOL, content=big, tool_call_id=f"c{i}")
        )
    assert len(msgs) <= TRANSCRIPT_MAX_MESSAGES
    assert _transcript_bytes(msgs) > TRANSCRIPT_MAX_BYTES

    await append_session(sid, msgs, cap=cap)
    loaded = await load_session(sid, cap=cap)
    assert _transcript_bytes(loaded) <= TRANSCRIPT_MAX_BYTES
    # never begins on an orphan tool result
    assert loaded[0].role != Role.TOOL


async def test_single_oversize_message_kept_to_avoid_empty_transcript(cap):
    # Degenerate case: ONE message already exceeds the byte cap. We can't drop it
    # (that would lose this round entirely / leave an empty transcript), so keep
    # it — the cap bounds runaway accumulation, it is not a hard guillotine on a
    # legitimately large single turn. Still observable via a warning.
    sid = "world:akao:2026-06-04"
    huge = "z" * (TRANSCRIPT_MAX_BYTES * 2)
    msgs = [Message(role=Role.USER, content=huge)]
    assert _transcript_bytes(msgs) > TRANSCRIPT_MAX_BYTES

    await append_session(sid, msgs, cap=cap)
    loaded = await load_session(sid, cap=cap)
    # the single oversize message is preserved, not dropped to an empty list
    assert len(loaded) == 1
    assert loaded[0].text() == huge
