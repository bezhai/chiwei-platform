"""NotebookEntry 持久化契约 — 备忘录 & 日程 app 第一块（本子的底子）.

她随身的小本子：一个本子两种条目，差别**只在挂没挂提醒时间**——没时间的是备忘录
（``remind_at is None``），有时间的是日程（``remind_at`` 有值）。一条事哪天加个时间
就从备忘录变成日程，所以不拆两张表（spec「一个本子、两种条目」）。

设计上钉死的几条：

  * **没有优先级 / 标签 / 分类**（spec 不做结构化那套：人记备忘不填表）。条目就是
    一句大白话 ``content`` + 可选 ``remind_at`` + 状态 ``status``。
  * **as_latest + Version，Key 带 lane**：每次改 / 划 append 一版，对外读永远
    ``select_latest`` 取最新一版（旧版留作历史）。Key 含 lane —— runtime 持久化不会
    自动加 lane，不显式带上 coe / ppe 泳道就会覆盖 prod 的本子（写脏线上私人内容）。
  * **「记一条」幂等**：首写走 ``insert_idempotent``（ver=0），整轮重试用同一
    ``(lane, persona_id, entry_id)`` 再写一次 ON CONFLICT DO NOTHING —— 不重复记
    （对称 act 的 durable 幂等）。改 / 划走 ``insert_append`` append 新版。

集成测试（真实 Postgres）：整个正确性故事是首写幂等 + 改 append 出新版 + select_latest
取最新 + lane 隔离 + active-only 过滤——mock pg 等于什么都没测。
"""

from __future__ import annotations

import pytest

from app.domain.notebook import (
    STATUS_ACTIVE,
    STATUS_DONE,
    STATUS_DROPPED,
    NotebookEntry,
    entry_status_label,
    find_notebook_entry,
    list_notebook_entries,
    note_entry,
    render_notebook,
    update_entry,
)
from app.runtime.persist import select_latest
from tests.runtime.conftest import migrate


@pytest.fixture
async def notebook_db(test_db):
    """Build the NotebookEntry table on the test db."""
    await migrate(NotebookEntry, test_db)
    yield test_db


def _latest_kv(lane: str, persona_id: str, entry_id: str) -> dict:
    return {"lane": lane, "persona_id": persona_id, "entry_id": entry_id}


@pytest.mark.integration
async def test_note_memo_then_read_latest(notebook_db):
    """记一条没时间的备忘 → 落库读回，remind_at 为 None、状态 active。"""
    await note_entry(
        lane="coe-t3",
        persona_id="akao",
        entry_id="e-1",
        content="想看那部新出的动画",
        remind_at=None,
        noted_at="2026-06-13T12:30:00+08:00",
    )

    latest = await select_latest(NotebookEntry, _latest_kv("coe-t3", "akao", "e-1"))
    assert latest is not None
    assert latest.content == "想看那部新出的动画"
    assert latest.remind_at is None
    assert latest.status == STATUS_ACTIVE
    assert latest.noted_at == "2026-06-13T12:30:00+08:00"


@pytest.mark.integration
async def test_note_schedule_carries_remind_at(notebook_db):
    """排一条带时间的日程 → remind_at 落库（备忘 vs 日程只差这一个字段）。"""
    await note_entry(
        lane="coe-t3",
        persona_id="akao",
        entry_id="e-2",
        content="下午三点陪我妹去琴行",
        remind_at="2026-06-13T15:00:00+08:00",
        noted_at="2026-06-13T12:30:00+08:00",
    )

    latest = await select_latest(NotebookEntry, _latest_kv("coe-t3", "akao", "e-2"))
    assert latest is not None
    assert latest.content == "下午三点陪我妹去琴行"
    assert latest.remind_at == "2026-06-13T15:00:00+08:00"
    assert latest.status == STATUS_ACTIVE


@pytest.mark.integration
async def test_note_same_entry_id_is_idempotent(notebook_db):
    """「记一条」幂等命门：同 (lane, persona, entry_id) 再写一次 → 不新增第二条。

    整轮重试 / durable 重投会用同一派生 entry_id 再记一次 —— insert_idempotent 按
    (lane, persona, entry_id, ver=0) 去重，ON CONFLICT DO NOTHING、只落一条。
    """
    for _ in range(2):
        await note_entry(
            lane="coe-t3",
            persona_id="akao",
            entry_id="e-dup",
            content="买猫粮",
            remind_at=None,
            noted_at="2026-06-13T12:30:00+08:00",
        )

    entries = await list_notebook_entries(
        lane="coe-t3", persona_id="akao", active_only=False
    )
    dup = [e for e in entries if e.entry_id == "e-dup"]
    assert len(dup) == 1, f"同 entry_id 重记应只落一条，实得 {len(dup)} 条"
    assert dup[0].content == "买猫粮"


@pytest.mark.integration
async def test_update_content_appends_new_version(notebook_db):
    """改内容 → append 新版，select_latest 读到改后的内容（不是卡在旧的）。"""
    await note_entry(
        lane="coe-t3", persona_id="akao", entry_id="e-3",
        content="给妈妈打电话", remind_at=None,
        noted_at="2026-06-13T12:30:00+08:00",
    )
    await update_entry(
        lane="coe-t3", persona_id="akao", entry_id="e-3",
        content="给妈妈打电话问问她身体",
    )

    latest = await select_latest(NotebookEntry, _latest_kv("coe-t3", "akao", "e-3"))
    assert latest is not None
    assert latest.content == "给妈妈打电话问问她身体"
    assert latest.ver == 1, "改一次应 append 出 ver=1"


@pytest.mark.integration
async def test_update_add_then_clear_remind_at(notebook_db):
    """给备忘补时间 → 变日程；再把时间撤了 → 变回备忘（spec：含补时间 / 撤时间）。"""
    await note_entry(
        lane="coe-t3", persona_id="akao", entry_id="e-4",
        content="整理房间", remind_at=None,
        noted_at="2026-06-13T12:30:00+08:00",
    )
    # 补时间：备忘 → 日程
    await update_entry(
        lane="coe-t3", persona_id="akao", entry_id="e-4",
        remind_at="2026-06-13T16:00:00+08:00",
    )
    after_add = await select_latest(NotebookEntry, _latest_kv("coe-t3", "akao", "e-4"))
    assert after_add is not None
    assert after_add.remind_at == "2026-06-13T16:00:00+08:00"

    # 撤时间：日程 → 备忘（显式传 clear_remind_at=True 把时间撤了）
    await update_entry(
        lane="coe-t3", persona_id="akao", entry_id="e-4",
        clear_remind_at=True,
    )
    after_clear = await select_latest(NotebookEntry, _latest_kv("coe-t3", "akao", "e-4"))
    assert after_clear is not None
    assert after_clear.remind_at is None, "撤时间后该变回 None（备忘）"


@pytest.mark.integration
async def test_update_preserves_unchanged_fields(notebook_db):
    """只改一个字段时其余字段沿用最新一版（不被默认值清掉）。"""
    await note_entry(
        lane="coe-t3", persona_id="akao", entry_id="e-5",
        content="周末爬山", remind_at="2026-06-15T08:00:00+08:00",
        noted_at="2026-06-13T12:30:00+08:00",
    )
    # 只划掉状态，content / remind_at / noted_at 都不该丢
    await update_entry(
        lane="coe-t3", persona_id="akao", entry_id="e-5",
        status=STATUS_DONE,
    )
    latest = await select_latest(NotebookEntry, _latest_kv("coe-t3", "akao", "e-5"))
    assert latest is not None
    assert latest.status == STATUS_DONE
    assert latest.content == "周末爬山", "改状态不该丢内容"
    assert latest.remind_at == "2026-06-15T08:00:00+08:00", "改状态不该丢时间"
    assert latest.noted_at == "2026-06-13T12:30:00+08:00", "改状态不该丢记录时刻"


@pytest.mark.integration
async def test_update_missing_entry_raises(notebook_db):
    """改一条不存在的 entry → 抛 ValueError（工具层把它喂回模型重调；不静默造一条）。"""
    with pytest.raises(ValueError):
        await update_entry(
            lane="coe-t3", persona_id="akao", entry_id="nope",
            content="x",
        )


@pytest.mark.integration
async def test_list_active_only_excludes_done_and_dropped(notebook_db):
    """翻本子默认只看还活着的：active 在、done / dropped 不在。"""
    await note_entry(
        lane="coe-t3", persona_id="akao", entry_id="a", content="还惦记的",
        remind_at=None, noted_at="2026-06-13T12:30:00+08:00",
    )
    await note_entry(
        lane="coe-t3", persona_id="akao", entry_id="b", content="做过的",
        remind_at=None, noted_at="2026-06-13T12:30:00+08:00",
    )
    await update_entry(
        lane="coe-t3", persona_id="akao", entry_id="b",
        status=STATUS_DONE,
    )
    await note_entry(
        lane="coe-t3", persona_id="akao", entry_id="c", content="划掉的",
        remind_at=None, noted_at="2026-06-13T12:30:00+08:00",
    )
    await update_entry(
        lane="coe-t3", persona_id="akao", entry_id="c",
        status=STATUS_DROPPED,
    )

    active = await list_notebook_entries(
        lane="coe-t3", persona_id="akao", active_only=True
    )
    ids = {e.entry_id for e in active}
    assert ids == {"a"}, f"active-only 应只剩还惦记的，实得 {ids}"

    every = await list_notebook_entries(
        lane="coe-t3", persona_id="akao", active_only=False
    )
    assert {e.entry_id for e in every} == {"a", "b", "c"}, "看全部含 done / dropped"


@pytest.mark.integration
async def test_list_takes_latest_version_per_entry(notebook_db):
    """列表对每个 entry 只取最新一版（改过的取改后的，不重复列旧版）。"""
    await note_entry(
        lane="coe-t3", persona_id="akao", entry_id="e", content="旧内容",
        remind_at=None, noted_at="2026-06-13T12:30:00+08:00",
    )
    await update_entry(
        lane="coe-t3", persona_id="akao", entry_id="e",
        content="新内容",
    )

    entries = await list_notebook_entries(
        lane="coe-t3", persona_id="akao", active_only=False
    )
    assert len(entries) == 1, "同一 entry 多版应只列最新一版一行"
    assert entries[0].content == "新内容"


@pytest.mark.integration
async def test_lane_isolation_on_notebook(notebook_db):
    """lane 隔离命门：prod 与 coe 各自的本子绝不互相覆盖 / 互读。"""
    await note_entry(
        lane="prod", persona_id="akao", entry_id="same-id", content="prod-条目",
        remind_at=None, noted_at="2026-06-13T12:30:00+08:00",
    )
    await note_entry(
        lane="coe-t3", persona_id="akao", entry_id="same-id", content="coe-条目",
        remind_at=None, noted_at="2026-06-13T12:30:00+08:00",
    )

    prod = await select_latest(NotebookEntry, _latest_kv("prod", "akao", "same-id"))
    coe = await select_latest(NotebookEntry, _latest_kv("coe-t3", "akao", "same-id"))
    assert prod is not None and coe is not None
    assert prod.content == "prod-条目"
    assert coe.content == "coe-条目"


# ---------------------------------------------------------------------------
# find_notebook_entry — 读单条最新一版（第三块日程到点 gate 读「这条现在还作不作数」
# 的单一来源；update_entry 也复用它，不再各自 inline select_latest）。
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_find_notebook_entry_returns_latest_version(notebook_db):
    """读单条：取这条 entry 的最新一版（改过取改后的），不存在返回 None。"""
    await note_entry(
        lane="coe-t3", persona_id="akao", entry_id="f-1",
        content="原始内容", remind_at="2026-06-13T15:00:00+08:00",
        noted_at="2026-06-13T12:30:00+08:00",
    )
    await update_entry(
        lane="coe-t3", persona_id="akao", entry_id="f-1",
        remind_at="2026-06-13T16:00:00+08:00",
    )

    got = await find_notebook_entry(
        lane="coe-t3", persona_id="akao", entry_id="f-1"
    )
    assert got is not None
    assert got.remind_at == "2026-06-13T16:00:00+08:00", "改期后该读到新一版的 remind_at"

    missing = await find_notebook_entry(
        lane="coe-t3", persona_id="akao", entry_id="nope"
    )
    assert missing is None, "不存在的 entry 读出 None（不抛、不造）"


@pytest.mark.integration
async def test_find_notebook_entry_lane_isolated(notebook_db):
    """读单条 lane 隔离：coe 的本子读不到 prod 同 id 的条目。"""
    await note_entry(
        lane="prod", persona_id="akao", entry_id="dup", content="prod 的",
        remind_at=None, noted_at="2026-06-13T12:30:00+08:00",
    )
    got = await find_notebook_entry(lane="coe-t3", persona_id="akao", entry_id="dup")
    assert got is None, "lane 隔离：coe 读不到 prod 的同 id 条目"


def test_status_constants_are_the_three_states():
    """三态钉死：还惦记 / 做了 / 划了，没有「到点了」这种存储态（到点是派生显示）。"""
    assert {STATUS_ACTIVE, STATUS_DONE, STATUS_DROPPED} == {
        "active",
        "done",
        "dropped",
    }


# ---------------------------------------------------------------------------
# 领域层校验（机制护栏）：status / remind_at 写入必须合法，非法 fail-fast 抛
# ValueError —— 工具层 @tool_error 把它喂回模型重填，绝不静默写脏。校验在 DB
# 读写之前（fail-fast），所以这些是纯单测、不需要真 PG。
# ---------------------------------------------------------------------------


async def test_update_entry_rejects_invalid_status():
    """bug 2 复现：status 拼错（complete 而非 done）→ 必须抛 ValueError，不静默写脏。

    现状：status 是裸 str、update_entry 原样写入。模型拼错（complete）→ 条目既不在
    active-only（不进输入）、又被 reminder gate 当非 active 丢掉 → 静默失踪、她再也
    看不到。校验在 DB 之前 fail-fast，所以本测试无需真 PG。
    """
    with pytest.raises(ValueError):
        await update_entry(
            lane="coe-t3",
            persona_id="akao",
            entry_id="e-bad-status",
            status="complete",  # 拼错：合法值是 done
        )


@pytest.mark.integration
async def test_update_entry_accepts_valid_statuses(notebook_db):
    """合法 status（done / dropped / active / None）不被校验误伤——只挡非法值。

    这条与 reject 测试成对：校验只拦真正的脏值，不能把合法的三态 / 不传也挡掉。
    None（不改状态）也必须放行。先记一条 active 日程，再用每个合法 status 改它、
    读回确认真落库（None 时状态沿用前版 active），证明合法值确实穿过校验落地。
    """
    await note_entry(
        lane="coe-t3", persona_id="akao", entry_id="ok-status",
        content="一件事", remind_at=None, noted_at="2026-06-13T12:30:00+08:00",
    )
    for ok, want in (
        (STATUS_DONE, STATUS_DONE),
        (STATUS_DROPPED, STATUS_DROPPED),
        (STATUS_ACTIVE, STATUS_ACTIVE),
        (None, STATUS_ACTIVE),  # None=不改，沿用上一版（上一版是 active）
    ):
        await update_entry(
            lane="coe-t3", persona_id="akao", entry_id="ok-status", status=ok,
        )
        latest = await select_latest(
            NotebookEntry, _latest_kv("coe-t3", "akao", "ok-status")
        )
        assert latest is not None and latest.status == want


async def test_update_entry_rejects_unparseable_remind_at():
    """bug 3 复现：remind_at 脏串（解析不出 ISO 时刻）→ 必须抛 ValueError，不静默写脏。

    现状：remind_at 写入不校验。脏串渲染侧当「还惦记 / 没到点」、调度侧
    fire_schedule_reminders 解析不了夹成 delay=0 → 立即错误提醒，两边不一致。
    校验在 DB 之前 fail-fast，所以本测试无需真 PG。
    """
    with pytest.raises(ValueError):
        await update_entry(
            lane="coe-t3",
            persona_id="akao",
            entry_id="e-bad-time",
            remind_at="下午三点",  # 不是合法 ISO 时刻
        )


async def test_note_entry_rejects_unparseable_remind_at():
    """bug 3 复现（首写路径）：note_entry 写脏 remind_at → 也必须 fail-fast 抛 ValueError。

    脏 remind_at 从首写就该挡住（note 工具带 remind_at 排日程的入口），否则脏串落库后
    渲染 / 调度两边不一致。校验在 insert 之前，所以无需真 PG。
    """
    with pytest.raises(ValueError):
        await note_entry(
            lane="coe-t3",
            persona_id="akao",
            entry_id="e-bad-note-time",
            content="排个日程",
            remind_at="明天早上",  # 脏串
            noted_at="2026-06-13T12:30:00+08:00",
        )


@pytest.mark.integration
async def test_note_entry_accepts_none_and_valid_iso_remind_at(notebook_db):
    """合法 remind_at（None 备忘 / 合法 ISO 日程）不被校验误伤——只挡脏串。

    None（备忘、不挂时间）与合法 ISO（日程）都必须放行、真落库；校验只拦解析不出的
    脏串。读回确认两者都按原值落地，证明合法值确实穿过校验。
    """
    await note_entry(
        lane="coe-t3", persona_id="akao", entry_id="ok-memo",
        content="只是备忘", remind_at=None, noted_at="2026-06-13T12:30:00+08:00",
    )
    memo = await select_latest(NotebookEntry, _latest_kv("coe-t3", "akao", "ok-memo"))
    assert memo is not None and memo.remind_at is None

    await note_entry(
        lane="coe-t3", persona_id="akao", entry_id="ok-sched",
        content="排个日程", remind_at="2026-06-13T15:00:00+08:00",
        noted_at="2026-06-13T12:30:00+08:00",
    )
    sched = await select_latest(NotebookEntry, _latest_kv("coe-t3", "akao", "ok-sched"))
    assert sched is not None and sched.remind_at == "2026-06-13T15:00:00+08:00"


# ---------------------------------------------------------------------------
# render_notebook / entry_status_label — 本子条目渲染（单一定义处，纯函数）.
#
# 第二块（进她脑子）把还活着的条目渲进她每轮唤醒输入 + chat inner_context，复用的
# 就是这一处渲染（read_notebook 工具、life 唤醒、chat 上下文三处共用）。这些是纯
# 函数（不碰 DB），按 NotebookEntry 实例直接断言渲染契约。
# ---------------------------------------------------------------------------


def _entry(entry_id, content, *, remind_at=None, status=STATUS_ACTIVE):
    return NotebookEntry(
        lane="coe-t3",
        persona_id="akao",
        entry_id=entry_id,
        content=content,
        remind_at=remind_at,
        status=status,
        noted_at="2026-06-13T10:00:00+08:00",
    )


def test_render_empty_book_gives_a_hint_not_blank():
    """空本子给一句提示（不返回空串让模型困惑、不报错）。"""
    out = render_notebook([], now="2026-06-13T12:00:00+08:00")
    assert out
    assert "空" in out


def test_render_memo_has_id_and_content_no_time():
    """备忘（无 remind_at）渲一行：带 id + 内容，不出现提醒时间。"""
    out = render_notebook(
        [_entry("e-1", "想看那部新动画")], now="2026-06-13T12:00:00+08:00"
    )
    assert "e-1" in out
    assert "想看那部新动画" in out
    assert "提醒" not in out, "备忘没时间，不该渲出提醒时间"


def test_render_schedule_shows_remind_time():
    """日程（有 remind_at）渲一行：带提醒时间。"""
    out = render_notebook(
        [_entry("e-2", "下午三点陪我妹去琴行", remind_at="2026-06-13T15:00:00+08:00")],
        now="2026-06-13T12:00:00+08:00",
    )
    assert "下午三点陪我妹去琴行" in out
    assert "2026-06-13T15:00:00+08:00" in out


def test_status_label_active_memo_is_still_holding():
    """active 备忘（无时间）状态显示「还惦记」。"""
    assert entry_status_label(_entry("e", "x"), "2026-06-13T12:00:00+08:00") == "还惦记"


def test_status_label_overdue_schedule_is_derived_due():
    """active 日程且 remind_at 已早于 now → 派生显示「到点了」（非存储态）。"""
    e = _entry("e", "x", remind_at="2026-06-13T10:00:00+08:00")
    assert entry_status_label(e, "2026-06-13T12:00:00+08:00") == "到点了"


def test_status_label_future_schedule_still_holding():
    """active 日程但 remind_at 还没到 → 仍「还惦记」（没到点不显示到点了）。"""
    e = _entry("e", "x", remind_at="2026-06-13T15:00:00+08:00")
    assert entry_status_label(e, "2026-06-13T12:00:00+08:00") == "还惦记"


def test_status_label_done_and_dropped():
    """done → 做了；dropped → 划了。"""
    assert entry_status_label(
        _entry("e", "x", status=STATUS_DONE), "2026-06-13T12:00:00+08:00"
    ) == "做了"
    assert entry_status_label(
        _entry("e", "x", status=STATUS_DROPPED), "2026-06-13T12:00:00+08:00"
    ) == "划了"


def test_status_label_unparseable_remind_at_falls_back_to_holding():
    """remind_at / now 脏串解析不出 → 退回「还惦记」（不静默猜过没过点）。"""
    e = _entry("e", "x", remind_at="不是时间")
    assert entry_status_label(e, "2026-06-13T12:00:00+08:00") == "还惦记"
