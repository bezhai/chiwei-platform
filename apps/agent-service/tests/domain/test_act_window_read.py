"""睡前回顾的 act 证据查询 — 按 (lane, persona, 时间窗) 只读她这个生活日做过的事.

回顾本体的证据之一是「这个生活日窗口内她的 act」。既有的 ``list_recent_acts``
是 world 的复合游标 pull（按 created_at 落库序、全 persona），不是「某 persona
某窗口」的语义，所以加一个独立只读查询 ``list_persona_acts_between``：

  * 按 ``occurred_at``（她做事的时刻）过滤——回顾看的是"这一天她经历了什么"，
    用做事时刻、不用落库时刻（与 world 游标的语义相反、互不混用）。
  * 窗口闭区间 ``[start, end]``，两端 ISO 文本在 SQL 侧 cast 成 timestamptz 比较
    （occurred_at 历史上有 CST / UTC 两种 aware ISO，cast 后同一真实时刻口径）。
  * 只读她自己的（persona 过滤）+ lane 隔离。
  * 按 occurred_at 升序（一天的事按先后讲）。

集成测试（真 Postgres）：正确性全在 SQL 过滤 / 排序，mock 测不到。
"""

from __future__ import annotations

import pytest

from app.data.queries.acts import list_persona_acts_between
from app.domain.world_events import ActPerformed
from app.runtime.persist import insert_idempotent
from tests.runtime.conftest import migrate


@pytest.fixture
async def act_db(test_db):
    await migrate(ActPerformed, test_db)
    yield test_db


async def _seed(lane, act_id, persona_id, description, occurred_at):
    await insert_idempotent(
        ActPerformed(
            lane=lane,
            act_id=act_id,
            persona_id=persona_id,
            description=description,
            occurred_at=occurred_at,
        )
    )


@pytest.mark.integration
async def test_window_filters_by_occurred_at_inclusive(act_db):
    """窗口闭区间 [start, end] 按 occurred_at 过滤：窗口外的不读、边界上的读到。"""
    await _seed("coe-t2", "before", "akao", "窗口前做的事", "2026-06-10T03:50:00+08:00")
    await _seed("coe-t2", "edge-start", "akao", "正好四点做的事", "2026-06-10T04:00:00+08:00")
    await _seed("coe-t2", "mid", "akao", "白天做的事", "2026-06-10T12:00:00+08:00")
    await _seed("coe-t2", "edge-end", "akao", "入睡那刻做的事", "2026-06-10T23:30:00+08:00")
    await _seed("coe-t2", "after", "akao", "窗口后做的事", "2026-06-10T23:31:00+08:00")

    got = await list_persona_acts_between(
        lane="coe-t2",
        persona_id="akao",
        start_iso="2026-06-10T04:00:00+08:00",
        end_iso="2026-06-10T23:30:00+08:00",
    )

    assert [a.act_id for a in got] == ["edge-start", "mid", "edge-end"]


@pytest.mark.integration
async def test_window_orders_by_occurred_at_ascending(act_db):
    """一天的事按发生先后升序（落库顺序无关——回顾讲的是经历顺序）。"""
    # 故意倒序落库
    await _seed("coe-t2", "evening", "akao", "晚上的事", "2026-06-10T21:00:00+08:00")
    await _seed("coe-t2", "morning", "akao", "早上的事", "2026-06-10T08:00:00+08:00")

    got = await list_persona_acts_between(
        lane="coe-t2",
        persona_id="akao",
        start_iso="2026-06-10T04:00:00+08:00",
        end_iso="2026-06-10T23:30:00+08:00",
    )

    assert [a.act_id for a in got] == ["morning", "evening"]


@pytest.mark.integration
async def test_window_spans_natural_days_and_mixed_tz(act_db):
    """熬夜窗口跨自然日 + occurred_at 历史 UTC 格式：按真实时刻比较都读得到。

    01:30 CST == 前一日 17:30 UTC——历史 life 写过 UTC aware ISO，cast 成
    timestamptz 后同一真实时刻口径，不漏。
    """
    await _seed("coe-t2", "day", "akao", "白天的事", "2026-06-10T12:00:00+08:00")
    # 06-11 01:00 CST，但写成 UTC 形式（06-10 17:00Z）
    await _seed("coe-t2", "late-utc", "akao", "熬夜做的事", "2026-06-10T17:00:00+00:00")

    got = await list_persona_acts_between(
        lane="coe-t2",
        persona_id="akao",
        start_iso="2026-06-10T04:00:00+08:00",
        end_iso="2026-06-11T01:30:00+08:00",
    )

    assert [a.act_id for a in got] == ["day", "late-utc"]


@pytest.mark.integration
async def test_persona_and_lane_isolation(act_db):
    """只读她自己的 act（persona 过滤）+ lane 隔离（coe 不读 prod）。"""
    await _seed("coe-t2", "hers", "akao", "她做的事", "2026-06-10T12:00:00+08:00")
    await _seed("coe-t2", "sisters", "chinagi", "姐姐做的事", "2026-06-10T12:00:00+08:00")
    await _seed("prod", "prod-act", "akao", "prod 里做的事", "2026-06-10T12:00:00+08:00")

    got = await list_persona_acts_between(
        lane="coe-t2",
        persona_id="akao",
        start_iso="2026-06-10T04:00:00+08:00",
        end_iso="2026-06-10T23:30:00+08:00",
    )

    assert [a.act_id for a in got] == ["hers"]


@pytest.mark.integration
async def test_empty_window_returns_empty(act_db):
    """窗口内没做过事返回空列表（证据段如实说"这天没做什么"由上层负责）。"""
    got = await list_persona_acts_between(
        lane="coe-t2",
        persona_id="akao",
        start_iso="2026-06-10T04:00:00+08:00",
        end_iso="2026-06-10T23:30:00+08:00",
    )
    assert got == []
