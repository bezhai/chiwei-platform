"""Durable act 读取查询契约 — world 醒来按复合游标批量 pull act.

新范式（pull）：act 不再唤醒 world。life 做完一件事直接 ``insert_idempotent``
落 ``data_act_performed``，world 按自己 sleep 的节奏醒来时从"上次消费游标之后"
批量读这段时间攒下的 act 一并推演，推完把游标推进到本批末尾。:func:`list_recent_acts`
是读侧底座：按复合游标 ``(created_at, act_id)`` 过滤、一轮最多读 N 条（按落库
顺序取最早的、防单轮 context 爆炸），剩下的下轮接着读。

为什么游标用 ``created_at`` 而不是 ``occurred_at``：``occurred_at`` 是 life 在轮次
开始就固定的"做事时刻"，act 工具稍后才落库——跨 persona 并发时 occurred_at 顺序
≠ 落库顺序。若按 occurred_at 推进游标，会先消费"晚发生但早落库"的 act 把游标推过
去，之后落库的"早发生" act 永远读不到（漏 act）。``created_at`` 是 framework 给每行
自动加的 ``TIMESTAMPTZ DEFAULT now()``、单调落库时刻——按它推进游标不会漏。

为什么是复合游标而不是只用 created_at：同一落库瞬间理论上可能多条 act，``>`` 会漏掉
边界同刻的新行、``>=`` 会重读边界旧行。加 ``act_id`` 作稳定 tie-breaker，读取条件
是 ``created_at > 游标 OR (created_at = 游标 AND act_id > 游标act_id)``。游标为 None
（冷启动、从没消费过）时读全既有 act。

这些是 integration 测试（真 Postgres）——正确性全在"durable 行怎么落、复合游标
查询怎么筛"，mock pg 测不到任何东西。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.data.queries.acts import list_recent_acts
from app.data.session import get_session
from app.domain.world_events import ActPerformed
from app.runtime.migrator import _table_name
from app.runtime.persist import insert_idempotent
from tests.runtime.conftest import migrate

_ACT_TABLE = _table_name(ActPerformed)


@pytest.fixture
async def act_db(test_db):
    """Build the durable act table on the test db."""
    await migrate(ActPerformed, test_db)
    yield test_db


async def _seed(lane: str, act_id: str, persona_id: str, description: str, occurred_at: str):
    # 生产里 life 做完一件事 → ``perform_act`` → ``insert_idempotent`` 直接落表
    # （pull 范式：不 emit、不唤醒）。测试用同一条持久化原语种数据，对齐生产真实落盘。
    # created_at 由 framework 的 ``DEFAULT now()`` 自动写，落库顺序即调用顺序。
    await insert_idempotent(
        ActPerformed(
            lane=lane,
            act_id=act_id,
            persona_id=persona_id,
            description=description,
            occurred_at=occurred_at,
        )
    )


async def _force_created_at(lane: str, act_id: str, created_at: str):
    """直接改某行的 ``created_at`` 列到指定时刻，模拟"先发生但晚落库"的乱序。

    生产里 created_at 随真实落库时刻单调；测试要构造 occurred_at 与 created_at 顺序
    相反的乱序场景，得绕过 DEFAULT now() 手动钉死 created_at。
    """
    async with get_session() as s:
        await s.execute(
            text(
                f"UPDATE {_ACT_TABLE} SET created_at = (:c)::text::timestamptz "
                f"WHERE lane = :lane AND act_id = :act_id"
            ),
            {"c": created_at, "lane": lane, "act_id": act_id},
        )
        await s.commit()


@pytest.mark.integration
async def test_cold_start_cursor_none_reads_all_acts(act_db):
    """游标为 None（冷启动 / 从没消费过）→ 读全既有 act，按落库顺序升序。"""
    await _seed("coe-t1", "a1", "chinagi", "我起床去厨房煮咖啡", "2026-06-04T08:00:00+08:00")
    await _seed("coe-t1", "a2", "ayana", "我出门上学", "2026-06-04T08:00:20+08:00")
    await _seed("coe-t1", "a3", "akao", "我去找千凪", "2026-06-04T08:00:40+08:00")

    got = await list_recent_acts(
        lane="coe-t1", cursor_created_at=None, cursor_act_id=None, limit=10
    )

    descriptions = [a.description for a, _created_at in got]
    assert descriptions == ["我起床去厨房煮咖啡", "我出门上学", "我去找千凪"], (
        f"游标为 None 时读全既有 act（按 created_at 升序），实际 {descriptions}"
    )


@pytest.mark.integration
async def test_returns_created_at_alongside_each_row(act_db):
    """读回每条 act 时一并带它的 ``created_at``（engine 用它算游标终点 / 起点）。

    ``created_at`` 不在 ``ActPerformed.model_fields`` 里（是 runtime 列），所以必须
    从 SELECT 行单独取出、与 ActPerformed 一并返回。
    """
    await _seed("coe-t1", "a1", "chinagi", "我去厨房做饭", "2026-06-04T08:00:10+08:00")

    got = await list_recent_acts(
        lane="coe-t1", cursor_created_at=None, cursor_act_id=None, limit=10
    )

    assert len(got) == 1
    act, created_at = got[0]
    assert isinstance(act, ActPerformed)
    assert act.act_id == "a1"
    assert isinstance(created_at, str) and created_at, (
        "每条 act 必须带它的 created_at 字符串（游标推进 / 起点比对靠它）"
    )


@pytest.mark.integration
async def test_cursor_excludes_already_consumed_acts(act_db):
    """游标之后才读：游标落库时刻之前 / 同刻且 act_id ≤ 游标 的旧 act 不重读。"""
    await _seed("coe-t1", "old", "chinagi", "上一批读过的事", "2026-06-04T08:00:00+08:00")
    await _seed("coe-t1", "new", "akao", "刚做的新事", "2026-06-04T08:00:30+08:00")

    # 先读出 old 的真实 created_at 作游标，只读它之后的。
    first = await list_recent_acts(
        lane="coe-t1", cursor_created_at=None, cursor_act_id=None, limit=10
    )
    old_act, old_created_at = first[0]
    assert old_act.act_id == "old"

    got = await list_recent_acts(
        lane="coe-t1",
        cursor_created_at=old_created_at,
        cursor_act_id="old",
        limit=10,
    )

    assert [a.description for a, _c in got] == ["刚做的新事"], (
        "游标之前 / 同刻已消费的 act 不该重读"
    )


@pytest.mark.integration
async def test_same_instant_act_id_tiebreak_no_dup_no_miss(act_db):
    """同一落库瞬间多条 act：用 act_id tie-break，游标行不重读、同刻更大的 act_id 读到。

    三条 act 钉成同一 ``created_at``。游标停在 ``(t, "b")``：``"a"``（≤ 游标）不读，
    ``"c"``（同刻但 act_id > 游标）读到。只用 created_at 的 ``>`` 会漏掉 c、``>=``
    会重读 a/b——复合游标两头都不错。
    """
    await _seed("coe-t1", "a", "chinagi", "同刻动作 a", "2026-06-04T08:00:00+08:00")
    await _seed("coe-t1", "b", "ayana", "同刻动作 b", "2026-06-04T08:00:01+08:00")
    await _seed("coe-t1", "c", "akao", "同刻动作 c", "2026-06-04T08:00:02+08:00")
    # 钉死三条同一 created_at，制造"同落库瞬间"。
    t = "2026-06-04T08:00:00+08:00"
    for act_id in ("a", "b", "c"):
        await _force_created_at("coe-t1", act_id, t)

    got = await list_recent_acts(
        lane="coe-t1", cursor_created_at=t, cursor_act_id="b", limit=10
    )

    descriptions = [a.description for a, _c in got]
    assert descriptions == ["同刻动作 c"], (
        f"同刻：游标行及之前不重读、同刻更大 act_id 读到，实际 {descriptions}"
    )


@pytest.mark.integration
async def test_limit_reads_only_earliest_n(act_db):
    """读取上限 N：积压超 N 条只读最早落库的 N 条，剩下下轮接着读、不丢。"""
    for i in range(5):
        await _seed(
            "coe-t1", f"a{i}", "akao", f"动作{i}",
            f"2026-06-04T08:0{i}:00+08:00",
        )

    first = await list_recent_acts(
        lane="coe-t1", cursor_created_at=None, cursor_act_id=None, limit=3
    )
    assert [a.description for a, _c in first] == ["动作0", "动作1", "动作2"], (
        "只读最早落库的 N=3 条"
    )

    # 下一轮从上一批末尾接着读，剩下两条读到。
    last_act, last_created_at = first[-1]
    second = await list_recent_acts(
        lane="coe-t1",
        cursor_created_at=last_created_at,
        cursor_act_id=last_act.act_id,
        limit=3,
    )
    assert [a.description for a, _c in second] == ["动作3", "动作4"], (
        "剩下的 act 下轮从游标接着读、不丢"
    )


@pytest.mark.integration
async def test_out_of_order_occurred_at_not_missed(act_db):
    """乱序命门：occurred_at 早但 created_at 晚的 act 不被漏读（必改 1 的核心回归）。

    场景还原 codex 指出的 out-of-order 漏读：life 轮首固定 occurred_at、稍后才落库，
    跨 persona 并发时会出现"晚发生但早落库"和"早发生但晚落库"。

      - act ``late_occ``：occurred_at 晚（08:05），但**先**落库（created_at 08:00）。
      - act ``early_occ``：occurred_at 早（08:01），但**后**落库（created_at 08:10）。

    world 先消费 ``late_occ``、游标推进到它。若游标按 occurred_at（08:05）推进，
    early_occ（occurred_at 08:01 < 08:05）会被永远滤掉——漏 act。按 created_at
    （08:00）推进则 early_occ（created_at 08:10 > 08:00）下轮读得到——不漏。
    """
    # late_occ 先落库（created_at 08:00），occurred_at 晚。
    await _seed("coe-t1", "late_occ", "ayana", "晚发生但先落库", "2026-06-04T08:05:00+08:00")
    await _force_created_at("coe-t1", "late_occ", "2026-06-04T08:00:00+08:00")
    # early_occ 后落库（created_at 08:10），occurred_at 早。
    await _seed("coe-t1", "early_occ", "akao", "早发生但晚落库", "2026-06-04T08:01:00+08:00")
    await _force_created_at("coe-t1", "early_occ", "2026-06-04T08:10:00+08:00")

    # 第一批：world 醒来读全（冷启动），按 created_at 升序只读到先落库的 late_occ
    # （limit=1 模拟它先单独被消费、游标推进到它）。
    first = await list_recent_acts(
        lane="coe-t1", cursor_created_at=None, cursor_act_id=None, limit=1
    )
    assert [a.act_id for a, _c in first] == ["late_occ"], (
        "按 created_at 升序，先落库的 late_occ 先被读到"
    )

    # 游标推进到 late_occ 的 created_at（08:00）。
    _act, late_created_at = first[0]

    # 第二批：从 late_occ 之后读。early_occ 的 occurred_at（08:01）早于 late_occ 的
    # occurred_at（08:05），若按 occurred_at 推进游标它会被滤掉漏读；按 created_at
    # （08:00）推进，early_occ（created_at 08:10）读得到。
    second = await list_recent_acts(
        lane="coe-t1",
        cursor_created_at=late_created_at,
        cursor_act_id="late_occ",
        limit=10,
    )
    assert [a.act_id for a, _c in second] == ["early_occ"], (
        "occurred_at 早但 created_at 晚的 act 必须下轮读得到（游标按 created_at 不漏）"
    )


@pytest.mark.integration
async def test_lane_isolation_on_acts(act_db):
    """lane 隔离：coe 的 act 不会被 prod 的 world 读到，反之亦然。"""
    await _seed("prod", "p1", "akao", "prod 动作", "2026-06-04T08:00:10+08:00")
    await _seed("coe-t1", "c1", "akao", "coe 动作", "2026-06-04T08:00:10+08:00")

    prod = await list_recent_acts(
        lane="prod", cursor_created_at=None, cursor_act_id=None, limit=10
    )
    coe = await list_recent_acts(
        lane="coe-t1", cursor_created_at=None, cursor_act_id=None, limit=10
    )

    assert [a.description for a, _c in prod] == ["prod 动作"]
    assert [a.description for a, _c in coe] == ["coe 动作"]


@pytest.mark.integration
async def test_empty_window_returns_empty(act_db):
    """游标后没有 act 返回空（不报错）。"""
    await _seed("coe-t1", "a1", "akao", "唯一一条", "2026-06-04T08:00:00+08:00")
    first = await list_recent_acts(
        lane="coe-t1", cursor_created_at=None, cursor_act_id=None, limit=10
    )
    _act, created_at = first[0]

    got = await list_recent_acts(
        lane="coe-t1",
        cursor_created_at=created_at,
        cursor_act_id="a1",
        limit=10,
    )
    assert got == []


@pytest.mark.integration
async def test_returns_full_act_rows(act_db):
    """读回的是完整 ``ActPerformed`` 行（persona / description / act_id 都在，供 world 推演）。"""
    await _seed("coe-t1", "a1", "chinagi", "我去厨房做饭", "2026-06-04T08:00:10+08:00")

    got = await list_recent_acts(
        lane="coe-t1", cursor_created_at=None, cursor_act_id=None, limit=10
    )

    assert len(got) == 1
    row, _created_at = got[0]
    assert isinstance(row, ActPerformed)
    assert row.act_id == "a1"
    assert row.persona_id == "chinagi"
    assert row.description == "我去厨房做饭"
    assert row.lane == "coe-t1"
