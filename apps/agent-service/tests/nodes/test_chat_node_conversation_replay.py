"""用户对话回灌 life 信箱这条边。

用户和某 persona 聊完一次，这次对话作为一条 ``external`` event 进**那个
persona 的信箱**（她事后知道"刚和谁聊过啥"）。chat 即时回复快路径不变——回灌
发生在最后一段 response emit 之后，不挡回复。

summary 的「聊了啥」**直接用用户原话**：这是她自己经历过的对话回灌进她自己脑子
（聊的时候就经历了、chat 入口也过了 pre-safety），不是隐私泄露，不需要 LLM 概括
脱敏。原话本身就是最真实的"聊了啥"，概括成二手货反而失真，每轮跑一次 offline LLM
纯浪费——所以回灌**不调任何 LLM / Agent**。只留一个宽松上限（200 字）纯防极端长文，
正常聊天消息根本到不了。``user_message`` 为空（纯图片 / 表情）时退回 ``刚和{谁}聊过
一次``。event_id 用 session_id 让同一轮对话重投幂等；session_id 缺失时跳过回灌（不把
无关对话合并去重成 chat:None）。回灌失败不能拖垮 chat（快路径已经回完了）。
"""

from __future__ import annotations

import pytest

from app.domain.chat_dataflow import ChatRequest


def _happy_path_mocks(cn, monkeypatch, *, user_msg="input", reply_parts=None):
    """装上 chat_node happy-path 跑通所需的最小 mock。

    ``user_msg``    本轮用户消息（find_message_content 返回，喂给 parse_content）。
    ``reply_parts`` 赤尾本轮回复的流式分片（_build_and_stream yield 出来的）。
    """
    if reply_parts is None:
        reply_parts = ["hello ", "world"]

    async def fake_find_msg(mid): return user_msg
    async def fake_find_gray(mid): return {}
    async def fake_guard(p): return "guard"

    async def fake_pre(*a, **k):
        from app.domain.safety import PreSafetyVerdict
        return PreSafetyVerdict(
            pre_request_id="x", message_id="m1", is_blocked=False
        )

    async def fake_stream(*a, **k):
        for p in reply_parts:
            yield p

    async def fake_resolve(p, c): return "bot-x"
    async def fake_set(*a, **k): pass
    async def fake_find_username(uid): return None

    monkeypatch.setattr(cn, "find_message_content", fake_find_msg)
    monkeypatch.setattr(cn, "find_gray_config", fake_find_gray)
    monkeypatch.setattr(cn, "fetch_guard_message", fake_guard)
    monkeypatch.setattr(cn, "run_pre_safety_check", fake_pre)
    monkeypatch.setattr(cn, "resolve_bot_name_for_persona", fake_resolve)
    monkeypatch.setattr(cn, "set_agent_response_bot", fake_set)
    monkeypatch.setattr(cn, "_build_and_stream", fake_stream)
    monkeypatch.setattr(cn, "find_username", fake_find_username)

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
async def test_summary_uses_real_user_message_not_summarized(monkeypatch):
    """核心：summary 直接用用户原话,不概括、不上 LLM。

    回灌是她自己经历过的对话回灌进自己脑子,原话本身就是最真实的"聊了啥"。
    summary 里必须出现用户这轮说的原话。
    """
    from app.nodes import chat_node as cn

    _happy_path_mocks(
        monkeypatch=monkeypatch, cn=cn,
        user_msg="周末想去爬山,你要不要一起",
        reply_parts=["好啊", "几点出发"],
    )
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

    assert len(delivered) == 1
    summary = delivered[0]["summary"]
    # summary 直接含用户原话（不是被概括成的二手话题）。
    assert "周末想去爬山,你要不要一起" in summary, (
        f"summary 应直接用用户原话,实际 {summary!r}"
    )


@pytest.mark.asyncio
async def test_replay_does_not_call_any_llm_or_agent(monkeypatch):
    """回灌路径绝不触碰 LLM / Agent —— 用真实输入,不跑 offline 概括。

    把 chat_node 模块里所有 Agent 相关引用换成会爆的探针,跑完一轮回灌后断言
    它们一次都没被调过。回退后这些符号本应已被删除,这里用 getattr 防御性探测:
    只要还存在就装探针,确保即便残留也不会被回灌路径触达。
    """
    from app.nodes import chat_node as cn

    _happy_path_mocks(
        monkeypatch=monkeypatch, cn=cn,
        user_msg="今天聊点啥",
        reply_parts=["嗯"],
    )
    monkeypatch.setenv("LANE", "coe-t1")

    async def fake_deliver(**kwargs):
        return 1

    monkeypatch.setattr(cn, "deliver_event", fake_deliver)

    agent_calls: list = []

    class _ExplodingAgent:
        def __init__(self, *a, **k):
            agent_calls.append(("init", a, k))

        async def run(self, *a, **k):
            agent_calls.append(("run", a, k))
            raise AssertionError("回灌不应调用任何 LLM / Agent")

    if hasattr(cn, "Agent"):
        monkeypatch.setattr(cn, "Agent", _ExplodingAgent)
    # 旧的概括函数若还在,把它替成会爆的,确保没人再调
    if hasattr(cn, "_summarize_conversation_topic"):
        async def _boom(*a, **k):
            agent_calls.append(("summarize", a, k))
            raise AssertionError("回灌不应概括,应直接用原话")
        monkeypatch.setattr(cn, "_summarize_conversation_topic", _boom)

    req = ChatRequest(
        message_id="m1", persona_id="akao", session_id="s1",
        chat_id="c1", is_p2p=True, user_id="u1", lane="coe-t1",
    )
    await cn.chat_node(req)

    assert agent_calls == [], f"回灌路径不该调任何 LLM/Agent,实际调了 {agent_calls!r}"


@pytest.mark.asyncio
async def test_summary_falls_back_when_user_message_empty(monkeypatch):
    """用户这轮没文字（纯图片 / 表情，渲染为空）时退回兜底文案。"""
    from app.nodes import chat_node as cn

    # 纯表情消息 -> render() 出空文本（v2 且 items 全是 sticker 但渲染空白的极端情形）。
    # 用空字符串模拟 user_message 为空这一终态。
    _happy_path_mocks(
        monkeypatch=monkeypatch, cn=cn,
        user_msg="",  # find_message_content 空 -> 走 fetch-empty 短路,不在这测
        reply_parts=["嗯"],
    )
    monkeypatch.setenv("LANE", "coe-t1")

    delivered: list[dict] = []

    async def fake_deliver(**kwargs):
        delivered.append(kwargs)
        return 1

    monkeypatch.setattr(cn, "deliver_event", fake_deliver)

    # raw_content 空会触发 fetch-empty 短路、根本不回灌；要测"有内容但 render 空"
    # 这种情况,直接调 _replay_conversation_to_mailbox 传空 user_message。
    req = ChatRequest(
        message_id="m1", persona_id="akao", session_id="s1",
        chat_id="c1", is_p2p=True, user_id="u1", lane="coe-t1",
    )
    await cn._replay_conversation_to_mailbox(req, user_message="")

    assert len(delivered) == 1
    summary = delivered[0]["summary"]
    assert "聊过一次" in summary, f"空消息应退回兜底文案,实际 {summary!r}"


@pytest.mark.asyncio
async def test_summary_truncates_overly_long_message(monkeypatch):
    """宽松上限纯防极端长文：超 200 字截断加省略号,正常消息不受影响。"""
    from app.nodes import chat_node as cn

    _happy_path_mocks(cn, monkeypatch)
    monkeypatch.setenv("LANE", "coe-t1")

    delivered: list[dict] = []

    async def fake_deliver(**kwargs):
        delivered.append(kwargs)
        return 1

    monkeypatch.setattr(cn, "deliver_event", fake_deliver)

    long_msg = "啊" * 500
    req = ChatRequest(
        message_id="m1", persona_id="akao", session_id="s1",
        chat_id="c1", is_p2p=True, user_id="u1", lane="coe-t1",
    )
    await cn._replay_conversation_to_mailbox(req, user_message=long_msg)

    assert len(delivered) == 1
    summary = delivered[0]["summary"]
    # 原话被截断到 200 字以内 + 省略号；500 个"啊"绝不全进 summary。
    assert long_msg not in summary, "超长原话不该原样落库"
    assert "…" in summary or "..." in summary, f"截断应带省略号,实际 {summary!r}"
    assert summary.count("啊") <= 200, "截断后原话片段不超过 200 字"


@pytest.mark.asyncio
async def test_normal_short_message_not_truncated(monkeypatch):
    """正常长度消息（远低于 200 字）完整保留,不被防御性上限切坏。"""
    from app.nodes import chat_node as cn

    _happy_path_mocks(cn, monkeypatch)
    monkeypatch.setenv("LANE", "coe-t1")

    delivered: list[dict] = []

    async def fake_deliver(**kwargs):
        delivered.append(kwargs)
        return 1

    monkeypatch.setattr(cn, "deliver_event", fake_deliver)

    msg = "明天的会议改到下午三点了,记得带上季度报表"
    req = ChatRequest(
        message_id="m1", persona_id="akao", session_id="s1",
        chat_id="c1", is_p2p=True, user_id="u1", lane="coe-t1",
    )
    await cn._replay_conversation_to_mailbox(req, user_message=msg)

    assert len(delivered) == 1
    summary = delivered[0]["summary"]
    assert msg in summary, f"正常消息应完整保留,实际 {summary!r}"
    assert "…" not in summary and "..." not in summary, "正常消息不该被截断"


@pytest.mark.asyncio
async def test_replay_summary_uses_username_when_resolvable(monkeypatch):
    """跟谁：能解析到真实名字时 summary 用名字，否则退回 user_id。"""
    from app.nodes import chat_node as cn

    _happy_path_mocks(
        monkeypatch=monkeypatch, cn=cn,
        user_msg="明天的会议改到下午三点了",
        reply_parts=["收到", "，我记下了"],
    )
    monkeypatch.setenv("LANE", "coe-t1")

    async def fake_find_username(uid): return "小明"
    monkeypatch.setattr(cn, "find_username", fake_find_username)

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

    assert len(delivered) == 1
    summary = delivered[0]["summary"]
    assert "小明" in summary, f"能解析名字时 summary 应带名字，实际 {summary!r}"
    assert "明天的会议改到下午三点了" in summary, (
        f"summary 应带用户原话，实际 {summary!r}"
    )


@pytest.mark.asyncio
async def test_replay_event_contract_unchanged(monkeypatch):
    """summary 改回原话不改 event 其余契约：event_id 按 session 幂等 / kind / source。"""
    from app.nodes import chat_node as cn

    _happy_path_mocks(
        monkeypatch=monkeypatch, cn=cn,
        user_msg="今天天气真好",
        reply_parts=["是呀"],
    )
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

    assert len(delivered) == 1
    d = delivered[0]
    assert d["event_id"] == "chat:s1", "event_id 仍按 session 幂等去重"
    assert d["kind"] == "external"
    assert d["source"] == "user:u1"


@pytest.mark.asyncio
async def test_replay_skipped_when_session_id_missing(monkeypatch):
    """session_id 缺失时跳过回灌：不把无关对话合并去重成 chat:None。

    ``ChatRequest.session_id`` 允许为 None（chat_dataflow）。旧实现
    ``event_id=f"chat:{req.session_id}"`` 在 None 时变 ``chat:None``，把不同的
    无关回灌错误合并去重成同一条。session_id 缺失就不回灌（宁可不写，也不错合并）。
    """
    from app.nodes import chat_node as cn

    _happy_path_mocks(cn, monkeypatch)
    monkeypatch.setenv("LANE", "coe-t1")

    delivered: list[dict] = []

    async def fake_deliver(**kwargs):
        delivered.append(kwargs)
        return 1

    monkeypatch.setattr(cn, "deliver_event", fake_deliver)

    req = ChatRequest(
        message_id="m1", persona_id="akao", session_id=None,
        chat_id="c1", is_p2p=True, user_id="u1", lane="coe-t1",
    )
    await cn.chat_node(req)

    assert delivered == [], "session_id 缺失时不该回灌（避免 chat:None 错误合并）"


@pytest.mark.asyncio
async def test_replay_image_message_renders_to_real_text(monkeypatch):
    """用户这轮发纯图片：render 出 ``[图片]`` 占位,直接用作原话(非空 -> 不走兜底)。"""
    from app.nodes import chat_node as cn

    # 纯图片消息 -> parse_content().render() 渲染成 "[图片]"，非空。
    _happy_path_mocks(
        monkeypatch=monkeypatch, cn=cn,
        user_msg='{"v": 2, "text": "", "items": [{"type": "image", "value": "k1"}]}',
        reply_parts=["这张照片拍得真好看"],
    )
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

    assert len(delivered) == 1
    summary = delivered[0]["summary"]
    # [图片] 是真实渲染原话,直接进 summary（非空,不走"聊过一次"兜底）。
    assert "[图片]" in summary, f"图片占位应作为原话进 summary,实际 {summary!r}"


@pytest.mark.asyncio
async def test_replay_occurred_at_is_cst_aware_iso(monkeypatch):
    """回灌 event 的 occurred_at 是 CST aware ISO（不再 Unix 毫秒）。

    旧 bug：``occurred_at=str(int(time.time() * 1000))`` 写 Unix 毫秒，跟 world
    的 CST ISO / life 的 UTC ISO 同框混着喂给 agent、时间窗口比较差 8 小时。
    阶段 0 改成 CST aware ISO（含 +08:00），跟全链路同一个"现在"。
    """
    from app.infra import cst_time
    from app.nodes import chat_node as cn

    _happy_path_mocks(cn, monkeypatch)
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

    assert len(delivered) == 1
    occ = delivered[0]["occurred_at"]
    # 不再是纯 Unix 毫秒数字串
    assert not occ.isdigit(), f"occurred_at 不该再是 Unix 毫秒，实际 {occ!r}"
    # 是 CST aware ISO（带 +08:00），且可被 helper 解析回真实时刻
    assert "+08:00" in occ
    assert cst_time.parse(occ) is not None


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
