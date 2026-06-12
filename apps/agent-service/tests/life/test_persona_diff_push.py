"""persona 漂移 diff 飞书推送 — 落版之后告诉 bezhai「她变了哪一笔」(spec 决策 6).

出站契约（Task 3 实证）：agent-service 唯一飞书出口 = chat_response 队列，这里
emit 一条**合成** ChatResponseSegment。这些测试钉死机制层硬约束：

  * 配置缺失 / 空（Dynamic Config ``persona_review_notify``）→ 不推（info 留痕）、
    绝不抛——缺省态是「只落库不通知」；
  * 配置在（``chat_id|bot_name``）→ emit 一条形状钉死的 segment：
    message_id 合成派生 / part_index=0 / is_last / is_proactive / root_id=None /
    bot_name 显式 / lane 显式带在 body / session_id 非空（worker 的
    findOneBy(undefined) footgun 护栏）；
  * message_id 从 (lane, persona, version) 确定性派生——重推同版本撞同一个联合
    Key (message_id, persona_id, part_index)，被 dedup 挡；
  * fail-open 铁律：读配置 / 配置形状不对 / emit 任何一步炸 → 绝不向上抛
    （版本已落，推送只是事后通知），error 留痕可感知。
"""

from __future__ import annotations

import logging

import pytest

import app.life.persona_diff_push as pdp
from app.domain.chat_dataflow import ChatResponseSegment

_LANE = "coe-t3"
_PERSONA = "akao"
_OLD = "出厂身份正文：她是她。"
_NEW = "慢漂后的身份正文：经历长进了她是谁。"
_CHAT = "018f0000-aaaa-bbbb-cccc-000000000001"
_BOT = "chiwei_dev"


def _patch_config(monkeypatch, value):
    """把 Dynamic Config 的 get 换成固定返回值/异常，记录被读的 key。"""
    calls: list[str] = []

    def fake_get(key: str, *, default: str = "") -> str:
        calls.append(key)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(pdp.dynamic_config, "get", fake_get)
    return calls


def _patch_emit(monkeypatch, *, boom: bool = False):
    emitted: list = []

    async def fake_emit(data):
        if boom:
            raise RuntimeError("mq down during diff push")
        emitted.append(data)

    monkeypatch.setattr(pdp, "emit", fake_emit)
    return emitted


async def _push(**overrides):
    kwargs = dict(
        lane=_LANE,
        persona_id=_PERSONA,
        old_narrative=_OLD,
        new_narrative=_NEW,
        version=2,
    )
    kwargs.update(overrides)
    await pdp.push_persona_diff(**kwargs)


# ---------------------------------------------------------------------------
# 配置缺省：不推、info 留痕、不抛
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_config_skips_push_with_info(monkeypatch, caplog):
    """key 缺省（空串）→ 不 emit、info 点名配置 key、绝不抛。"""
    calls = _patch_config(monkeypatch, "")
    emitted = _patch_emit(monkeypatch)

    with caplog.at_level(logging.INFO):
        await _push()  # 不抛

    assert emitted == []
    assert calls == [pdp.PERSONA_REVIEW_NOTIFY_KEY]
    assert any(
        pdp.PERSONA_REVIEW_NOTIFY_KEY in r.message and r.levelno == logging.INFO
        for r in caplog.records
    ), "缺省不推要 info 留痕（点名配置 key）"


@pytest.mark.asyncio
async def test_blank_config_skips_push(monkeypatch):
    """全空白配置 = 缺省：不推。"""
    _patch_config(monkeypatch, "   ")
    emitted = _patch_emit(monkeypatch)

    await _push()

    assert emitted == []


# ---------------------------------------------------------------------------
# 配置在：emit 一条形状钉死的合成 segment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_configured_push_emits_wellformed_segment(monkeypatch):
    """形状契约（worker 消费侧逐字段依赖，一个都不能歪）。"""
    _patch_config(monkeypatch, f"{_CHAT}|{_BOT}")
    emitted = _patch_emit(monkeypatch)

    await _push()

    assert len(emitted) == 1
    seg = emitted[0]
    assert isinstance(seg, ChatResponseSegment)
    assert seg.message_id == f"persona-review:{_LANE}:{_PERSONA}:v2"
    assert seg.persona_id == _PERSONA
    assert seg.part_index == 0
    assert seg.is_last is True
    assert seg.is_proactive is True, "worker 靠它走 proactive 分支"
    assert seg.root_id is None, "无 root → worker 跳过 message 反查、sendText 直发"
    assert seg.chat_id == _CHAT
    assert seg.bot_name == _BOT, "合成消息没有 agent_response 行，bot_name 必须显式带"
    assert seg.lane == _LANE, "sink 不注入 header lane，必须显式带在 body"
    assert seg.status == "success"
    assert seg.session_id, "session_id 必须非空（worker findOneBy(undefined) footgun）"
    assert seg.published_at is not None


@pytest.mark.asyncio
async def test_push_text_carries_new_full_and_version_pointer(monkeypatch):
    """消息文本：新版全文 + 版本对照提示（旧版省略成版本链指针，长度不失控）。"""
    _patch_config(monkeypatch, f"{_CHAT}|{_BOT}")
    emitted = _patch_emit(monkeypatch)

    await _push()

    text = emitted[0].content
    assert _NEW in text, "新版全文必须在"
    assert "v2" in text
    assert "v1" in text, "上一版以版本链指针（v{n-1}）形式给出"
    assert _PERSONA in text
    assert _LANE in text, "bezhai 要能分辨这是哪个泳道的慢漂"


@pytest.mark.asyncio
async def test_push_text_shows_changed_lines_as_diff(monkeypatch):
    """新旧有差异 → 消息前部含 unified diff 变化块：被改的行（- 旧 / + 新）一眼
    可见，owner 不用对照两版全文自己找；新版全文跟在变化块之后。"""
    _patch_config(monkeypatch, f"{_CHAT}|{_BOT}")
    emitted = _patch_emit(monkeypatch)

    old = "她是赤尾。\n她在读高三。\n她喜欢画画。"
    new = "她是赤尾。\n她考完了试，正在等放榜。\n她喜欢画画。"
    await _push(old_narrative=old, new_narrative=new)

    text = emitted[0].content
    assert "-她在读高三。" in text, "被改掉的旧行要在 diff 块里可见"
    assert "+她考完了试，正在等放榜。" in text, "改成的新行要在 diff 块里可见"
    assert new in text, "新版全文仍然完整在消息里"
    assert text.index("-她在读高三。") < text.index(new), (
        "变化摘要（diff 块）在前、新版全文在后"
    )


@pytest.mark.asyncio
async def test_push_text_no_change_says_so(monkeypatch):
    """原样重写（diff 为空）→ 明说「无变化、原样保留」，不留一个空 diff 段让
    owner 猜；新版全文仍在。"""
    _patch_config(monkeypatch, f"{_CHAT}|{_BOT}")
    emitted = _patch_emit(monkeypatch)

    same = "她是赤尾。\n她喜欢画画。"
    await _push(old_narrative=same, new_narrative=same)

    text = emitted[0].content
    assert "无变化" in text
    assert "原样" in text
    assert same in text, "新版全文仍然完整在消息里"


@pytest.mark.asyncio
async def test_config_value_with_spaces_still_parses(monkeypatch):
    """运维侧配置带空格（' chat | bot '）不影响解析。"""
    _patch_config(monkeypatch, f"  {_CHAT} | {_BOT}  ")
    emitted = _patch_emit(monkeypatch)

    await _push()

    assert len(emitted) == 1
    assert emitted[0].chat_id == _CHAT
    assert emitted[0].bot_name == _BOT


# ---------------------------------------------------------------------------
# 幂等：message_id 从 (lane, persona, version) 确定性派生
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_version_message_id_stable_for_dedup(monkeypatch):
    """重推同版本 → 同 message_id（撞 Key 被 dedup 挡）；不同版本 → 不同 id。"""
    _patch_config(monkeypatch, f"{_CHAT}|{_BOT}")
    emitted = _patch_emit(monkeypatch)

    await _push(version=2)
    await _push(version=2)
    await _push(version=3)

    assert emitted[0].message_id == emitted[1].message_id
    assert emitted[2].message_id != emitted[0].message_id


# ---------------------------------------------------------------------------
# fail-open 铁律：任何一步炸都绝不向上抛（版本已落，推送只是事后通知）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_failure_does_not_raise(monkeypatch, caplog):
    """emit（mq publish）炸 → 不抛、error 留痕。"""
    _patch_config(monkeypatch, f"{_CHAT}|{_BOT}")
    _patch_emit(monkeypatch, boom=True)

    with caplog.at_level(logging.ERROR):
        await _push()  # 不抛

    assert any(r.levelno == logging.ERROR for r in caplog.records)


@pytest.mark.asyncio
async def test_config_read_failure_does_not_raise(monkeypatch, caplog):
    """Dynamic Config 系统挂了 → 不抛、error 留痕（绝不影响已落的版本）。"""
    _patch_config(monkeypatch, RuntimeError("dynamic config service down"))
    emitted = _patch_emit(monkeypatch)

    with caplog.at_level(logging.ERROR):
        await _push()  # 不抛

    assert emitted == []
    assert any(r.levelno == logging.ERROR for r in caplog.records)


@pytest.mark.asyncio
async def test_malformed_config_no_push_logs_error(monkeypatch, caplog):
    """形状不对（没有 '|' / 半边为空）= 配置错误：不推 + error 留痕（要可感知，
    与「故意留空」的 info 区分开）。"""
    emitted = _patch_emit(monkeypatch)

    for bad in ("only-a-chat-id", f"{_CHAT}|", f"|{_BOT}"):
        caplog.clear()
        _patch_config(monkeypatch, bad)
        with caplog.at_level(logging.ERROR):
            await _push()  # 不抛
        assert emitted == []
        assert any(
            r.levelno == logging.ERROR for r in caplog.records
        ), f"坏配置 {bad!r} 必须 error 可感知"
