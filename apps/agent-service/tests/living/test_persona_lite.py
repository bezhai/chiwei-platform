"""读书那条路的「她是谁」：版本链优先、链空退回主表的 ``persona_lite``，进程内缓存 5 分钟.

读书 agent 的 prompt 有两个变量 ``persona_name`` / ``persona_lite``
（:meth:`app.living.persona.chain.PersonaChain.lite_vars`）：

  * 链上有最新版（读 a，不分来源）→ ``persona_lite`` 用链上 narrative；名字仍从
    ``bot_persona`` 主表读。
  * 链空、链上最新版空白、链读失败 → 退回主表的 ``persona_lite``（读书绝不能因为链读不到而塌）。
  * 主表没这一行 → 名字退回 persona_id，正文是空串，链也不再查（链以主表为源）。
  * lane 口径 = ``current_deployment_lane() or "prod"``。
  * 缓存：同一个服务对象（一个进程一份）TTL 300s 内不重查，周级漂移无需失效机制。

单测全程打桩主表读和链读，不碰 DB。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.living.persona.chain as chain_mod
from app.living.persona.chain import PersonaChain, PersonaVersion


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
    monkeypatch.setattr(chain_mod, "find_persona", find_mock)
    monkeypatch.setattr(chain_mod, "read_latest_version", chain_mock)
    return find_mock, chain_mock


async def test_chain_empty_falls_back_to_the_main_table(stub_reads):
    """链空 → 两个变量都来自主表。"""
    assert await PersonaChain().lite_vars(persona_id="akao") == {
        "persona_name": "赤尾",
        "persona_lite": "主表的身份正文。",
    }


async def test_chain_latest_overrides_persona_lite_only(stub_reads):
    """链上有版本 → ``persona_lite`` 来自链；名字仍来自主表。"""
    _, chain_mock = stub_reads
    chain_mock.return_value = _chain_version("链上慢漂后的身份正文。")

    got = await PersonaChain().lite_vars(persona_id="akao")

    assert got == {"persona_name": "赤尾", "persona_lite": "链上慢漂后的身份正文。"}


async def test_owner_version_takes_effect_via_read_a(stub_reads):
    """读 a 不分来源：owner 盖版后链读返回 owner 版，读侧即生效。"""
    _, chain_mock = stub_reads
    chain_mock.return_value = _chain_version("bezhai 盖的正文。", source="owner")

    got = await PersonaChain().lite_vars(persona_id="akao")
    assert got["persona_lite"] == "bezhai 盖的正文。"


async def test_missing_bot_persona_keeps_minimal_fallback(stub_reads):
    """主表没这行 → 名字退回 persona_id、正文空串（链也不再查：链以主表为源）。"""
    find_mock, chain_mock = stub_reads
    find_mock.return_value = None

    got = await PersonaChain().lite_vars(persona_id="ghost")

    assert got == {"persona_name": "ghost", "persona_lite": ""}
    chain_mock.assert_not_awaited()


async def test_chain_read_failure_falls_back_to_main_table(stub_reads):
    """链读抛异常 → 不塌，退回主表正文。"""
    _, chain_mock = stub_reads
    chain_mock.side_effect = RuntimeError("pg down")

    got = await PersonaChain().lite_vars(persona_id="akao")
    assert got["persona_lite"] == "主表的身份正文。"


@pytest.mark.parametrize("blank", ["", "   ", "\n\t  \n"])
async def test_chain_blank_narrative_falls_back_to_main_table(stub_reads, blank):
    """链上最新版正文空白 → 按链空处理，退回主表正文（防御纵深：owner 人工插空等写入口）。"""
    _, chain_mock = stub_reads
    chain_mock.return_value = _chain_version(blank)

    got = await PersonaChain().lite_vars(persona_id="akao")
    assert got["persona_lite"] == "主表的身份正文。"


async def test_chain_lane_defaults_to_prod(stub_reads, monkeypatch):
    """lane 口径 = current_deployment_lane() or "prod"。"""
    _, chain_mock = stub_reads
    monkeypatch.setattr(chain_mod, "current_deployment_lane", lambda: None)

    await PersonaChain().lite_vars(persona_id="akao")
    chain_mock.assert_awaited_once_with(lane="prod", persona_id="akao")


async def test_chain_lane_uses_deployment_lane(stub_reads, monkeypatch):
    _, chain_mock = stub_reads
    monkeypatch.setattr(chain_mod, "current_deployment_lane", lambda: "ppe-review")

    await PersonaChain().lite_vars(persona_id="akao")
    chain_mock.assert_awaited_once_with(lane="ppe-review", persona_id="akao")


async def test_ttl_cache_serves_second_call_without_requery(stub_reads):
    """TTL 内第二次调用走缓存：主表与链都只查一次。"""
    find_mock, chain_mock = stub_reads
    chain_mock.return_value = _chain_version("链上正文。")
    chain = PersonaChain()

    first = await chain.lite_vars(persona_id="akao")
    second = await chain.lite_vars(persona_id="akao")

    assert second == first
    assert find_mock.await_count == 1
    assert chain_mock.await_count == 1


async def test_expired_cache_requeries(stub_reads, monkeypatch):
    """过期后重查：TTL 归零，第二次调用必须重新读库。"""
    find_mock, _ = stub_reads
    monkeypatch.setattr(chain_mod, "LITE_TTL_SECONDS", 0)
    chain = PersonaChain()

    await chain.lite_vars(persona_id="akao")
    await chain.lite_vars(persona_id="akao")

    assert find_mock.await_count == 2
