from __future__ import annotations

from datetime import datetime
from typing import Annotated

import pytest

from app.data.migrator import MigrationError, plan_migration
from app.data.schema import Data, Key, Version


class Msg(Data):
    mid: Annotated[str, Key]
    gen: int = 0
    text: str


class State(Data):
    pid: Annotated[str, Key]
    ver: Annotated[int, Version] = 0
    mood: str


def test_plan_creates_table_for_new_data():
    plan = plan_migration([Msg], existing_schema={})
    stmts = [s.sql for s in plan.stmts]
    assert any("CREATE TABLE IF NOT EXISTS data_msg" in s for s in stmts)
    assert any('"mid" VARCHAR' in s or '"mid" TEXT' in s for s in stmts)
    # dedup_hash UNIQUE for idempotent durable writes
    assert any("dedup_hash" in s for s in stmts)


def test_plan_adds_index_on_key_version_for_append_only():
    plan = plan_migration([State], existing_schema={})
    stmts = " ".join(s.sql for s in plan.stmts)
    # State has Version -> index (pid, ver DESC)
    assert "CREATE INDEX" in stmts
    assert "pid" in stmts and "ver" in stmts


def test_plan_add_column_on_existing_table():
    existing = {"data_msg": {"mid": "text", "text": "text"}}  # gen missing
    plan = plan_migration([Msg], existing_schema=existing)
    stmts = [s.sql for s in plan.stmts]
    assert any("ALTER TABLE data_msg ADD COLUMN" in s and "gen" in s for s in stmts)


def test_plan_rejects_breaking_change():
    existing = {"data_msg": {"mid": "text", "text": "text", "obsolete": "int"}}
    with pytest.raises(MigrationError, match="column data_msg.obsolete dropped"):
        plan_migration([Msg], existing_schema=existing)


class Optional1(Data):
    oid: Annotated[str, Key]
    mood: str | None = None
    when: datetime | None = None


def test_optional_unwraps_to_nullable_pg_type():
    # Optional[X] columns must get the scalar PG type of X, not JSONB.
    plan = plan_migration([Optional1], existing_schema={})
    ddl = " ".join(s.sql for s in plan.stmts)
    assert '"mood" TEXT' in ddl
    assert '"when" TIMESTAMPTZ' in ddl


def test_non_optional_union_rejected():
    class BadUnion(Data):
        bid: Annotated[str, Key]
        mixed: int | str  # ambiguous — no single PG type

    with pytest.raises(MigrationError, match="Union"):
        plan_migration([BadUnion], existing_schema={})


def test_plan_rejects_type_change():
    # existing has `gen` as TEXT, but Msg declares `gen: int`
    # -> BIGINT. Silent type change would corrupt persistence.
    existing = {"data_msg": {"mid": "text", "gen": "text", "text": "text"}}
    with pytest.raises(MigrationError, match="type mismatch"):
        plan_migration([Msg], existing_schema=existing)


class Windowed(Data):
    """Meta.indexes 声明式二级索引的测试 Data（游标窗口查询形态）。"""

    wlane: Annotated[str, Key]
    wid: Annotated[str, Key]
    body: str

    class Meta:
        indexes = (("wlane", "created_at", "wid"),)


def test_meta_indexes_emits_create_index_for_new_table():
    """Meta.indexes 声明 → 建表计划里带幂等的复合索引 DDL（列序保序、列名带引号）。

    游标窗口类查询（WHERE key 列 + created_at 增量过滤）没有支撑索引会随
    append-only 历史线性退化——声明式索引让 Data 自己把查询形态说出来，
    migrator 统一建，业务不 ad-hoc 发 DDL。
    """
    plan = plan_migration([Windowed], existing_schema={})
    stmts = [s.sql for s in plan.stmts]
    assert any(
        'CREATE INDEX IF NOT EXISTS ix_data_windowed_wlane_created_at_wid '
        'ON data_windowed("wlane", "created_at", "wid")' == s
        for s in stmts
    ), f"未生成声明的复合索引，实得: {stmts}"


def test_meta_indexes_backfills_on_existing_table():
    """表已存在（coe/prod 早前部署建过）→ 声明的索引仍要补建（IF NOT EXISTS 幂等）。

    现状 migrator 对已存在的表只发 ALTER ADD COLUMN、索引只在新建分支发——
    那样先上过一版的表永远补不上索引。声明式索引必须两个分支都发。
    """
    existing = {
        "data_windowed": {
            "wlane": "text",
            "wid": "text",
            "body": "text",
            "dedup_hash": "text",
            "created_at": "timestamptz",
        }
    }
    plan = plan_migration([Windowed], existing_schema=existing)
    stmts = [s.sql for s in plan.stmts]
    assert any(
        "CREATE INDEX IF NOT EXISTS ix_data_windowed_wlane_created_at_wid" in s
        for s in stmts
    ), f"已存在的表未补建声明索引，实得: {stmts}"


def test_meta_indexes_rejects_unknown_column():
    """索引列既不是声明字段、也不是 migrator 实际建的 runtime 列 → fail-fast。

    拼错列名若放行到 DDL 才炸，报错离声明处太远；在 plan 阶段带类名/列名
    清晰拒绝（同 destructive change 的 MigrationError 姿势）。
    """

    class BadIndex(Data):
        bid2: Annotated[str, Key]

        class Meta:
            indexes = (("bid2", "no_such_col"),)

    with pytest.raises(MigrationError, match="no_such_col"):
        plan_migration([BadIndex], existing_schema={})


def test_plan_migration_skips_transient():
    """Data classes with Meta.transient=True must produce zero DDL.

    Transient Data (e.g. vectorize output ``Fragment``) never reaches pg;
    it flows through an in-process edge to a VectorStore Sink. The
    migrator must emit no CREATE, no ALTER, no INDEX for it.
    """
    class TransientTmp(Data):
        tid: Annotated[str, Key]

        class Meta:
            transient = True

    plan = plan_migration([TransientTmp], existing_schema={})
    sql_joined = "\n".join(s.sql for s in plan.stmts)
    # The auto-derived table name would be "data_transient_tmp" if not skipped.
    assert "data_transient_tmp" not in sql_joined
    assert plan.stmts == []
