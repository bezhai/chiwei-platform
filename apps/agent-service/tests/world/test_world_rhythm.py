"""这家作息节律的底子 — Task 2.

world 懂这家大致作息（开局写死的节奏，不是逐时刻死表），但**只铺客观、不下
命令、不碰情绪**。节律是给 world 推演时当客观背景喂给 LLM 的素材（千凪早起、
赤尾睡到下午、绫奈工作日上学八点上课），不是"到点强制 X 起床"的规则。

这条测试钉死两件事：
  1. 节律是可读的客观背景文本（喂 LLM 用）；
  2. 它不含命令式 / 情绪式措辞（不是 "强制起床" / "她应该开心"），守住
     world 只铺客观的宪法。
"""

from __future__ import annotations

from app.world.rhythm import household_rhythm


def test_rhythm_is_readable_objective_background():
    """节律是一段可读的客观作息背景，覆盖三姐妹的大致节奏。"""
    text = household_rhythm()
    assert isinstance(text, str)
    assert text.strip()
    # 覆盖三姐妹的客观作息特征（早起 / 睡到下午 / 上学）
    assert "千凪" in text or "chinagi" in text
    assert "赤尾" in text or "akao" in text
    assert "绫奈" in text or "ayana" in text


def test_rhythm_contains_no_command_or_emotion():
    """节律只铺客观、不下命令、不碰情绪 —— world 宪法。"""
    text = household_rhythm()
    forbidden = ["强制", "必须起床", "命令", "应该开心", "应该难过", "心情"]
    for word in forbidden:
        assert word not in text, f"节律不该含命令 / 情绪措辞：{word!r}"
