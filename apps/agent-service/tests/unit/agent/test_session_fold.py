"""折叠机制纯逻辑层契约 — transcript 到阈值整卷压成一条合成 USER 消息 (沉淀 Task 1).

钉死 spec 决策 3/4/5 的机制边界（表驱动）：

  * 阈值：100 条触发 / 99 条不触发；策略 None = 完全不折叠（连 load 都不做）。
  * 折叠产物 = **单条合成 USER 消息**，上半沉淀正文 + 尾部机制载荷段，两段由
    固定标记行（``[transcript-fold]`` / ``[fold-markers]``）可靠切分。
  * marker 保全（铁律①③）：被折叠各轮的完整 round marker 字符串逐行进载荷段——
    life / world 两套现有幂等扫描 / 游标解析函数**直接调用**断言折叠后仍命中。
  * 重折叠：旧沉淀 + 新轮交回调整篇重写；marker 由**代码做并集**（绝不经回调）。
  * fail-open：回调抛错 / 超时 = 本版不折、transcript 原样不动。
  * 铁律②：marker 字符串不得混进沉淀正文——build 时净化 + warning。
"""

from __future__ import annotations

import logging
import uuid

import pytest

import app.agent.session_fold as fold_mod
import app.nodes.life_wake as life_wake
import app.world.engine as world_engine
from app.agent.neutral import Message, Role
from app.agent.session_fold import (
    FOLD_HEADER,
    FOLD_MARKERS_HEADER,
    FOLD_TRIGGER_MESSAGES,
    FoldPolicy,
    build_fold_message,
    extract_round_markers,
    fold_session,
    is_fold_message,
    split_fold_message,
    strip_round_markers,
)

_SID = "coe-x:akao:2026-06-10"

_WORLD_END_CREATED = "2026-06-10T12:00:00+08:00"
_WORLD_END_ACT = str(uuid.uuid5(uuid.NAMESPACE_DNS, "act-seed"))


def _life_round(i: int) -> list[Message]:
    """一轮 life：带 round marker 的 USER stimulus + ASSISTANT 所想。"""
    marker = life_wake._round_marker(f"life-rid-{i:03d}")
    return [
        Message(role=Role.USER, content=f"{marker}\n现在是 12:{i:02d}。你感知到第 {i} 件动静。"),
        Message(role=Role.ASSISTANT, content=f"我想了想第 {i} 件事"),
    ]


def _world_round(i: int) -> list[Message]:
    """一轮 world：带「round_id + 终点游标」marker 的 USER stimulus + ASSISTANT。"""
    marker = world_engine._round_marker(
        f"world-rid-{i:03d}",
        end_created_at=_WORLD_END_CREATED,
        end_act_id=_WORLD_END_ACT,
    )
    return [
        Message(role=Role.USER, content=f"{marker}\n【现实此刻】12:{i:02d}，推演这批动作。"),
        Message(role=Role.ASSISTANT, content=f"世界第 {i} 轮往前流了一格"),
    ]


def _rounds(n_messages: int) -> list[Message]:
    """凑出恰好 ``n_messages`` 条的 life 轮序列（2 条/轮，奇数补一条 ASSISTANT）。"""
    out: list[Message] = []
    i = 0
    while len(out) + 2 <= n_messages:
        out.extend(_life_round(i))
        i += 1
    if len(out) < n_messages:
        out.append(Message(role=Role.ASSISTANT, content="补一条尾巴"))
    return out


class _FakeStore:
    """fold_session 的假存储：记录 load / replace 调用 + ver CAS 语义，替代 PG IO。

    版本语义对齐真存储：load 返回 (messages, 当前 ver)；replace 带 expected_ver
    时做 CAS——期间 ver 前进（有人 append）则放弃写入、返回 False。
    """

    def __init__(self, messages: list[Message]):
        self.messages = list(messages)
        self.ver = 1 if messages else 0
        self.load_calls = 0
        self.replaced: list[Message] | None = None

    async def load(self, session_id: str) -> tuple[list[Message], int]:
        self.load_calls += 1
        return list(self.messages), self.ver

    async def append(self, messages: list[Message]) -> None:
        """模拟并发 append（锁过期后另一轮写入）：ver 前进。"""
        self.messages = self.messages + list(messages)
        self.ver += 1

    async def replace(
        self,
        session_id: str,
        messages: list[Message],
        *,
        expected_ver: int | None = None,
    ) -> bool:
        if expected_ver is not None and expected_ver != self.ver:
            return False
        self.replaced = list(messages)
        self.messages = list(messages)
        self.ver += 1
        return True


@pytest.fixture
def store(monkeypatch):
    """把 session_fold 的存取面换成假存储（默认空 transcript，测试自己塞）。"""
    fake = _FakeStore([])
    monkeypatch.setattr(fold_mod, "load_session_versioned", fake.load)
    monkeypatch.setattr(fold_mod, "replace_session", fake.replace)
    return fake


async def _stub_sediment(prior: str | None, rounds: list[Message]) -> str:
    return "今天上午我帮妹妹讲了题，又把房间收拾了一遍。"


# ---------------------------------------------------------------------------
# 阈值与默认关闭
# ---------------------------------------------------------------------------


async def test_no_policy_means_no_fold_at_all(store):
    """策略 None = 完全不折叠：不 load、不写、返回 False（chat 等调用方零感知）。"""
    store.messages = _rounds(FOLD_TRIGGER_MESSAGES + 10)
    assert await fold_session(_SID, None) is False
    assert store.load_calls == 0
    assert store.replaced is None


@pytest.mark.parametrize(
    ("n_messages", "expect_fold"),
    [
        (FOLD_TRIGGER_MESSAGES - 1, False),  # 99 条不触发
        (FOLD_TRIGGER_MESSAGES, True),  # 100 条触发
        (FOLD_TRIGGER_MESSAGES + 30, True),  # 超过也触发
    ],
)
async def test_fold_triggers_at_threshold(store, n_messages, expect_fold):
    store.messages = _rounds(n_messages)
    calls: list[tuple[str | None, int]] = []

    async def writer(prior: str | None, rounds: list[Message]) -> str:
        calls.append((prior, len(rounds)))
        return "沉淀正文"

    folded = await fold_session(_SID, FoldPolicy(write_sediment=writer))
    assert folded is expect_fold
    if expect_fold:
        assert store.replaced is not None and len(store.replaced) == 1
        assert calls == [(None, n_messages)]  # 首折：无旧沉淀、整卷进回调
    else:
        assert store.replaced is None
        assert calls == []  # 不到阈值不烧回调


# ---------------------------------------------------------------------------
# 折叠产物：单条 USER 消息、两段可靠切分
# ---------------------------------------------------------------------------


async def test_fold_product_is_single_user_message_with_two_sections(store):
    store.messages = _rounds(FOLD_TRIGGER_MESSAGES)
    await fold_session(_SID, FoldPolicy(write_sediment=_stub_sediment))

    assert store.replaced is not None and len(store.replaced) == 1
    folded = store.replaced[0]
    assert folded.role == Role.USER
    assert is_fold_message(folded)

    sediment, markers = split_fold_message(folded)
    assert sediment == "今天上午我帮妹妹讲了题，又把房间收拾了一遍。"
    # 载荷段 = 被折叠各轮的完整 marker 字符串，逐行、保序
    expected = [life_wake._round_marker(f"life-rid-{i:03d}") for i in range(50)]
    assert markers == expected


def test_is_fold_message_gates_role_and_header():
    fold = build_fold_message("沉淀", ["[life-round:x]"])
    assert is_fold_message(fold) is True
    # 普通 USER stimulus（哪怕含 marker）不是折叠消息
    assert is_fold_message(_life_round(0)[0]) is False
    # 同样文本但 ASSISTANT role：不是（两套扫描都只看 USER，折叠产物必须是 USER）
    impostor = Message(role=Role.ASSISTANT, content=fold.text())
    assert is_fold_message(impostor) is False


def test_build_then_split_roundtrip_with_empty_markers():
    """没有任何 marker 的卷（理论边界）也能 build / split 回来。"""
    fold = build_fold_message("只有沉淀没有载荷", [])
    sediment, markers = split_fold_message(fold)
    assert sediment == "只有沉淀没有载荷"
    assert markers == []


def test_split_non_fold_message_raises():
    with pytest.raises(ValueError):
        split_fold_message(Message(role=Role.USER, content="普通消息"))


# ---------------------------------------------------------------------------
# marker 保全：现有 life / world 扫描函数对折叠后 transcript 仍命中（铁律③前半）
# ---------------------------------------------------------------------------


async def test_life_idempotent_scan_still_hits_after_fold(store):
    store.messages = _rounds(FOLD_TRIGGER_MESSAGES)
    await fold_session(_SID, FoldPolicy(write_sediment=_stub_sediment))
    folded_history = store.replaced

    # 直接调 life 的现有扫描：被折叠的每一轮都仍判「已处理」
    for i in range(50):
        assert life_wake._round_already_processed(folded_history, f"life-rid-{i:03d}")
    # 没出现过的轮仍判未处理（不误伤）
    assert not life_wake._round_already_processed(folded_history, "life-rid-999")


async def test_world_scan_and_cursor_parse_still_work_after_fold(store):
    rounds = _world_round(0) + _world_round(1) + _rounds(FOLD_TRIGGER_MESSAGES - 4)
    store.messages = rounds
    await fold_session(_SID, FoldPolicy(write_sediment=_stub_sediment))
    folded_history = store.replaced

    # 直接调 world 的现有函数：命中 + 从 marker 解析出终点游标
    assert world_engine._round_in_history(folded_history, "world-rid-001")
    assert world_engine._round_already_processed(folded_history, "world-rid-001") == (
        _WORLD_END_CREATED,
        _WORLD_END_ACT,
    )
    assert not world_engine._round_in_history(folded_history, "world-rid-999")


async def test_world_empty_batch_marker_survives_fold(store):
    """空批次 marker（``end:-``）也保全：命中但无终点游标（与折叠前语义一致）。"""
    empty_marker = world_engine._round_marker(
        "world-rid-empty", end_created_at=None, end_act_id=None
    )
    store.messages = [
        Message(role=Role.USER, content=f"{empty_marker}\n例行看一眼世界。"),
        Message(role=Role.ASSISTANT, content="世界安静流动"),
    ] + _rounds(FOLD_TRIGGER_MESSAGES - 2)
    await fold_session(_SID, FoldPolicy(write_sediment=_stub_sediment))
    folded_history = store.replaced

    assert world_engine._round_in_history(folded_history, "world-rid-empty")
    assert world_engine._round_already_processed(folded_history, "world-rid-empty") is None


def test_extract_round_markers_only_scans_user_messages():
    """marker 印在 USER stimulus 里；ASSISTANT 复述的 marker 不算（对齐两套扫描的口径）。"""
    msgs = [
        Message(role=Role.USER, content="[life-round:u1]\n现在是中午。"),
        Message(role=Role.ASSISTANT, content="我看到 [life-round:echo] 这行字"),
        Message(role=Role.TOOL, content="[life-round:tool]", tool_call_id="c1"),
    ]
    assert extract_round_markers(msgs) == ["[life-round:u1]"]


# ---------------------------------------------------------------------------
# 重折叠：旧沉淀 + 新轮整篇重写；marker 由代码并集（绝不经回调）
# ---------------------------------------------------------------------------


async def test_refold_unions_markers_in_code_not_via_callback(store):
    old_markers = ["[life-round:old-1]", "[world-round:old-2|end:-]"]
    old_fold = build_fold_message("旧沉淀：上午的事。", old_markers)
    new_rounds = _rounds(FOLD_TRIGGER_MESSAGES - 1)
    store.messages = [old_fold] + new_rounds

    seen: list[tuple[str | None, list[Message]]] = []

    async def writer(prior: str | None, rounds: list[Message]) -> str:
        seen.append((prior, rounds))
        return "重写后的整篇沉淀（不含任何 marker）"

    assert await fold_session(_SID, FoldPolicy(write_sediment=writer)) is True

    # 回调拿到的是：旧沉淀正文 + 新轮消息（不含旧折叠消息本身、不含 marker 载荷职责）
    assert len(seen) == 1
    prior, rounds = seen[0]
    assert prior == "旧沉淀：上午的事。"
    assert rounds == new_rounds

    # marker 并集由代码做：回调输出零 marker，载荷段仍是 旧∪新、保序去重
    sediment, markers = split_fold_message(store.replaced[0])
    assert sediment == "重写后的整篇沉淀（不含任何 marker）"
    new_markers = extract_round_markers(new_rounds)
    assert markers == old_markers + new_markers


async def test_refold_dedups_duplicate_markers(store):
    """同一 marker 既在旧载荷又在新轮里（防御性场景）：并集只留一份。"""
    dup = "[life-round:dup-1]"
    old_fold = build_fold_message("旧沉淀", [dup, "[life-round:old-2]"])
    new_rounds = [
        Message(role=Role.USER, content=f"{dup}\n重投的同一轮"),
        Message(role=Role.ASSISTANT, content="想了想"),
    ] + _rounds(FOLD_TRIGGER_MESSAGES - 3)
    store.messages = [old_fold] + new_rounds

    await fold_session(_SID, FoldPolicy(write_sediment=_stub_sediment))
    _, markers = split_fold_message(store.replaced[0])
    assert markers.count(dup) == 1
    assert markers[:2] == [dup, "[life-round:old-2]"]


# ---------------------------------------------------------------------------
# fail-open：回调失败 = 本版不折、transcript 原样不动
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("exc", [RuntimeError("llm down"), TimeoutError()])
async def test_callback_failure_fails_open(store, caplog, exc):
    before = _rounds(FOLD_TRIGGER_MESSAGES)
    store.messages = before

    async def failing(prior: str | None, rounds: list[Message]) -> str:
        raise exc

    with caplog.at_level(logging.WARNING):
        assert await fold_session(_SID, FoldPolicy(write_sediment=failing)) is False
    assert store.replaced is None  # 没写任何新版本
    assert store.messages == before  # 原样不动
    assert any("fold" in r.message.lower() for r in caplog.records)  # 不静默


async def test_store_failure_fails_open(store, caplog, monkeypatch):
    """写回失败同样 fail-open（折叠是写回之后的独立步骤，绝不影响已落定的轮）。"""
    store.messages = _rounds(FOLD_TRIGGER_MESSAGES)

    async def broken_replace(
        session_id: str, messages: list[Message], *, expected_ver: int | None = None
    ) -> bool:
        raise RuntimeError("pg down")

    monkeypatch.setattr(fold_mod, "replace_session", broken_replace)
    with caplog.at_level(logging.WARNING):
        assert await fold_session(_SID, FoldPolicy(write_sediment=_stub_sediment)) is False


# ---------------------------------------------------------------------------
# 版本 CAS：load 后、replace 前有人 append（锁过期）→ 放弃本次折叠，不覆写
# ---------------------------------------------------------------------------


async def test_concurrent_append_during_sediment_abandons_fold(store, caplog):
    """沉淀 LLM 期间 ver 前进（并发 append）→ replace CAS 放弃、原样不动、warning。

    场景（codex T3 必改 2）：单飞锁 TTL 600s 不续租，本体轮 + 沉淀超 600s 时锁
    过期、新一轮进入并 append；旧 fold 的整卷覆写若照常落库会把新 append 吞掉。
    乐观并发：load 时记 ver，写入校验 ver 未变；变了 = 放弃本次折叠（fail-open，
    下次到阈值再折），新 append 一条不丢。
    """
    before = _rounds(FOLD_TRIGGER_MESSAGES)
    store.messages = before

    async def racing_writer(prior: str | None, rounds: list[Message]) -> str:
        # 沉淀 LLM 还在跑，另一轮（锁过期后进入）append 了新消息
        await store.append(_life_round(999))
        return "基于过时整卷写出的沉淀"

    with caplog.at_level(logging.WARNING):
        assert (
            await fold_session(_SID, FoldPolicy(write_sediment=racing_writer)) is False
        )

    assert store.replaced is None, "stale 覆写必须被放弃"
    assert store.messages == before + _life_round(999), "并发 append 一条不丢"
    assert any("fold" in r.message.lower() for r in caplog.records), "放弃不静默"


# ---------------------------------------------------------------------------
# 铁律②：marker 不得混进沉淀正文 —— build 时净化
# ---------------------------------------------------------------------------


def test_build_sanitizes_markers_out_of_sediment(caplog):
    dirty = (
        "上午我写了作业。\n"
        "[life-round:leaked]\n"
        f"{FOLD_MARKERS_HEADER}\n"
        "中午吃了面，[world-round:leak2|end:-] 然后睡了会儿。"
    )
    with caplog.at_level(logging.WARNING):
        fold = build_fold_message(dirty, ["[life-round:real]"])
    sediment, markers = split_fold_message(fold)
    assert "[life-round:leaked]" not in sediment
    assert "[world-round:" not in sediment
    assert FOLD_MARKERS_HEADER not in sediment
    assert "上午我写了作业。" in sediment
    assert "中午吃了面，" in sediment and "然后睡了会儿。" in sediment
    assert markers == ["[life-round:real]"]  # 载荷段不受净化影响
    assert any("sediment" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# strip_round_markers：回顾证据过滤用的纯函数
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # marker 独占一行：整行删
        ("[life-round:abc]\n现在是中午。", "现在是中午。"),
        # marker 混在行内：只摘掉 marker、其余保留
        ("开头 [world-round:r|end:-] 结尾", "开头  结尾"),
        # 无 marker：原样
        ("纯感知文本\n第二行", "纯感知文本\n第二行"),
        # 多个 marker 都摘
        ("[life-round:a]\n[world-round:b|end:-]\n正文", "正文"),
    ],
)
def test_strip_round_markers(text, expected):
    assert strip_round_markers(text) == expected


def test_fold_header_lines_are_machine_constants():
    """两个段标记是固定行级常量（Task 2 的沉淀 agent 与读取方都按它识别）。"""
    fold = build_fold_message("正文", ["[life-round:x]"])
    text = fold.text()
    lines = text.split("\n")
    assert lines[0] == FOLD_HEADER
    assert FOLD_MARKERS_HEADER in lines
    # 载荷段在尾部：markers 紧跟在最后一个 FOLD_MARKERS_HEADER 行之后
    sep_at = len(lines) - 1 - lines[::-1].index(FOLD_MARKERS_HEADER)
    assert lines[sep_at + 1 :] == ["[life-round:x]"]
