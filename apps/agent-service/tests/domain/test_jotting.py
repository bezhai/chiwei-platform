"""Jotting 持久化契约 — 随笔（随手记）+ 吸收水位窗口.

她的草稿纸：当下的念头、观察、感想，随手写一条是一条。与本子（NotebookEntry
待办）完全不同的行为面：**无三态 status、无 remind_at、无编辑、无版本链**——写了
就写了，纯 append。生命周期到当天日结为止：睡前回顾把窗口内随笔吸收进日页后
**翻页**（推进吸收水位），之后翻不到、也不重复吸收；数据留在表里不物理删。

窗口水位语义（Task 1 钉死，Task 3 只调接口不自行发明判据）：

  * **显式吸收水位**：独立的 ``JottingWatermark`` 版本链（Key 带 lane + persona），
    不从 day page 存在性 / 时间隐式推导。
  * **复合游标 ``(created_at, jot_id)``**：窗口读只取水位之后的行（按 framework
    落库时刻 ``created_at`` 单调序 + ``jot_id`` tie-breaker，同 acts.py 的 pull
    游标命门——不用 noted_at，主观时刻与落库顺序可乱序、会漏）。
  * **翻页幂等 + 水位单调不回退**：翻到窗口读给出的游标；重复翻 / 拿旧游标翻
    （失败重试、sweep 补跑竞态）都是 no-op，不 append 冗余版本、绝不回退。
  * **review 中途新写的随笔不被静默吸收**：翻页只翻到"读窗口那一刻"的游标，
    之后写入的随笔留在窗口里等下一次日结。

集成测试（真实 Postgres）：正确性故事是首写幂等 + 游标过滤 + 翻页幂等/单调 +
lane / persona 隔离——全在 SQL 行为里，mock pg 等于什么都没测。
"""

from __future__ import annotations

import pytest

from app.domain.jotting import (
    Jotting,
    JottingWatermark,
    count_unabsorbed_jottings,
    jot_down,
    read_unabsorbed_jottings,
    render_jottings,
    turn_jotting_page,
)
from app.runtime.persist import select_all_versions
from tests.runtime.conftest import migrate


@pytest.fixture
async def jotting_db(test_db):
    """Build the Jotting + JottingWatermark tables on the test db."""
    await migrate(Jotting, test_db)
    await migrate(JottingWatermark, test_db)
    yield test_db


async def _jot(lane, persona_id, jot_id, content, noted_at="2026-07-06T12:30:00+08:00"):
    await jot_down(
        lane=lane,
        persona_id=persona_id,
        jot_id=jot_id,
        content=content,
        noted_at=noted_at,
    )


# ---------------------------------------------------------------------------
# 记一条（幂等首写）+ 窗口读基本面
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_jot_down_then_window_read_in_order(jotting_db):
    """记两条 → 窗口读按落库先后返回，内容 / noted_at 原样读回，游标非空。"""
    await _jot("coe-t5", "akao", "j-1", "手机还在转圈，怎么回事", "2026-07-06T12:30:00+08:00")
    await _jot("coe-t5", "akao", "j-2", "窗外的云像一条鱼", "2026-07-06T12:31:00+08:00")

    window = await read_unabsorbed_jottings(lane="coe-t5", persona_id="akao")

    assert [j.jot_id for j in window.jottings] == ["j-1", "j-2"]
    assert window.jottings[0].content == "手机还在转圈，怎么回事"
    assert window.jottings[0].noted_at == "2026-07-06T12:30:00+08:00"
    assert window.jottings[1].content == "窗外的云像一条鱼"
    assert window.cursor is not None, "非空窗口必须给出翻页游标"


@pytest.mark.integration
async def test_jot_down_same_id_is_idempotent(jotting_db):
    """「记一条」幂等命门：同 (lane, persona, jot_id) 再写一次 → 只落一条。

    整轮重试 / durable 重投用同一派生 jot_id 再记一次——insert_idempotent 按键
    去重、ON CONFLICT DO NOTHING，窗口里不出现第二条。
    """
    for _ in range(2):
        await _jot("coe-t5", "akao", "j-dup", "同一个念头")

    window = await read_unabsorbed_jottings(lane="coe-t5", persona_id="akao")
    dup = [j for j in window.jottings if j.jot_id == "j-dup"]
    assert len(dup) == 1, f"同 jot_id 重记应只落一条，实得 {len(dup)} 条"


@pytest.mark.integration
async def test_window_cold_start_is_empty_with_none_cursor(jotting_db):
    """冷启动（没记过、没翻过）：窗口空列表、游标 None、计数 0。"""
    window = await read_unabsorbed_jottings(lane="coe-t5", persona_id="akao")
    assert window.jottings == []
    assert window.cursor is None
    assert await count_unabsorbed_jottings(lane="coe-t5", persona_id="akao") == 0


# ---------------------------------------------------------------------------
# 窗口翻页：吸收后翻不到、计数按水位、翻页幂等且水位单调不回退
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_turn_page_absorbs_window(jotting_db):
    """翻页后窗口清空、计数归零；之后新写的随笔重新进窗口（草稿纸翻到新一页）。"""
    await _jot("coe-t5", "akao", "j-1", "上午的观察")
    await _jot("coe-t5", "akao", "j-2", "中午的感想")
    window = await read_unabsorbed_jottings(lane="coe-t5", persona_id="akao")

    await turn_jotting_page(
        lane="coe-t5", persona_id="akao",
        cursor=window.cursor, turned_at="2026-07-07T00:10:00+08:00",
    )

    after = await read_unabsorbed_jottings(lane="coe-t5", persona_id="akao")
    assert after.jottings == [], "翻页后窗口内不该再有已吸收的随笔"
    assert after.cursor is None
    assert await count_unabsorbed_jottings(lane="coe-t5", persona_id="akao") == 0

    await _jot("coe-t5", "akao", "j-3", "新一天的念头")
    fresh = await read_unabsorbed_jottings(lane="coe-t5", persona_id="akao")
    assert [j.jot_id for j in fresh.jottings] == ["j-3"]
    assert await count_unabsorbed_jottings(lane="coe-t5", persona_id="akao") == 1


@pytest.mark.integration
async def test_jots_written_after_window_read_survive_turn(jotting_db):
    """命门：review 中途新写的随笔不被静默吸收。

    读窗口拿到游标后又写了一条 → 用旧游标翻页 → 新写的那条仍在窗口里（它没被
    这次日页看到过，必须留给下一次日结，不丢证据）。
    """
    await _jot("coe-t5", "akao", "j-1", "review 前的随笔")
    window = await read_unabsorbed_jottings(lane="coe-t5", persona_id="akao")
    await _jot("coe-t5", "akao", "j-2", "review 中途冒出的念头")

    await turn_jotting_page(
        lane="coe-t5", persona_id="akao",
        cursor=window.cursor, turned_at="2026-07-07T00:10:00+08:00",
    )

    after = await read_unabsorbed_jottings(lane="coe-t5", persona_id="akao")
    assert [j.jot_id for j in after.jottings] == ["j-2"]


@pytest.mark.integration
async def test_turn_page_repeat_same_cursor_is_noop(jotting_db):
    """翻页幂等：同一游标翻两次 → 第二次 no-op，不 append 冗余水位版本。"""
    await _jot("coe-t5", "akao", "j-1", "一条随笔")
    window = await read_unabsorbed_jottings(lane="coe-t5", persona_id="akao")

    for _ in range(2):
        await turn_jotting_page(
            lane="coe-t5", persona_id="akao",
            cursor=window.cursor, turned_at="2026-07-07T00:10:00+08:00",
        )

    versions = await select_all_versions(
        JottingWatermark, {"lane": "coe-t5", "persona_id": "akao"}
    )
    assert len(versions) == 1, "重复翻同一游标不该 append 第二版水位"
    after = await read_unabsorbed_jottings(lane="coe-t5", persona_id="akao")
    assert after.jottings == []


@pytest.mark.integration
async def test_turn_page_never_regresses(jotting_db):
    """水位单调不回退：先翻到新游标，再拿旧游标翻（迟到的重试）→ no-op。

    回退会让已吸收的随笔重新入窗、次日日页重复吸收——旧游标必须被单调守卫挡住，
    且不 append 冗余版本。
    """
    await _jot("coe-t5", "akao", "j-1", "早先的随笔")
    early = await read_unabsorbed_jottings(lane="coe-t5", persona_id="akao")
    await _jot("coe-t5", "akao", "j-2", "后来的随笔")
    late = await read_unabsorbed_jottings(lane="coe-t5", persona_id="akao")

    await turn_jotting_page(
        lane="coe-t5", persona_id="akao",
        cursor=late.cursor, turned_at="2026-07-07T00:10:00+08:00",
    )
    # 迟到的重试拿着旧游标再翻一次
    await turn_jotting_page(
        lane="coe-t5", persona_id="akao",
        cursor=early.cursor, turned_at="2026-07-07T00:11:00+08:00",
    )

    versions = await select_all_versions(
        JottingWatermark, {"lane": "coe-t5", "persona_id": "akao"}
    )
    assert len(versions) == 1, "旧游标翻页应被单调守卫挡住、不 append 版本"
    after = await read_unabsorbed_jottings(lane="coe-t5", persona_id="akao")
    assert after.jottings == [], "水位回退会让已吸收随笔复活——必须仍为空窗口"


@pytest.mark.integration
async def test_turn_page_none_cursor_is_noop(jotting_db):
    """空窗口的游标是 None → 翻页 no-op（不落任何水位行、不抛）。"""
    window = await read_unabsorbed_jottings(lane="coe-t5", persona_id="akao")
    assert window.cursor is None

    await turn_jotting_page(
        lane="coe-t5", persona_id="akao",
        cursor=window.cursor, turned_at="2026-07-07T00:10:00+08:00",
    )

    versions = await select_all_versions(
        JottingWatermark, {"lane": "coe-t5", "persona_id": "akao"}
    )
    assert versions == []


# ---------------------------------------------------------------------------
# lane / persona 隔离（泳道隔离命门 + 每人一张草稿纸）
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_lane_isolation_on_jottings_and_watermark(jotting_db):
    """lane 隔离命门：coe 的窗口读不到 prod 的随笔；coe 翻页绝不吸收 prod 的窗口。"""
    await _jot("prod", "akao", "same-id", "prod 的随笔")
    await _jot("coe-t5", "akao", "same-id", "coe 的随笔")

    coe = await read_unabsorbed_jottings(lane="coe-t5", persona_id="akao")
    assert [j.content for j in coe.jottings] == ["coe 的随笔"]

    await turn_jotting_page(
        lane="coe-t5", persona_id="akao",
        cursor=coe.cursor, turned_at="2026-07-07T00:10:00+08:00",
    )

    prod = await read_unabsorbed_jottings(lane="prod", persona_id="akao")
    assert [j.content for j in prod.jottings] == ["prod 的随笔"], (
        "coe 翻页绝不能把 prod 的窗口翻掉"
    )


@pytest.mark.integration
async def test_persona_isolation_on_jottings_and_watermark(jotting_db):
    """每人一张草稿纸：窗口 / 计数 / 翻页都按 persona 隔离。"""
    await _jot("coe-t5", "akao", "j-a", "赤尾的随笔")
    await _jot("coe-t5", "chinagi", "j-c", "姐姐的随笔")

    akao = await read_unabsorbed_jottings(lane="coe-t5", persona_id="akao")
    assert [j.content for j in akao.jottings] == ["赤尾的随笔"]

    await turn_jotting_page(
        lane="coe-t5", persona_id="akao",
        cursor=akao.cursor, turned_at="2026-07-07T00:10:00+08:00",
    )

    chinagi = await read_unabsorbed_jottings(lane="coe-t5", persona_id="chinagi")
    assert [j.content for j in chinagi.jottings] == ["姐姐的随笔"]
    assert await count_unabsorbed_jottings(lane="coe-t5", persona_id="chinagi") == 1


# ---------------------------------------------------------------------------
# 支撑索引 — 窗口读 / 计数的查询形态必须有索引兜着（codex T3 必改）
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_jotting_table_has_window_scan_index(jotting_db):
    """建表后 data_jotting 必须带 (lane, persona_id, created_at, jot_id) 复合索引。

    count 每个 life 轮都调（stimulus 存在提示行）、窗口读按水位取增量，而随笔
    全历史只增不删——没有支撑索引这两条查询会随历史线性退化，违反 spec「窗口
    读 / 计数不扫全历史」。自动迁移默认只建 dedup_hash 唯一索引，必须由
    Meta.indexes 声明补上。断言真实 pg_indexes 里的索引定义（列全、序对）。
    """
    from sqlalchemy import text

    from app.data.session import get_session

    async with get_session() as s:
        r = await s.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = 'data_jotting'"
            )
        )
        indexdefs = [row[0] for row in r.fetchall()]
    assert any(
        "(lane, persona_id, created_at, jot_id)" in d for d in indexdefs
    ), f"缺窗口查询支撑索引，现有索引: {indexdefs}"


# ---------------------------------------------------------------------------
# render_jottings — 随笔渲染（单一定义处，纯函数）：life 翻随笔工具与 review
# 证据段共用这一份渲染。
# ---------------------------------------------------------------------------


def _jotting(jot_id, content, noted_at="2026-07-06T12:30:00+08:00"):
    return Jotting(
        lane="coe-t5",
        persona_id="akao",
        jot_id=jot_id,
        content=content,
        noted_at=noted_at,
    )


def test_render_empty_gives_a_hint_not_blank():
    """空窗口给一句提示（不返回空串让模型困惑、不报错）。"""
    out = render_jottings([])
    assert out
    assert "没有" in out


def test_render_lines_carry_content_and_noted_at():
    """每条一行：带内容 + 她写下的时刻（无 id、无状态——随笔没有那些）。"""
    out = render_jottings(
        [
            _jotting("j-1", "手机还在转圈", "2026-07-06T12:30:00+08:00"),
            _jotting("j-2", "云像一条鱼", "2026-07-06T13:00:00+08:00"),
        ]
    )
    assert "手机还在转圈" in out
    assert "2026-07-06T12:30:00+08:00" in out
    assert "云像一条鱼" in out
    assert len(out.splitlines()) == 2
