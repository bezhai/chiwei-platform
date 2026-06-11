"""life 感知白名单：哪些会话的聊天回灌进 life engine（spec Task 5）。

成本止血：现在每条群消息都会唤醒一轮 life。收窄为只有 Dynamic Config
``life_feed_chat_whitelist``（逗号分隔 common_conversation_id 列表）白名单内
的群的消息进 life 成为她的经历；其他群只走 chat 被动回复路径。

口径：
- p2p 私聊不过滤（用户口径只针对"群"），且 p2p 短路时不消费配置
- fail-closed：配置缺失/为空 → 所有群聊回灌全部跳过。配置系统挂了宁可她
  暂时听不见群聊，也不能成本失控。
"""

from __future__ import annotations

import logging

import pytest

from app.life import feed_whitelist as fw

# ---------------------------------------------------------------------------
# parse_whitelist：配置串 -> 白名单集合
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", frozenset()),                          # 空串 -> 空白名单
        ("   ", frozenset()),                       # 全空白 -> 空白名单
        (",,", frozenset()),                        # 只有分隔符 -> 空白名单
        ("c1", frozenset({"c1"})),                  # 单个 id
        ("c1,c2", frozenset({"c1", "c2"})),         # 多个 id
        (" c1 , c2 ,", frozenset({"c1", "c2"})),    # 带空格 / 尾逗号
    ],
)
def test_parse_whitelist(raw: str, expected: frozenset[str]):
    assert fw.parse_whitelist(raw) == expected


# ---------------------------------------------------------------------------
# should_feed_chat_to_life：白名单判定
# ---------------------------------------------------------------------------


def _patch_config(monkeypatch, value: str) -> list[str]:
    """把 Dynamic Config 的 get 换成固定返回值，记录被读的 key。"""
    calls: list[str] = []

    def fake_get(key: str, *, default: str = "") -> str:
        calls.append(key)
        return value

    monkeypatch.setattr(fw.dynamic_config, "get", fake_get)
    return calls


@pytest.mark.asyncio
async def test_group_in_whitelist_feeds(monkeypatch):
    calls = _patch_config(monkeypatch, "c1,c2")
    assert await fw.should_feed_chat_to_life(chat_id="c1", is_p2p=False) is True
    assert calls == [fw.LIFE_FEED_CHAT_WHITELIST_KEY]


@pytest.mark.asyncio
async def test_group_not_in_whitelist_skipped(monkeypatch, caplog):
    """白名单非空、单纯不在名单内：正常挡下，**不**升 warning（info 在挡点打）。"""
    _patch_config(monkeypatch, "c1,c2")
    with caplog.at_level(logging.WARNING):
        assert await fw.should_feed_chat_to_life(chat_id="c9", is_p2p=False) is False
    assert caplog.records == [], "正常名单外挡下是预期行为，不该告警"


@pytest.mark.asyncio
async def test_empty_config_fail_closed_logs_warning(monkeypatch, caplog):
    """配置缺失/为空 -> 所有群聊一律跳过（fail-closed），且必须 warning 可感知。

    空白名单挡下回灌时升 warning（codex T3 小改）：fail-closed 把"配置丢失"和
    "正常名单外"挡成同一个结果，配置系统挂了 / key 被误删若只有 info，止血会
    无声变成"她永远听不见群聊"。
    """
    _patch_config(monkeypatch, "")
    with caplog.at_level(logging.WARNING):
        assert await fw.should_feed_chat_to_life(chat_id="c1", is_p2p=False) is False
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        fw.LIFE_FEED_CHAT_WHITELIST_KEY in r.message for r in warnings
    ), "空白名单挡下必须 warning 点名配置 key（配置丢失要可感知）"


@pytest.mark.asyncio
async def test_group_without_chat_id_skipped(monkeypatch):
    """群聊但 chat_id 缺失：无法判定归属 -> 按 fail-closed 跳过。"""
    _patch_config(monkeypatch, "c1")
    assert await fw.should_feed_chat_to_life(chat_id=None, is_p2p=False) is False


@pytest.mark.asyncio
async def test_p2p_not_filtered_and_skips_config(monkeypatch):
    """p2p 私聊不过滤：空配置下照样放行，且根本不消费 Dynamic Config。"""
    calls = _patch_config(monkeypatch, "")
    assert await fw.should_feed_chat_to_life(chat_id="c1", is_p2p=True) is True
    assert calls == [], "p2p 短路不该读配置"


@pytest.mark.asyncio
async def test_whitelist_value_with_spaces_still_matches(monkeypatch):
    """运维侧配置带空格（' c1 , c2 '）不影响命中。"""
    _patch_config(monkeypatch, " c1 , c2 ")
    assert await fw.should_feed_chat_to_life(chat_id="c2", is_p2p=False) is True
