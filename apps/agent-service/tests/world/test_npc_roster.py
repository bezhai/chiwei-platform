"""NPC 名册（NPCRoster）— 世界里有名有姓的固定人物（NPC 层第一刀）.

world 推演时除了三姐妹和用户，还能看到一份「世界的固定人物」名册——7 个 NPC
（绫奈 3 / 赤尾 2 / 千凪 2），每个 NPC 一行。照 WorldArc / RelationshipPage 模板
（lane keyed + 多 Key + Version、append-only、读最新一版、整篇重写——演化层更新某个
NPC 直接 append 那一行的新版）。

钉死的语义（docstring 层契约，本文件断言数据层行为）：

  * 自然键 ``(lane, npc_name)``：泳道隔离（coe / ppe 绝不能覆盖 prod 的名册）+
    每个 NPC 一条链。``npc_name`` 是 NPC 的稳定标识（名字），跟关系页 other_user_id
    的 ``npc:xxx`` 约定对齐（本刀只落名字，event / 关系页是后面的刀）。
  * ``relates_to`` 是该 NPC 主要关联哪个姐妹的 persona_id（akao / chinagi / ayana），
    world 据此把名册归到对应姐妹。
  * ``sketch`` 是性格底色 + 会冒什么事的自然语言速写（不拆结构化字段）。
  * ``version`` 让演化层更新某个 NPC 时 append 新版、读最新一版（与 sibling 同模板）。
  * seed：7 个种子 NPC 一次性灌库，逐 NPC 幂等（CAS expected_current_ver=0，
    重跑无害、并发双跑也只落一行），照 :func:`seed_persona_chain` 的先例。

持久化用真实 Postgres（testcontainers）——版本链 / 多 Key / 隔离的正确性全在
"能不能 append 进去、能不能按 (lane, name) 读回、list 一个 lane 全部 NPC、seed
能否端到端落库"，mock pg 等于什么都没测。
"""

from __future__ import annotations

import pytest

from app.world.npc_roster import (
    NPC_SEEDS,
    NPCRoster,
    list_npc_roster,
    read_npc,
    seed_npc_roster,
    write_npc,
)
from tests.runtime.conftest import migrate


@pytest.fixture
async def roster_db(test_db):
    await migrate(NPCRoster, test_db)
    yield test_db


# ---------------------------------------------------------------------------
# Data 骨架（泳道隔离 + 自然键 + 不撞框架保留列）
# ---------------------------------------------------------------------------


def test_npc_roster_key_is_lane_and_name():
    """名册自然键 = (lane, npc_name)：泳道隔离 + 每个 NPC 一条链。"""
    from app.runtime.data import key_fields

    assert set(key_fields(NPCRoster)) == {"lane", "npc_name"}


def test_npc_roster_has_version_for_evolution():
    """名册带 Version：演化层更新某个 NPC 时 append 新版、读最新一版。"""
    from app.runtime.data import version_field

    assert version_field(NPCRoster) == "version"


def test_npc_roster_fields_avoid_framework_reserved_columns():
    """字段名不撞框架保留列（id / created_at / updated_at / dedup_hash）。

    三步检查第①步：字段名绝不撞 migrator 保留列（否则建表 / migrate 会冲突）。
    """
    reserved = {"id", "created_at", "updated_at", "dedup_hash"}
    assert not reserved & set(NPCRoster.model_fields)
    # 业务字段齐全（relates_to 归类 + sketch 速写）。
    assert "relates_to" in NPCRoster.model_fields
    assert "sketch" in NPCRoster.model_fields


def test_npc_roster_table_name_is_data_npc_roster():
    """表名 = data_npc_roster（framework 命名约定：data_ + snake_case 类名）。"""
    from app.runtime.migrator import _table_name

    assert _table_name(NPCRoster) == "data_npc_roster"


# ---------------------------------------------------------------------------
# 种子名册：7 个 NPC（绫奈 3 / 赤尾 2 / 千凪 2），按所属姐妹归类
# ---------------------------------------------------------------------------


def test_npc_seeds_count_and_distribution():
    """7 个种子：绫奈(ayana) 3 个、赤尾(akao) 2 个、千凪(chinagi) 2 个。"""
    assert len(NPC_SEEDS) == 7
    by_sister: dict[str, int] = {}
    for s in NPC_SEEDS:
        by_sister[s.relates_to] = by_sister.get(s.relates_to, 0) + 1
    assert by_sister == {"ayana": 3, "akao": 2, "chinagi": 2}


def test_npc_seeds_relates_to_only_three_sisters():
    """relates_to 取值只能是三姐妹之一（akao / chinagi / ayana）。"""
    assert {s.relates_to for s in NPC_SEEDS} <= {"akao", "chinagi", "ayana"}


def test_npc_seeds_names_unique_and_expected():
    """7 个种子名字唯一、且就是 spec 钉死的 7 个。"""
    names = [s.npc_name for s in NPC_SEEDS]
    assert len(set(names)) == 7
    assert set(names) == {
        "林小满",
        "顾舟",
        "沈乐",
        "陈鹿",
        "池夏",
        "许念",
        "苏晴",
    }


def test_npc_seeds_have_nonempty_sketch():
    """每个种子都有非空速写（性格底色 + 会冒什么事）。"""
    for s in NPC_SEEDS:
        assert s.sketch.strip(), f"{s.npc_name} 必须有非空速写"


# ---------------------------------------------------------------------------
# 真 PG 端到端（写 / 读 / list / 版本链 / 隔离）— 三步检查第③步
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_write_then_read_npc(roster_db):
    """写一个 NPC → 按 (lane, npc_name) 读回最新（端到端 schema 能建、能读写）。"""
    await write_npc(
        lane="coe-t1",
        npc_name="林小满",
        relates_to="ayana",
        sketch="同桌兼死党，会照顾爱慌的绫奈。",
    )

    npc = await read_npc(lane="coe-t1", npc_name="林小满")
    assert npc is not None
    assert npc.relates_to == "ayana"
    assert "同桌" in npc.sketch


@pytest.mark.integration
async def test_read_npc_cold_start_returns_none(roster_db):
    """没写过的 NPC 读回 None（名册里没这个人）。"""
    assert await read_npc(lane="coe-t1", npc_name="查无此人") is None


@pytest.mark.integration
async def test_npc_rewrite_appends_versions_and_reads_latest(roster_db):
    """同一个 NPC 整篇重写：版本递增、历史保留、读侧只认最新一版（演化层用）。"""
    from app.runtime.persist import select_all_versions

    keys = {"lane": "coe-t1", "npc_name": "林小满"}
    await write_npc(**keys, relates_to="ayana", sketch="第一版：刚转学来的同桌。")
    await write_npc(**keys, relates_to="ayana", sketch="第二版：成了无话不谈的死党。")

    versions = await select_all_versions(NPCRoster, keys)
    assert [n.version for n in versions] == [1, 2], (
        "名册是 append-only 版本链：版本逐次递增、旧版保留"
    )
    latest = await read_npc(**keys)
    assert latest.sketch == "第二版：成了无话不谈的死党。"


@pytest.mark.integration
async def test_list_npc_roster_returns_latest_per_npc(roster_db):
    """list 一个 lane 全部 NPC：每个 NPC 只取最新一版；空名册返回空表。"""
    assert await list_npc_roster(lane="coe-t1") == []

    await write_npc(
        lane="coe-t1", npc_name="林小满", relates_to="ayana", sketch="旧版。"
    )
    await write_npc(
        lane="coe-t1", npc_name="林小满", relates_to="ayana", sketch="新版。"
    )
    await write_npc(
        lane="coe-t1", npc_name="许念", relates_to="chinagi", sketch="同事。"
    )

    roster = await list_npc_roster(lane="coe-t1")
    by_name = {n.npc_name: n for n in roster}
    assert set(by_name) == {"林小满", "许念"}
    assert by_name["林小满"].sketch == "新版。", "每个 NPC 只取最新一版"


@pytest.mark.integration
async def test_npc_roster_lane_isolation(roster_db):
    """泳道隔离：coe 的名册绝不覆盖 / 泄露到 prod。"""
    await write_npc(
        lane="coe-t1", npc_name="林小满", relates_to="ayana", sketch="coe 的一个人。"
    )

    assert await read_npc(lane="prod", npc_name="林小满") is None
    assert await list_npc_roster(lane="prod") == []


# ---------------------------------------------------------------------------
# seed：7 个种子端到端落库（逐 NPC 幂等、泳道隔离）— 三步检查第③步
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_seed_writes_all_seven_npcs(roster_db):
    """seed 把 7 个种子全部落进指定 lane，list 能读回全 7 个。"""
    await seed_npc_roster(lane="coe-t1")

    roster = await list_npc_roster(lane="coe-t1")
    assert len(roster) == 7
    assert {n.npc_name for n in roster} == {s.npc_name for s in NPC_SEEDS}
    # 内容对齐种子（relates_to / sketch 落对）。
    by_name = {n.npc_name: n for n in roster}
    for s in NPC_SEEDS:
        assert by_name[s.npc_name].relates_to == s.relates_to
        assert by_name[s.npc_name].sketch == s.sketch


@pytest.mark.integration
async def test_seed_is_idempotent_per_npc(roster_db):
    """seed 重跑无害：逐 NPC CAS 幂等，第二次跑不再 append 新版（版本不涨）。"""
    from app.runtime.persist import select_all_versions

    await seed_npc_roster(lane="coe-t1")
    await seed_npc_roster(lane="coe-t1")

    # 每个 NPC 仍只有一版（seed 第二次零操作，不 append v2）。
    for s in NPC_SEEDS:
        versions = await select_all_versions(
            NPCRoster, {"lane": "coe-t1", "npc_name": s.npc_name}
        )
        assert [n.version for n in versions] == [1], (
            f"{s.npc_name} seed 重跑应幂等、不 append 新版，实际 {[n.version for n in versions]}"
        )


@pytest.mark.integration
async def test_seed_does_not_overwrite_evolved_npc(roster_db):
    """seed 对已演化（链非空）的 NPC 零操作：不把演化后的版本盖回出厂速写。"""
    # 演化层先把林小满 append 了一版（链非空）。
    await write_npc(
        lane="coe-t1",
        npc_name="林小满",
        relates_to="ayana",
        sketch="演化后的速写，绝不该被 seed 盖回。",
    )

    await seed_npc_roster(lane="coe-t1")

    latest = await read_npc(lane="coe-t1", npc_name="林小满")
    assert latest.sketch == "演化后的速写，绝不该被 seed 盖回。", (
        "seed 是 CAS 幂等（仅链空时灌）：链非空的 NPC 绝不被出厂速写盖回"
    )


@pytest.mark.integration
async def test_seed_lane_isolation(roster_db):
    """seed 指定 lane：只落进该 lane，绝不污染别的泳道。"""
    await seed_npc_roster(lane="coe-t1")

    assert await list_npc_roster(lane="prod") == []
    assert await list_npc_roster(lane="coe-other") == []
