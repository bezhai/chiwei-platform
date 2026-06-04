"""用户对话回灌 life 信箱这条边 — Task 1.

用户和某 persona 聊完一次，这次对话作为一条 ``external`` event 进**那个
persona 的信箱**（她事后知道"刚和谁聊过"）。chat 即时回复快路径不变——回灌
发生在最后一段 response emit 之后，不挡回复。

第一刀只回灌"发生过一次对话",不抠"她答应了什么"。event_id 用 session_id 让
同一轮对话重投幂等。回灌失败不能拖垮 chat（快路径已经回完了）。
"""

from __future__ import annotations

import pytest

from app.domain.chat_dataflow import ChatRequest


def _happy_path_mocks(cn, monkeypatch):
    """装上 chat_node happy-path 跑通所需的最小 mock。"""
    async def fake_find_msg(mid): return "input"
    async def fake_find_gray(mid): return {}
    async def fake_guard(p): return "guard"

    async def fake_pre(*a, **k):
        from app.domain.safety import PreSafetyVerdict
        return PreSafetyVerdict(
            pre_request_id="x", message_id="m1", is_blocked=False
        )

    async def fake_stream(*a, **k):
        for p in ["hello ", "world"]:
            yield p

    async def fake_resolve(p, c): return "bot-x"
    async def fake_set(*a, **k): pass

    monkeypatch.setattr(cn, "find_message_content", fake_find_msg)
    monkeypatch.setattr(cn, "find_gray_config", fake_find_gray)
    monkeypatch.setattr(cn, "fetch_guard_message", fake_guard)
    monkeypatch.setattr(cn, "run_pre_safety_check", fake_pre)
    monkeypatch.setattr(cn, "resolve_bot_name_for_persona", fake_resolve)
    monkeypatch.setattr(cn, "set_agent_response_bot", fake_set)
    monkeypatch.setattr(cn, "_build_and_stream", fake_stream)

    async def fake_emit(d): pass
    monkeypatch.setattr(cn, "emit", fake_emit)


@pytest.mark.asyncio
async def test_chat_completion_delivers_external_event_to_persona(monkeypatch):
    """聊完一次,对应 persona 信箱多一条 external "刚聊过" event。"""
    from app.nodes import chat_node as cn

    _happy_path_mocks(cn, monkeypatch)

    # lane 隔离由进程级部署泳道决定（与 world/life/取用端统一，必改 3），不是
    # req.lane —— coe-t1 部署下回灌进 coe-t1 信箱。
    monkeypatch.setenv("LANE", "coe-t1")

    delivered: list[dict] = []

    async def fake_deliver(**kwargs):
        delivered.append(kwargs)
        return 1

    monkeypatch.setattr(cn, "deliver_event", fake_deliver)

    req = ChatRequest(
        message_id="m1", persona_id="akao", session_id="s1",
        chat_id="c1", is_p2p=True, user_id="u1", lane="coe-t1",
    )
    await cn.chat_node(req)

    assert len(delivered) == 1, "聊完一次应回灌恰好一条 external event"
    d = delivered[0]
    assert d["lane"] == "coe-t1"          # lane 隔离：进对应泳道的信箱（进程级部署泳道）
    assert d["persona_id"] == "akao"      # 进的是这个 persona 的信箱
    assert d["kind"] == "external"        # 外部消息类
    assert "u1" in d["source"] or "u1" in d.get("summary", "")  # 知道和谁聊的
    assert d["event_id"]                  # 有去重键


@pytest.mark.asyncio
async def test_replay_failure_does_not_break_chat(monkeypatch):
    """回灌失败不能拖垮 chat 快路径(回复早已 emit 完)。"""
    from app.nodes import chat_node as cn

    _happy_path_mocks(cn, monkeypatch)

    async def boom(**kwargs):
        raise RuntimeError("mailbox down")

    monkeypatch.setattr(cn, "deliver_event", boom)

    req = ChatRequest(
        message_id="m1", persona_id="akao", session_id="s1",
        chat_id="c1", is_p2p=True, user_id="u1", lane="coe-t1",
    )
    # 不应抛出——回灌失败被吞掉、只 log
    await cn.chat_node(req)


@pytest.mark.asyncio
async def test_replay_lane_uses_deployment_lane_when_req_lane_empty(monkeypatch):
    """必改 3 复现：req.lane 空时，回灌 lane 用进程级部署泳道(prod)，不写空串。

    旧 bug：回灌用 ``lane=req.lane or ""``。prod 下 req.lane 可能空 → external
    event 进 ``lane=""`` 信箱，而 world/life/取用端全链路用
    ``current_deployment_lane() or "prod"``（即 prod）口径 → life 在 "prod"
    唤醒读不到 ""信箱的 event，对话回灌闭环分叉。
    """
    from app.nodes import chat_node as cn

    _happy_path_mocks(cn, monkeypatch)

    # 进程级部署泳道 = prod（LANE 未设 → None → 归一到 "prod"）
    monkeypatch.delenv("LANE", raising=False)

    delivered: list[dict] = []

    async def fake_deliver(**kwargs):
        delivered.append(kwargs)
        return 1

    monkeypatch.setattr(cn, "deliver_event", fake_deliver)

    # req.lane 空串 —— prod 入口常见（channel-server 未注入 request lane）
    req = ChatRequest(
        message_id="m1", persona_id="akao", session_id="s1",
        chat_id="c1", is_p2p=True, user_id="u1", lane="",
    )
    await cn.chat_node(req)

    assert len(delivered) == 1
    assert delivered[0]["lane"] == "prod", (
        f"回灌该用进程级部署泳道 prod（与 world/life/取用端统一），"
        f"实际 {delivered[0]['lane']!r}"
    )


@pytest.mark.asyncio
async def test_replay_lane_uses_deployment_lane_on_coe(monkeypatch):
    """coe 泳道部署下，回灌 lane = 进程级部署泳道（coe-x），与全链路统一。"""
    from app.nodes import chat_node as cn

    _happy_path_mocks(cn, monkeypatch)
    monkeypatch.setenv("LANE", "coe-x")

    delivered: list[dict] = []

    async def fake_deliver(**kwargs):
        delivered.append(kwargs)
        return 1

    monkeypatch.setattr(cn, "deliver_event", fake_deliver)

    # 即便 req.lane 与部署泳道不一致，回灌走进程级部署泳道（取用端读这个口径）
    req = ChatRequest(
        message_id="m1", persona_id="akao", session_id="s1",
        chat_id="c1", is_p2p=True, user_id="u1", lane="",
    )
    await cn.chat_node(req)

    assert len(delivered) == 1
    assert delivered[0]["lane"] == "coe-x"


@pytest.mark.asyncio
async def test_no_replay_when_no_persona(monkeypatch):
    """fetch-empty 等没真正完成一轮 persona 对话的分支不回灌。

    raw_content 空 → 走"未找到"短路 return,没有真对话发生,不该回灌。
    """
    from app.nodes import chat_node as cn

    async def fake_find_msg(mid): return ""  # 空 -> fetch-empty 分支
    async def fake_find_gray(mid): return {}
    async def fake_guard(p): return "guard"

    async def fake_pre(*a, **k):
        from app.domain.safety import PreSafetyVerdict
        return PreSafetyVerdict(
            pre_request_id="x", message_id="m1", is_blocked=False
        )

    monkeypatch.setattr(cn, "find_message_content", fake_find_msg)
    monkeypatch.setattr(cn, "find_gray_config", fake_find_gray)
    monkeypatch.setattr(cn, "fetch_guard_message", fake_guard)
    monkeypatch.setattr(cn, "run_pre_safety_check", fake_pre)

    async def fake_emit(d): pass
    monkeypatch.setattr(cn, "emit", fake_emit)

    delivered: list = []

    async def fake_deliver(**kwargs):
        delivered.append(kwargs)
        return 1

    monkeypatch.setattr(cn, "deliver_event", fake_deliver)

    req = ChatRequest(
        message_id="m1", persona_id="akao", session_id="s1",
        chat_id="c1", is_p2p=True, user_id="u1", lane="coe-t1",
    )
    await cn.chat_node(req)

    assert delivered == [], "没真正完成对话的分支不该回灌信箱"
