"""chat 收口 → life 信箱回灌的白名单闸口（spec Task 5）。

只有白名单内的群的对话回灌进 life（成为她的经历/唤醒 life 轮）；白名单外的群
与空配置（fail-closed）一律跳过回灌。p2p 私聊不过滤。被挡的只是
``deliver_event`` 这一处回灌——chat 即时回复 emit 照常，安全链不受影响。
"""

from __future__ import annotations

import pytest

from app.domain.chat_dataflow import ChatRequest
from app.life import feed_whitelist as fw
from tests.nodes.test_chat_node_conversation_replay import _happy_path_mocks

WL_CHAT = "019e820c-9134-7113-b8e2-ad4f3b926dde"


def _patch_whitelist(monkeypatch, value: str) -> None:
    """把 Dynamic Config 的白名单 key 钉成固定值（真解析逻辑照走）。"""

    def fake_get(key: str, *, default: str = "") -> str:
        assert key == fw.LIFE_FEED_CHAT_WHITELIST_KEY
        return value

    monkeypatch.setattr(fw.dynamic_config, "get", fake_get)


def _capture_deliver(monkeypatch, cn) -> list[dict]:
    delivered: list[dict] = []

    async def fake_deliver(**kwargs):
        delivered.append(kwargs)
        return 1

    monkeypatch.setattr(cn, "deliver_event", fake_deliver)
    return delivered


def _group_req(**overrides) -> ChatRequest:
    base: dict = dict(
        message_id="m1", persona_id="akao", session_id="s1",
        chat_id=WL_CHAT, is_p2p=False, user_id="u1", lane="coe-t1",
    )
    base.update(overrides)
    return ChatRequest(**base)


@pytest.mark.asyncio
async def test_group_in_whitelist_replays_to_life(monkeypatch):
    """白名单内的群：聊完照常回灌一条 external event 进 persona 信箱。"""
    from app.nodes import chat_node as cn

    _happy_path_mocks(cn, monkeypatch)
    monkeypatch.setenv("LANE", "coe-t1")
    _patch_whitelist(monkeypatch, f"other-id,{WL_CHAT}")
    delivered = _capture_deliver(monkeypatch, cn)

    await cn.chat_node(_group_req())

    assert len(delivered) == 1, "白名单内的群应照常回灌 life"
    assert delivered[0]["persona_id"] == "akao"
    assert delivered[0]["kind"] == "external"


@pytest.mark.asyncio
async def test_group_not_in_whitelist_skips_replay_but_reply_intact(monkeypatch):
    """白名单外的群：不回灌 life（deliver_event 不被调），chat 回复照常 emit。"""
    from app.nodes import chat_node as cn

    _happy_path_mocks(cn, monkeypatch)
    monkeypatch.setenv("LANE", "coe-t1")
    _patch_whitelist(monkeypatch, "some-other-conversation-id")
    delivered = _capture_deliver(monkeypatch, cn)

    # 重新挂 emit 捕获回复段——挡回灌绝不能挡回复。
    emitted: list = []

    async def capture_emit(d):
        emitted.append(d)

    monkeypatch.setattr(cn, "emit", capture_emit)

    await cn.chat_node(_group_req())

    assert delivered == [], "白名单外的群不该回灌 life"
    assert len(emitted) >= 1, "chat 回复路径必须不受白名单影响"
    assert emitted[-1].is_last is True


@pytest.mark.asyncio
async def test_empty_whitelist_fail_closed_skips_all_groups(monkeypatch):
    """空配置（缺失/没配）：所有群聊回灌全部跳过——fail-closed 成本止血。"""
    from app.nodes import chat_node as cn

    _happy_path_mocks(cn, monkeypatch)
    monkeypatch.setenv("LANE", "coe-t1")
    _patch_whitelist(monkeypatch, "")
    delivered = _capture_deliver(monkeypatch, cn)

    await cn.chat_node(_group_req())

    assert delivered == [], "空白名单下任何群聊都不该回灌 life"


@pytest.mark.asyncio
async def test_p2p_not_filtered_even_with_empty_config(monkeypatch):
    """p2p 私聊不过滤：空配置下私聊回灌照常进 life。"""
    from app.nodes import chat_node as cn

    _happy_path_mocks(cn, monkeypatch)
    monkeypatch.setenv("LANE", "coe-t1")
    _patch_whitelist(monkeypatch, "")
    delivered = _capture_deliver(monkeypatch, cn)

    await cn.chat_node(_group_req(is_p2p=True))

    assert len(delivered) == 1, "p2p 不受白名单过滤"
