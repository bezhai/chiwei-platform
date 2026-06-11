"""世界阶段透传给角色 —— ``render_arc_awareness`` 的单一渲染处.

事故背景：世界阶段（WorldArc）明写「翻页级公共进展」，但角色（life / chat）不读它，
于是 persona 出厂设定与世界阶段脱节（世界已翻页、她还按旧设定过日子）。机制透传：
把最新一版世界阶段渲染成一段**给"活在里面的人"看**的第一人称框架文案 + 阶段全文，
喂进 life 每轮 stimulus 与 chat 的 inner_context。

钉死的几条：

  * 世界阶段的写作纪律只写「在场所有人都知道的公共进展」→ 全 persona 同享同一份、
    透传不破坏信息差。
  * 框架文案是机制层，**绝不硬编任何剧情事实**（高考 / 角色名 / 日期 —— 宪法）。
  * 空链（还没人写过世界阶段）/ 空白 narrative / 读失败 → 返回 ""，调用方整段
    不渲染、不塞占位文案。
"""

from __future__ import annotations

import pytest

import app.domain.arc_awareness as aa
from app.world.arc import WorldArc


def _arc(narrative: str, *, lane: str = "coe-t3") -> WorldArc:
    return WorldArc(
        lane=lane, narrative=narrative, turned_at="2026-06-09T18:00:00+08:00"
    )


def _install_read(monkeypatch, *, result=None, error: Exception | None = None):
    """桩掉 arc_awareness 模块级引用的 read_world_arc，记录读到的 lane。"""
    calls: list[str] = []

    async def fake_read(*, lane):
        calls.append(lane)
        if error is not None:
            raise error
        return result

    monkeypatch.setattr(aa, "read_world_arc", fake_read)
    return calls


@pytest.mark.asyncio
async def test_renders_narrative_inside_first_person_frame(monkeypatch):
    """有世界阶段 → 渲染出框架文案 + 阶段全文（给"活在里面的人"的第一人称视角）。"""
    narrative = "一家人刚搬过来，老二换了新学校，眼下是初夏。"
    _install_read(monkeypatch, result=_arc(narrative))

    out = await aa.render_arc_awareness(lane="coe-t3")

    assert narrative in out, "阶段全文必须原样进段落"
    assert "【你们一家所处的现实阶段】" in out, "必须带平直的第一人称框架标头"
    assert "你" in out.replace(narrative, ""), "框架文案是对她说话的第一人称口吻"


@pytest.mark.asyncio
async def test_reads_by_given_lane(monkeypatch):
    """按调用方给的 lane 读世界阶段（泳道隔离命门同 WorldState）。"""
    calls = _install_read(monkeypatch, result=None)

    await aa.render_arc_awareness(lane="ppe-x")

    assert calls == ["ppe-x"]


@pytest.mark.asyncio
async def test_cold_chain_returns_empty(monkeypatch):
    """空链（read_world_arc 返回 None）→ 返回 ""，调用方整段不渲染、无占位文案。"""
    _install_read(monkeypatch, result=None)

    assert await aa.render_arc_awareness(lane="coe-t3") == ""


@pytest.mark.asyncio
async def test_blank_narrative_returns_empty(monkeypatch):
    """narrative 全空白 → 同空链：不渲染光杆标头。"""
    _install_read(monkeypatch, result=_arc("   \n  "))

    assert await aa.render_arc_awareness(lane="coe-t3") == ""


@pytest.mark.asyncio
async def test_read_error_returns_empty_not_raise(monkeypatch):
    """读失败 → 返回 ""、不抛：透传是上下文增强，绝不能塌掉 chat / 杀掉 life 轮。"""
    _install_read(monkeypatch, error=RuntimeError("db down"))

    assert await aa.render_arc_awareness(lane="coe-t3") == ""


@pytest.mark.asyncio
async def test_frame_has_no_hardcoded_plot_facts(monkeypatch):
    """框架文案绝不硬编剧情事实（高考 / 具体日期数字 / 角色名）——宪法。

    用无数字的占位 narrative 渲染后剥掉 narrative，剩下的就是纯框架文案。
    """
    sentinel = "占位的世界阶段内容"
    _install_read(monkeypatch, result=_arc(sentinel))

    out = await aa.render_arc_awareness(lane="coe-t3")
    frame = out.replace(sentinel, "")

    assert "高考" not in frame, "框架文案不得硬编剧情事实（高考）"
    assert not any(ch.isdigit() for ch in frame), "框架文案不得硬编日期 / 数字事实"
    for name in ("千凪", "赤尾", "绫奈", "chinagi", "akao", "ayana"):
        assert name not in frame, f"框架文案不得硬编角色名 {name!r}"
