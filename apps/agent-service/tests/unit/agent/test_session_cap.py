"""``_cap_transcript`` 边界单测 —— 纯函数、不碰 IO.

session transcript 从 Redis 换 PG 后，旧 ``test_session.py``（fakeredis IO 版）删了，
但 ``_cap_transcript`` 是生产关键的安全阀逻辑（两轴截断 + 不以 orphan TOOL 开头 +
never-empty）。这里直接单测纯函数（比走 ``append_session`` IO 更聚焦），补回 codex T3
点名丢失的两条边界：**orphan TOOL 边界**（count / bytes 两种 cut 都不能让保留段以
TOOL 结果开头——provider 会拒）+ **单条 oversize never-empty**（一条消息自身超 byte
cap 也保留、不把整轮 trim 没）。
"""

from __future__ import annotations

from app.agent.neutral import Message, Role
from app.agent.session import (
    TRANSCRIPT_MAX_BYTES,
    TRANSCRIPT_MAX_MESSAGES,
    _cap_transcript,
    _replay_bytes,
)


def test_under_both_caps_unchanged():
    """不超任一 cap 时原样返回（不做无谓裁剪）。"""
    msgs = [Message(role=Role.USER, content=f"m{i}") for i in range(5)]
    assert _cap_transcript(msgs, "s") == msgs


def test_count_cap_drops_oldest_keeps_newest():
    """超 message-count cap：丢最旧、保最新，长度落到 cap 内。"""
    msgs = [
        Message(role=Role.USER, content=f"m{i}")
        for i in range(TRANSCRIPT_MAX_MESSAGES + 20)
    ]
    kept = _cap_transcript(msgs, "s")
    assert len(kept) <= TRANSCRIPT_MAX_MESSAGES
    assert kept[-1].content == msgs[-1].content  # 最新一条必须留住


def test_byte_cap_drops_oldest():
    """count 不触发、bytes 触发时也要裁，且裁到 byte cap 内。"""
    big = "x" * (TRANSCRIPT_MAX_BYTES // 4)
    msgs = [Message(role=Role.USER, content=f"{i}-{big}") for i in range(5)]
    assert len(msgs) <= TRANSCRIPT_MAX_MESSAGES  # count cap 不触发
    assert _replay_bytes(msgs) > TRANSCRIPT_MAX_BYTES  # 只有 byte cap 触发
    kept = _cap_transcript(msgs, "s")
    assert _replay_bytes(kept) <= TRANSCRIPT_MAX_BYTES


def test_count_cut_never_starts_on_orphan_tool():
    """count 驱动的 cut 落在 TOOL 上时，前进到下一个非 TOOL 边界（不留 orphan）。"""
    head = [
        Message(role=Role.ASSISTANT, content="old0"),
        Message(role=Role.ASSISTANT, content="old1"),
    ]
    tools = [
        Message(role=Role.TOOL, content="t0", tool_call_id="c0"),
        Message(role=Role.TOOL, content="t1", tool_call_id="c1"),
    ]
    rest = [
        Message(role=Role.USER, content=f"u{i}")
        for i in range(TRANSCRIPT_MAX_MESSAGES - 2)
    ]
    # len = MAX+2 → cut = 2，messages[2] 是 TOOL → 必须前进过这两条 TOOL
    msgs = head + tools + rest
    kept = _cap_transcript(msgs, "s")
    assert len(kept) <= TRANSCRIPT_MAX_MESSAGES
    assert kept[0].role != Role.TOOL


def test_byte_cut_never_starts_on_orphan_tool():
    """byte 驱动的 cut 同样不能以 orphan TOOL 开头。"""
    big = "y" * (TRANSCRIPT_MAX_BYTES // 3)
    msgs = []
    for i in range(6):
        msgs.append(Message(role=Role.ASSISTANT, content=f"a{i}"))
        msgs.append(Message(role=Role.TOOL, content=big, tool_call_id=f"c{i}"))
    assert _replay_bytes(msgs) > TRANSCRIPT_MAX_BYTES
    kept = _cap_transcript(msgs, "s")
    assert _replay_bytes(kept) <= TRANSCRIPT_MAX_BYTES
    assert kept[0].role != Role.TOOL


def test_single_oversize_message_never_trimmed_to_empty():
    """单条消息自身就超 byte cap：保留它（cap 限累积、不是单条铡刀），不裁成空。"""
    huge = "z" * (TRANSCRIPT_MAX_BYTES * 2)
    msgs = [Message(role=Role.USER, content=huge)]
    kept = _cap_transcript(msgs, "s")
    assert len(kept) == 1
    assert kept[0].content == huge
