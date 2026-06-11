"""睡前回顾的意识流证据拼装 × 折叠 — 载荷段过滤、沉淀正文保留（铁律③）.

``_transcript_evidence`` 按生活日窗口读两个自然日的 transcript。折叠落地后它会
读到两种形态，拼证据的口径钉死为：

  * **机制载荷过滤**：round marker 不是她的感知——折叠消息的 ``[fold-markers]``
    载荷段、普通 stimulus 开头的 marker 行，都绝不能进回顾证据。
  * **沉淀正文保留**：折叠消息上半段是她自己的记忆固化，照常进证据。
  * 生活日窗口跨 04:00 两个 session，任一被折叠都同样成立。
"""

from __future__ import annotations

import pytest

import app.nodes.life_wake as life_wake
import app.world.engine as world_engine
from app.agent.neutral import Message, Role
from app.agent.session_fold import build_fold_message
from app.life.review import _transcript_evidence

_SEDIMENT = "上午我陪妹妹改了卷子，心里一直惦记着下午的雨。"
_MARKERS = [
    life_wake._round_marker("life-rid-001"),
    world_engine._round_marker(
        "world-rid-002",
        end_created_at="2026-06-10T12:00:00+08:00",
        end_act_id="9f3c2a1e-0000-5000-8000-000000000000",
    ),
]


def _folded_history() -> list[Message]:
    """折叠后的一天：单条折叠消息 + 折叠后续上的一轮新感知。"""
    return [
        build_fold_message(_SEDIMENT, _MARKERS),
        Message(
            role=Role.USER,
            content=f"{life_wake._round_marker('life-rid-new')}\n现在是 18:00。窗外开始下雨了。",
        ),
        Message(role=Role.ASSISTANT, content="我去把阳台的衣服收进来"),
    ]


def _plain_history() -> list[Message]:
    return [
        Message(
            role=Role.USER,
            content=f"{life_wake._round_marker('life-rid-plain')}\n现在是 23:10。屋里很安静。",
        ),
        Message(role=Role.ASSISTANT, content="今天过得很满，准备睡了"),
        Message(role=Role.TOOL, content="状态已更新", tool_call_id="c1"),
    ]


def test_sediment_kept_and_payload_filtered():
    evidence = _transcript_evidence([("2026-06-10", _folded_history())])

    # 沉淀正文是她的记忆，保留进证据
    assert _SEDIMENT in evidence
    # 机制载荷绝不进证据：载荷段标记行、两种 round marker 全部过滤
    assert "[fold-markers]" not in evidence
    assert "[transcript-fold]" not in evidence
    assert "[life-round:" not in evidence
    assert "[world-round:" not in evidence
    # 折叠后续上的新轮照常进证据（marker 行被滤掉、正文在）
    assert "窗外开始下雨了。" in evidence
    assert "我去把阳台的衣服收进来" in evidence


def test_plain_history_marker_line_filtered_but_text_kept():
    """未折叠的普通 stimulus：开头的 marker 行同样不是她的感知，滤掉、正文保留。"""
    evidence = _transcript_evidence([("2026-06-10", _plain_history())])
    assert "[life-round:" not in evidence
    assert "屋里很安静。" in evidence
    assert "今天过得很满，准备睡了" in evidence
    assert "状态已更新" not in evidence  # TOOL 结果照旧不进证据


@pytest.mark.parametrize("folded_day", [0, 1])
def test_cross_4am_window_either_session_folded(folded_day):
    """生活日跨 04:00 两个自然日的 session，任一被折叠：沉淀保留、载荷过滤。"""
    sessions = [
        ("2026-06-10", _plain_history()),
        ("2026-06-11", _plain_history()),
    ]
    sessions[folded_day] = (sessions[folded_day][0], _folded_history())

    evidence = _transcript_evidence(sessions)
    assert _SEDIMENT in evidence
    assert "[fold-markers]" not in evidence
    assert "[life-round:" not in evidence
    assert "[world-round:" not in evidence
    # 两个自然日的段都在（未折叠那天的正文也没丢）
    assert "（2026-06-10 这个自然日）" in evidence
    assert "（2026-06-11 这个自然日）" in evidence
    assert "屋里很安静。" in evidence


def test_marker_only_user_message_contributes_nothing():
    """整条 USER 只剩 marker（理论边界）：滤掉后为空，不留空壳行。"""
    history = [
        Message(role=Role.USER, content=life_wake._round_marker("life-rid-only")),
        Message(role=Role.ASSISTANT, content="随手记一笔"),
    ]
    evidence = _transcript_evidence([("2026-06-10", history)])
    assert "[life-round:" not in evidence
    assert "〔你当时感知到〕" not in evidence  # 空感知不该出现空前缀行
    assert "随手记一笔" in evidence


def test_empty_days_still_say_no_record():
    evidence = _transcript_evidence([("2026-06-10", []), ("2026-06-11", [])])
    assert "没有留下意识流记录" in evidence
