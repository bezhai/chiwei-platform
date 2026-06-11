"""折叠机制 × PG 持久化 — 写回照常 + 折叠另写一版（真实 Postgres，testcontainers）.

钉死两阶段解耦在存储层的形态：

  * **默认无策略时 append_session 字节级行为不变**：存进 PG 的 transcript_json
    必须逐字节等于现状序列化（``to_replay_dict`` + ``json.dumps(ensure_ascii=False)``）
    ——折叠落地后 chat 等不带策略的调用方零感知。
  * 折叠 = 写回之后的独立一版：fold 成功 → 新版本只剩单条折叠消息，旧版本
    留作 durable 历史；fold 失败（回调炸）→ 不写新版、已写回的轮原样在。
  * 重折叠经 PG 往返仍正确：旧沉淀 + 新轮重写、marker 代码并集。
  * 硬上限（200 条 / 256KB）保留作兜底：折叠一直失败时 append 照样被 cap 住。
"""

from __future__ import annotations

import json
import logging

import pytest

import app.nodes.life_wake as life_wake
from app.agent.neutral import Message, Role
from app.agent.session import (
    TRANSCRIPT_MAX_MESSAGES,
    append_session,
    load_session,
)
from app.agent.session_fold import (
    FOLD_TRIGGER_MESSAGES,
    FoldPolicy,
    fold_session,
    is_fold_message,
    split_fold_message,
)
from app.domain.session_transcript import SessionTranscript
from app.runtime.persist import select_all_versions, select_latest
from tests.runtime.conftest import migrate

pytestmark = pytest.mark.integration

_SID = "coe-x:akao:2026-06-10"


@pytest.fixture
async def session_db(test_db):
    await migrate(SessionTranscript, test_db)
    yield test_db


def _life_round(i: int) -> list[Message]:
    marker = life_wake._round_marker(f"rid-{i:03d}")
    return [
        Message(role=Role.USER, content=f"{marker}\n第 {i} 轮的感知。"),
        Message(role=Role.ASSISTANT, content=f"第 {i} 轮的所想"),
    ]


async def _append_rounds(n_rounds: int, *, start: int = 0) -> list[Message]:
    out: list[Message] = []
    for i in range(start, start + n_rounds):
        round_msgs = _life_round(i)
        await append_session(_SID, round_msgs)
        out.extend(round_msgs)
    return out


async def _stub_sediment(prior: str | None, rounds: list[Message]) -> str:
    return "这一上午我过了 50 轮，记得每一件。"


async def test_append_without_policy_is_byte_identical(session_db):
    """不带折叠策略的写回，落进 PG 的字节与现状序列化逐字节一致。"""
    msgs = _life_round(0) + _life_round(1)
    await append_session(_SID, _life_round(0))
    await append_session(_SID, _life_round(1))

    row = await select_latest(SessionTranscript, {"session_id": _SID})
    expected = json.dumps(
        [m.to_replay_dict() for m in msgs], ensure_ascii=False
    )
    assert row.transcript_json == expected
    # 只有两个 append 版本，没有任何折叠版本混进来
    versions = await select_all_versions(SessionTranscript, {"session_id": _SID})
    assert [v.ver for v in versions] == [1, 2]


async def test_fold_writes_one_more_version_and_old_versions_retained(session_db):
    await _append_rounds(FOLD_TRIGGER_MESSAGES // 2)  # 50 轮 = 100 条

    assert await fold_session(_SID, FoldPolicy(write_sediment=_stub_sediment)) is True

    versions = await select_all_versions(SessionTranscript, {"session_id": _SID})
    assert len(versions) == FOLD_TRIGGER_MESSAGES // 2 + 1  # 写回各版 + 折叠一版

    loaded = await load_session(_SID)
    assert len(loaded) == 1
    assert is_fold_message(loaded[0])
    sediment, markers = split_fold_message(loaded[0])
    assert sediment == "这一上午我过了 50 轮，记得每一件。"
    # 经 PG 往返后 life 幂等扫描仍命中每一轮
    for i in range(FOLD_TRIGGER_MESSAGES // 2):
        assert life_wake._round_already_processed(loaded, f"rid-{i:03d}")
    assert len(markers) == FOLD_TRIGGER_MESSAGES // 2


async def test_fold_below_threshold_is_noop_on_pg(session_db):
    await _append_rounds(2)
    assert await fold_session(_SID, FoldPolicy(write_sediment=_stub_sediment)) is False
    versions = await select_all_versions(SessionTranscript, {"session_id": _SID})
    assert [v.ver for v in versions] == [1, 2]  # 没多写版本


async def test_failed_fold_keeps_written_rounds_and_appends_continue(session_db):
    """回调炸 → 不折不写；已写回的轮原样在；后续 append 照常续上。"""
    written = await _append_rounds(FOLD_TRIGGER_MESSAGES // 2)

    async def failing(prior: str | None, rounds: list[Message]) -> str:
        raise RuntimeError("sediment agent down")

    assert await fold_session(_SID, FoldPolicy(write_sediment=failing)) is False

    loaded = await load_session(_SID)
    assert [m.text() for m in loaded] == [m.text() for m in written]

    await append_session(_SID, _life_round(999))
    assert len(await load_session(_SID)) == len(written) + 2


async def test_hard_cap_still_guards_when_fold_keeps_failing(session_db):
    """折叠一直失败时，200 条硬上限仍兜底（带病也不无界膨胀）。"""
    await _append_rounds(TRANSCRIPT_MAX_MESSAGES // 2 + 10)  # 220 条 > 200
    loaded = await load_session(_SID)
    assert len(loaded) <= TRANSCRIPT_MAX_MESSAGES


async def test_concurrent_append_during_fold_aborts_replace(session_db, caplog):
    """load 后、replace 前并发 append 一条 → replace 放弃、transcript 含新 append、无覆写。

    版本 CAS（codex T3 必改 2）经真 PG 往返：fold 的 load 记下当时最新 ver，沉淀
    LLM（这里用回调模拟其 120s 窗口内有人 append——单飞锁 TTL 过期、新一轮进入）
    期间 transcript 版本前进；replace 的条件写入（同一条 SQL 校验 MAX(ver) ==
    期望版本）失败 → 放弃本次折叠（fail-open），新 append 的轮原样在、没有任何
    折叠版本混进来。
    """
    written = await _append_rounds(FOLD_TRIGGER_MESSAGES // 2)
    racing_round = _life_round(777)

    async def racing_sediment(prior: str | None, rounds: list[Message]) -> str:
        # 模拟沉淀 LLM 期间锁过期、另一轮 append 进同一条 session
        await append_session(_SID, racing_round)
        return "基于过时整卷写出的沉淀"

    with caplog.at_level(logging.WARNING):
        assert (
            await fold_session(_SID, FoldPolicy(write_sediment=racing_sediment))
            is False
        )

    loaded = await load_session(_SID)
    assert [m.text() for m in loaded] == [m.text() for m in written + racing_round], (
        "并发 append 的轮必须原样在，stale 覆写必须被放弃"
    )
    assert not any(is_fold_message(m) for m in loaded), "不得有折叠版本落库"
    # 没有多写任何版本：50 个 append + 并发那 1 个 append，无 fold 版本
    versions = await select_all_versions(SessionTranscript, {"session_id": _SID})
    assert [v.ver for v in versions] == list(
        range(1, FOLD_TRIGGER_MESSAGES // 2 + 2)
    )
    assert any("fold" in r.message.lower() for r in caplog.records), "放弃不静默"


async def test_refold_through_pg_unions_markers(session_db):
    # 第一次折叠
    await _append_rounds(FOLD_TRIGGER_MESSAGES // 2)
    await fold_session(_SID, FoldPolicy(write_sediment=_stub_sediment))

    # 折叠后新轮继续 append，攒到再次达阈值（1 条折叠 + 99 条新轮 ≥ 100 触发）
    await _append_rounds(FOLD_TRIGGER_MESSAGES // 2, start=100)

    async def rewrite(prior: str | None, rounds: list[Message]) -> str:
        assert prior == "这一上午我过了 50 轮，记得每一件。"
        return "整篇重写：上午 50 轮加下午 50 轮都在我心里。"

    assert await fold_session(_SID, FoldPolicy(write_sediment=rewrite)) is True

    loaded = await load_session(_SID)
    assert len(loaded) == 1
    sediment, markers = split_fold_message(loaded[0])
    assert sediment == "整篇重写：上午 50 轮加下午 50 轮都在我心里。"
    assert len(markers) == FOLD_TRIGGER_MESSAGES  # 旧 50 + 新 50，一条不丢
    for i in list(range(50)) + list(range(100, 150)):
        assert life_wake._round_already_processed(loaded, f"rid-{i:03d}")
