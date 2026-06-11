"""沉淀 agent 契约 — 折叠占位由真沉淀填充（沉淀 Task 2，spec 决策 4/5）.

:mod:`app.agent.session_fold` 定义了 ``SedimentWriter`` 回调契约；本文件钉死它的
两个实现（:mod:`app.agent.sediment`）的机制层硬约束：

  * 两口吻各自走对 prompt id：life=``life_sediment``、world=``world_sediment``，
    都是 offline-model、无会话（run 不传 session_id）、无工具、max_retries=1；
  * prompt_vars 契约：life=={persona_name, persona_lite}（与 life_day_review 同款薄
    契约）、world=={}（零 vars）；
  * 证据拼装：单条 user 消息——现实此刻 + 旧沉淀（首折如实说没有）+ 被折叠轮的
    USER/ASSISTANT 文本（TOOL 机械确认不进、round marker 摘干净、条目全量不截断）；
  * 成本独立入账：collect_usage 包住沉淀调用、record_round_cost(actor=
    ``{persona}:sediment`` / ``world:sediment``)，round_id 从 (session_id, 触发轮
    round_id) 派生幂等；
  * 失败语义：LLM 抛错 / 硬超时（asyncio.wait_for）/ 空产出都向上抛——交给
    fold_session fail-open（本版不折、transcript 原样不动）。

写什么口吻、留什么细节由沉淀 agent 自己判断（写作纪律在 langfuse prompt 层）；
这里没有内容检测器（赤尾宪法）。
"""

from __future__ import annotations

import asyncio

import pytest

import app.agent.sediment as sediment_mod
import app.agent.session_fold as fold_mod
from app.agent.neutral import Message, Role
from app.agent.sediment import (
    SEDIMENT_TIMEOUT_SECONDS,
    build_life_fold_policy,
    build_world_fold_policy,
)
from app.agent.session_fold import (
    FOLD_TRIGGER_MESSAGES,
    fold_session,
    split_fold_message,
)
from app.agent.trace import _accumulate_usage
from app.memory._persona import PersonaContext

_LANE = "coe-t2"
_PERSONA = "akao"
_SID = "coe-t2:akao:2026-06-11"
_ROUND = "life-round-abc"


def _life_rounds(n_rounds: int) -> list[Message]:
    """凑 n 轮 life：带 round marker 的 USER stimulus + ASSISTANT 所想。"""
    out: list[Message] = []
    for i in range(n_rounds):
        out.append(
            Message(
                role=Role.USER,
                content=f"[life-round:rid-{i:03d}]\n现在是 12:{i:02d}。你感知到第 {i} 件动静。",
            )
        )
        out.append(Message(role=Role.ASSISTANT, content=f"我想了想第 {i} 件事"))
    return out


@pytest.fixture(autouse=True)
def _stub_persona(monkeypatch):
    """load_persona 打桩（沉淀的 prompt_vars 从这取 persona_name / persona_lite）。"""

    async def fake_load_persona(persona_id):
        return PersonaContext(
            persona_id=persona_id, display_name="某姐姐", persona_lite="人设速写"
        )

    monkeypatch.setattr(sediment_mod, "load_persona", fake_load_persona)


@pytest.fixture(autouse=True)
def cost_records(monkeypatch):
    """record_round_cost 打桩：快照 usage（dict 会被复用，记当时值）。"""
    costs: list[dict] = []

    async def fake_record_round_cost(**kwargs):
        costs.append({**kwargs, "usage": dict(kwargs["usage"])})

    monkeypatch.setattr(sediment_mod, "record_round_cost", fake_record_round_cost)
    return costs


def _install_agent(
    monkeypatch,
    *,
    text="她口吻的当天回忆。",
    usage=None,
    exc=None,
    delay=0.0,
):
    """把沉淀模块的 ``Agent`` 换成记录调用参数的桩，返回 captured。"""
    captured: dict = {"runs": []}

    class _FakeAgent:
        def __init__(self, cfg, *, tools=None, **kwargs):
            captured["cfg"] = cfg
            captured["tools"] = tools

        async def run(
            self, messages, *, prompt_vars=None, context=None, session_id=None,
            max_retries=2,
        ):
            captured["runs"].append(
                {
                    "messages": messages,
                    "prompt_vars": prompt_vars,
                    "context": context,
                    "session_id": session_id,
                    "max_retries": max_retries,
                }
            )
            if delay:
                await asyncio.sleep(delay)
            if usage is not None:
                _accumulate_usage(usage)
            if exc is not None:
                raise exc
            return Message(role=Role.ASSISTANT, content=text)

    monkeypatch.setattr(sediment_mod, "Agent", _FakeAgent)
    return captured


def _life_writer():
    return build_life_fold_policy(
        lane=_LANE, persona_id=_PERSONA, session_id=_SID, round_id=_ROUND
    ).write_sediment


def _world_writer():
    return build_world_fold_policy(
        lane=_LANE, session_id=_SID, round_id=_ROUND
    ).write_sediment


def _user_blob(captured) -> str:
    run = captured["runs"][-1]
    assert len(run["messages"]) == 1, "沉淀输入必须是单条 user 消息（一次喂全）"
    assert run["messages"][0].role == Role.USER
    return run["messages"][0].text()


# ---------------------------------------------------------------------------
# 两口吻各自走对 prompt id + 调用契约（无会话 / 无工具 / max_retries=1）
# ---------------------------------------------------------------------------


def test_life_config_pins_prompt_id_and_offline_model():
    assert sediment_mod._LIFE_SEDIMENT_CFG.prompt_id == "life_sediment"
    assert sediment_mod._LIFE_SEDIMENT_CFG.model_id == "offline-model"


def test_world_config_pins_prompt_id_and_offline_model():
    assert sediment_mod._WORLD_SEDIMENT_CFG.prompt_id == "world_sediment"
    assert sediment_mod._WORLD_SEDIMENT_CFG.model_id == "offline-model"


async def test_life_writer_uses_life_config(monkeypatch):
    captured = _install_agent(monkeypatch)
    await _life_writer()(None, _life_rounds(2))
    assert captured["cfg"] is sediment_mod._LIFE_SEDIMENT_CFG


async def test_world_writer_uses_world_config(monkeypatch):
    captured = _install_agent(monkeypatch)
    await _world_writer()(None, _life_rounds(2))
    assert captured["cfg"] is sediment_mod._WORLD_SEDIMENT_CFG


async def test_life_prompt_vars_contract(monkeypatch):
    """life prompt_vars 契约 == {persona_name, persona_lite}（薄契约，同 life_day_review）。"""
    captured = _install_agent(monkeypatch)
    await _life_writer()(None, _life_rounds(2))
    assert captured["runs"][-1]["prompt_vars"] == {
        "persona_name": "某姐姐",
        "persona_lite": "人设速写",
    }


async def test_world_prompt_vars_contract_is_empty(monkeypatch):
    """world prompt_vars 契约 == {}（零 vars）。"""
    captured = _install_agent(monkeypatch)
    await _world_writer()(None, _life_rounds(2))
    assert captured["runs"][-1]["prompt_vars"] == {}


async def test_writer_is_sessionless_toolless_max_retries_one(monkeypatch):
    """无会话（run 不传 session_id——绝不把沉淀写回它正要折叠的 transcript）、无工具、max_retries=1。"""
    captured = _install_agent(monkeypatch)
    await _life_writer()(None, _life_rounds(2))
    run = captured["runs"][-1]
    assert run["session_id"] is None
    assert run["max_retries"] == 1
    assert not captured["tools"], "沉淀 agent 无工具（一次 LLM 调用整篇重写）"
    # langfuse 归组：context.session_id 只做 trace 标签（不续接）
    assert run["context"].session_id == _SID
    assert run["context"].persona_id == _PERSONA


# ---------------------------------------------------------------------------
# 证据拼装：现实此刻 / 首折 vs 重折叠 / 条目全量不截断 / marker 与 TOOL 不进
# ---------------------------------------------------------------------------


async def test_first_fold_says_no_prior_sediment(monkeypatch):
    """首折（prior=None）→ 如实说还没有旧沉淀，不冒充。"""
    captured = _install_agent(monkeypatch)
    await _life_writer()(None, _life_rounds(2))
    blob = _user_blob(captured)
    assert "第一次沉淀" in blob
    assert "【现实此刻】" in blob


async def test_refold_passes_prior_sediment_through(monkeypatch):
    """重折叠（prior 非 None）→ 旧沉淀全文透传进证据、首折占位语缺席。"""
    captured = _install_agent(monkeypatch)
    await _life_writer()("上午我帮妹妹讲了题。", _life_rounds(2))
    blob = _user_blob(captured)
    assert "上午我帮妹妹讲了题。" in blob
    assert "第一次沉淀" not in blob


async def test_evidence_keeps_every_entry_without_truncation(monkeypatch):
    """被折叠轮逐条进证据（条目控制、绝不字符截断）；marker 摘干净；TOOL 不进。"""
    rounds = _life_rounds(30)
    rounds.append(Message(role=Role.TOOL, content="状态已更新", tool_call_id="c1"))
    captured = _install_agent(monkeypatch)
    await _life_writer()(None, rounds)
    blob = _user_blob(captured)
    for i in range(30):
        assert f"你感知到第 {i} 件动静。" in blob
        assert f"我想了想第 {i} 件事" in blob
    assert "[life-round:" not in blob, "round marker 是机制载荷，绝不喂给沉淀 agent"
    assert "状态已更新" not in blob, "TOOL 机械确认不是她的经历，不进证据"


async def test_world_evidence_uses_deliberation_labels(monkeypatch):
    """world 证据用推演口吻的标签（输入 / 推演），不是 life 的感知口吻。"""
    captured = _install_agent(monkeypatch)
    await _world_writer()(
        None,
        [
            Message(
                role=Role.USER,
                content="[world-round:r1|end:-]\n【现实此刻】中午，推演这批动作。",
            ),
            Message(role=Role.ASSISTANT, content="世界往前流了一格"),
        ],
    )
    blob = _user_blob(captured)
    assert "推演这批动作。" in blob
    assert "世界往前流了一格" in blob
    assert "[world-round:" not in blob
    assert "〔当时给你的输入〕" in blob
    assert "〔你当时的推演〕" in blob


async def test_life_evidence_uses_her_perception_labels(monkeypatch):
    captured = _install_agent(monkeypatch)
    await _life_writer()(None, _life_rounds(1))
    blob = _user_blob(captured)
    assert "〔你当时感知到〕" in blob
    assert "〔你当时想着 / 说做了〕" in blob


async def test_writer_returns_sediment_text(monkeypatch):
    _install_agent(monkeypatch, text="  到此刻为止我记得这些。  ")
    sediment = await _life_writer()(None, _life_rounds(2))
    assert sediment == "到此刻为止我记得这些。"


def test_instructions_have_no_plot_facts_or_digits():
    """两份代码侧 instruction 零剧情事实、零数字（写作纪律在 langfuse prompt 层）。"""
    for instruction in (
        sediment_mod.life_sediment_instruction(),
        sediment_mod.world_sediment_instruction(),
    ):
        assert "高考" not in instruction
        for name in ("千凪", "赤尾", "绫奈", "chinagi", "akao", "ayana"):
            assert name not in instruction
        assert not any(ch.isdigit() for ch in instruction)


# ---------------------------------------------------------------------------
# 成本：独立 actor + (session_id, 触发轮) 派生幂等 round_id
# ---------------------------------------------------------------------------


async def test_life_cost_lands_on_sediment_actor(monkeypatch, cost_records):
    _install_agent(
        monkeypatch, usage={"input": 70, "output": 7, "total": 77}
    )
    await _life_writer()(None, _life_rounds(2))
    assert len(cost_records) == 1
    rec = cost_records[0]
    assert rec["lane"] == _LANE
    assert rec["actor"] == f"{_PERSONA}:sediment"
    assert rec["round_id"] == sediment_mod._sediment_round_id(_SID, _ROUND)
    assert rec["usage"]["input"] == 70
    assert rec["usage"]["calls"] == 1


async def test_world_cost_lands_on_world_sediment_actor(monkeypatch, cost_records):
    _install_agent(monkeypatch, usage={"input": 5, "output": 5, "total": 10})
    await _world_writer()(None, _life_rounds(2))
    assert cost_records[-1]["actor"] == "world:sediment"


def test_sediment_round_id_is_idempotent_per_trigger_round():
    """同 (session_id, 触发轮) → 同 round_id（重投不重复计成本）；触发轮变 → 变。"""
    a = sediment_mod._sediment_round_id(_SID, _ROUND)
    assert a == sediment_mod._sediment_round_id(_SID, _ROUND)
    assert a != sediment_mod._sediment_round_id(_SID, "another-round")
    assert a != sediment_mod._sediment_round_id("other-session", _ROUND)


# ---------------------------------------------------------------------------
# 失败语义：抛错 / 超时 / 空产出都向上抛（交给 fold_session fail-open）
# ---------------------------------------------------------------------------


async def test_llm_failure_propagates_and_skips_cost(monkeypatch, cost_records):
    _install_agent(monkeypatch, exc=RuntimeError("llm down"))
    with pytest.raises(RuntimeError):
        await _life_writer()(None, _life_rounds(2))
    assert cost_records == []


async def test_hard_timeout_raises_timeout_error(monkeypatch, cost_records):
    """硬超时（asyncio.wait_for 包 LLM 调用）→ TimeoutError 向上抛、不记成本。"""
    _install_agent(monkeypatch, delay=5.0)
    monkeypatch.setattr(sediment_mod, "SEDIMENT_TIMEOUT_SECONDS", 0.05)
    with pytest.raises(TimeoutError):
        await _life_writer()(None, _life_rounds(2))
    assert cost_records == []


def test_timeout_far_below_engine_lock_ttl():
    """硬超时远小于 life / world 单飞锁 TTL（600s）——折叠绝不把锁拖到过期。"""
    assert SEDIMENT_TIMEOUT_SECONDS * 2 <= 600


async def test_empty_output_raises_but_records_cost(monkeypatch, cost_records):
    """空产出 = 折出来会失忆 → 抛错走 fail-open；token 真烧了，成本照记。"""
    _install_agent(monkeypatch, text="   \n  ", usage={"input": 9, "output": 0, "total": 9})
    with pytest.raises(ValueError):
        await _life_writer()(None, _life_rounds(2))
    assert len(cost_records) == 1, "run 正常返回过，token 真烧了，成本照记"


# ---------------------------------------------------------------------------
# 与 fold_session 的端到端：成功折叠落库 / 失败 fail-open 原样不动
# ---------------------------------------------------------------------------


@pytest.fixture
def fold_store(monkeypatch):
    store = {
        "messages": _life_rounds(FOLD_TRIGGER_MESSAGES // 2),
        "ver": FOLD_TRIGGER_MESSAGES // 2,
        "replaced": None,
    }

    async def fake_load(session_id):
        return list(store["messages"]), store["ver"]

    async def fake_replace(session_id, messages, *, expected_ver=None):
        if expected_ver is not None and expected_ver != store["ver"]:
            return False
        store["replaced"] = list(messages)
        store["ver"] += 1
        return True

    monkeypatch.setattr(fold_mod, "load_session_versioned", fake_load)
    monkeypatch.setattr(fold_mod, "replace_session", fake_replace)
    return store


async def test_fold_session_with_life_policy_folds_and_persists(
    monkeypatch, fold_store
):
    _install_agent(monkeypatch, text="到此刻为止，我记得今天帮妹妹讲了题。")
    policy = build_life_fold_policy(
        lane=_LANE, persona_id=_PERSONA, session_id=_SID, round_id=_ROUND
    )

    assert await fold_session(_SID, policy) is True

    assert fold_store["replaced"] is not None and len(fold_store["replaced"]) == 1
    sediment, markers = split_fold_message(fold_store["replaced"][0])
    assert sediment == "到此刻为止，我记得今天帮妹妹讲了题。"
    assert len(markers) == FOLD_TRIGGER_MESSAGES // 2, "被折叠各轮 marker 逐行保全"


async def test_fold_session_fails_open_when_sediment_fails(monkeypatch, fold_store):
    """沉淀失败 → 本版不折、transcript 原样不动（已写回的轮不受影响）。"""
    _install_agent(monkeypatch, exc=RuntimeError("llm down"))
    policy = build_life_fold_policy(
        lane=_LANE, persona_id=_PERSONA, session_id=_SID, round_id=_ROUND
    )

    assert await fold_session(_SID, policy) is False
    assert fold_store["replaced"] is None
