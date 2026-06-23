"""load_persona 读侧切换 — 版本链优先、链空 fallback 主表字节级不变.

`app/memory/_persona.py` 是全部读取方（chat agent_stream / 睡前回顾 / 沉淀 /
life_wake / post_actions）的缓存单点。persona 慢漂落版本链后，这里只改一处：

  * 链上有最新版（读 a，不分来源）→ ``persona_lite`` 用链上 narrative；
    其余字段（display_name / persona_core / appearance_detail / error_messages）
    仍从 ``bot_persona`` 主表读。
  * 链空 → 整体 fallback 现行为**字节级不变**。
  * 链读失败 → 同 fallback（persona 注入绝不能塌 chat，照 context.py 姿势）。
  * lane 口径 = ``current_deployment_lane() or "prod"``（与 pages.py 同）。
  * 缓存机制原样：进程内 TTL 300s，周级漂移无需失效机制。

单测全程打桩 find_persona / 链读，不碰 DB。
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.memory._persona as persona_mod
from app.life.persona_chain import PersonaVersion
from app.memory._persona import PersonaContext, load_persona


@pytest.fixture(autouse=True)
def clear_persona_cache():
    """每条测试从空缓存开始（模块级 TTL 缓存是进程共享的）。"""
    persona_mod._persona_cache.clear()
    yield
    persona_mod._persona_cache.clear()


def _bot_persona_row():
    """bot_persona 主表一行的打桩（字段与 app.data.models.BotPersona 对齐）。"""
    return SimpleNamespace(
        persona_id="akao",
        display_name="赤尾",
        persona_core="核心正文（遗留未用）",
        persona_lite="主表的身份正文。",
        default_reply_style="自然",
        error_messages={"rate_limit": "稍等一下嘛"},
        appearance_detail="红发",
    )


def _chain_version(narrative: str, source: str = "review") -> PersonaVersion:
    return PersonaVersion(
        lane="prod",
        persona_id="akao",
        narrative=narrative,
        source=source,
        written_at="2026-06-08T05:00:00+08:00",
        version=2,
    )


@pytest.fixture
def stub_reads(monkeypatch):
    """打桩主表读 + 链读，返回两个 mock 供各测试改返回值 / 数调用次数。"""
    find_mock = AsyncMock(return_value=_bot_persona_row())
    chain_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(persona_mod, "find_persona", find_mock)
    monkeypatch.setattr(persona_mod, "read_latest_persona_version", chain_mock)
    return find_mock, chain_mock


async def test_chain_empty_falls_back_byte_identical(stub_reads):
    """链空 → 与现行为字节级一致：persona_lite 等全部来自主表。"""
    ctx = await load_persona("akao")

    assert ctx == PersonaContext(
        persona_id="akao",
        display_name="赤尾",
        persona_lite="主表的身份正文。",
        persona_core="核心正文（遗留未用）",
        appearance_detail="红发",
        error_messages={"rate_limit": "稍等一下嘛"},
        default_reply_style="自然",
    )


async def test_default_reply_style_from_main_table(stub_reads):
    """default_reply_style 从主表 bot_persona 读进 PersonaContext（per-persona 说话风格）。"""
    ctx = await load_persona("akao")
    assert ctx.default_reply_style == "自然"


async def test_default_reply_style_null_falls_back_to_empty(stub_reads):
    """主表 default_reply_style 为 None → 退回 ""（注入绝不传 None）。"""
    find_mock, _ = stub_reads
    row = _bot_persona_row()
    row.default_reply_style = None
    find_mock.return_value = row

    ctx = await load_persona("akao")
    assert ctx.default_reply_style == ""


async def test_missing_persona_default_reply_style_empty(stub_reads):
    """主表没这行 → fallback PersonaContext 的 default_reply_style 保持默认 ""。"""
    find_mock, _ = stub_reads
    find_mock.return_value = None

    ctx = await load_persona("ghost")
    assert ctx.default_reply_style == ""


async def test_chain_latest_overrides_persona_lite_only(stub_reads):
    """链上有版本 → persona_lite 来自链；其余字段仍来自主表。"""
    _, chain_mock = stub_reads
    chain_mock.return_value = _chain_version("链上慢漂后的身份正文。")

    ctx = await load_persona("akao")

    assert ctx.persona_lite == "链上慢漂后的身份正文。"
    assert ctx.display_name == "赤尾"
    assert ctx.persona_core == "核心正文（遗留未用）"
    assert ctx.appearance_detail == "红发"
    assert ctx.error_messages == {"rate_limit": "稍等一下嘛"}


async def test_owner_version_takes_effect_via_read_a(stub_reads):
    """读 a 不分来源：owner 盖版后链读返回 owner 版，读侧即生效。"""
    _, chain_mock = stub_reads
    chain_mock.return_value = _chain_version("bezhai 盖的正文。", source="owner")

    ctx = await load_persona("akao")
    assert ctx.persona_lite == "bezhai 盖的正文。"


async def test_missing_bot_persona_keeps_minimal_fallback(stub_reads):
    """主表没这行 → 现行为的最小 fallback 不变（链也不再查：链以主表为源）。"""
    find_mock, chain_mock = stub_reads
    find_mock.return_value = None

    ctx = await load_persona("ghost")

    assert ctx == PersonaContext(
        persona_id="ghost",
        display_name="ghost",
        persona_lite="",
    )
    chain_mock.assert_not_awaited()


async def test_chain_read_failure_falls_back_to_main_table(stub_reads):
    """链读抛异常 → 不塌，fallback 主表正文（persona 注入绝不能塌 chat）。"""
    _, chain_mock = stub_reads
    chain_mock.side_effect = RuntimeError("pg down")

    ctx = await load_persona("akao")
    assert ctx.persona_lite == "主表的身份正文。"


@pytest.mark.parametrize("blank", ["", "   ", "\n\t  \n"])
async def test_chain_blank_narrative_falls_back_to_main_table(stub_reads, blank):
    """链上最新版正文空白 → 按链空处理，fallback 主表正文（防御纵深：写侧
    update_persona 已拦空，这里挡 owner 人工插空等其它写入口——五个读取方
    绝不注入空 identity）。"""
    _, chain_mock = stub_reads
    chain_mock.return_value = _chain_version(blank)

    ctx = await load_persona("akao")
    assert ctx.persona_lite == "主表的身份正文。"


async def test_chain_lane_defaults_to_prod(stub_reads, monkeypatch):
    """lane 口径 = current_deployment_lane() or "prod"（与 pages.py 同）。"""
    _, chain_mock = stub_reads
    monkeypatch.setattr(persona_mod, "current_deployment_lane", lambda: None)

    await load_persona("akao")
    chain_mock.assert_awaited_once_with(lane="prod", persona_id="akao")


async def test_chain_lane_uses_deployment_lane(stub_reads, monkeypatch):
    _, chain_mock = stub_reads
    monkeypatch.setattr(
        persona_mod, "current_deployment_lane", lambda: "ppe-review"
    )

    await load_persona("akao")
    chain_mock.assert_awaited_once_with(lane="ppe-review", persona_id="akao")


async def test_ttl_cache_serves_second_call_without_requery(stub_reads):
    """TTL 内第二次调用走缓存：主表与链都只查一次（缓存机制保持原样）。"""
    find_mock, chain_mock = stub_reads
    chain_mock.return_value = _chain_version("链上正文。")

    first = await load_persona("akao")
    second = await load_persona("akao")

    assert second is first
    assert find_mock.await_count == 1
    assert chain_mock.await_count == 1


async def test_expired_cache_requeries(stub_reads):
    """过期后重查：手动把缓存条目推成过期，第二次调用必须重新读库。"""
    find_mock, _ = stub_reads

    ctx = await load_persona("akao")
    persona_mod._persona_cache["akao"] = (ctx, time.monotonic() - 1)

    await load_persona("akao")
    assert find_mock.await_count == 2
