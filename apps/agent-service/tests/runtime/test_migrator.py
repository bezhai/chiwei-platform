from __future__ import annotations

from datetime import datetime
from typing import Annotated

import pytest

from app.runtime.data import AdminOnly, Data, DedupKey, Key, Version
from app.runtime.migrator import MigrationError, plan_migration


class Msg(Data):
    mid: Annotated[str, Key, DedupKey]
    gen: Annotated[int, DedupKey] = 0
    text: str


class State(Data):
    pid: Annotated[str, Key]
    ver: Annotated[int, Version] = 0
    mood: str


class Cfg(Data, AdminOnly):
    cid: Annotated[str, Key]
    v: dict


def test_plan_creates_table_for_new_data():
    plan = plan_migration([Msg], existing_schema={})
    stmts = [s.sql for s in plan.stmts]
    assert any("CREATE TABLE IF NOT EXISTS data_msg" in s for s in stmts)
    assert any("mid VARCHAR" in s or "mid TEXT" in s for s in stmts)
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


def test_existing_table_mapping_skips_create():
    class Legacy(Data):
        mid: Annotated[str, Key]
        text: str

        class Meta:
            existing_table = "conversation_messages"

    existing = {"conversation_messages": {"mid": "text", "text": "text"}}
    plan = plan_migration([Legacy], existing_schema=existing)
    stmts = [s.sql for s in plan.stmts]
    assert not any("CREATE TABLE" in s for s in stmts)


def test_admin_only_not_migrated_by_business_code():
    # AdminOnly tables are managed externally; business migrator skips them.
    plan = plan_migration([Cfg], existing_schema={})
    stmts = [s.sql for s in plan.stmts]
    assert stmts == []  # skip entirely


class Optional1(Data):
    oid: Annotated[str, Key]
    mood: str | None = None
    when: datetime | None = None


def test_optional_unwraps_to_nullable_pg_type():
    # Optional[X] columns must get the scalar PG type of X, not JSONB.
    plan = plan_migration([Optional1], existing_schema={})
    ddl = " ".join(s.sql for s in plan.stmts)
    assert "mood TEXT" in ddl
    assert "when TIMESTAMPTZ" in ddl


def test_non_optional_union_rejected():
    class BadUnion(Data):
        bid: Annotated[str, Key]
        mixed: int | str  # ambiguous — no single PG type

    with pytest.raises(MigrationError, match="Union"):
        plan_migration([BadUnion], existing_schema={})


def test_existing_table_mapping_emits_no_alter():
    class Legacy(Data):
        mid: Annotated[str, Key]
        text: str

        class Meta:
            existing_table = "conversation_messages"

    # Existing table lacks dedup_hash/created_at: a managed table would
    # emit ALTERs. Adoption mode must emit nothing.
    existing = {"conversation_messages": {"mid": "text", "text": "text"}}
    plan = plan_migration([Legacy], existing_schema=existing)
    assert plan.stmts == []


def test_existing_table_mapping_no_drop_check():
    class Legacy(Data):
        mid: Annotated[str, Key]

        class Meta:
            existing_table = "conversation_messages"

    # Legacy table has columns not declared on the Data class. A managed
    # table would raise ``dropped``; adoption mode skips the drop-check too.
    existing = {"conversation_messages": {"mid": "text", "extra_col": "text"}}
    plan = plan_migration([Legacy], existing_schema=existing)
    assert plan.stmts == []


def test_plan_rejects_type_change():
    # existing has `gen` as TEXT, but Msg declares `gen: Annotated[int, DedupKey]`
    # -> BIGINT. Silent type change would corrupt persistence.
    existing = {"data_msg": {"mid": "text", "gen": "text", "text": "text"}}
    with pytest.raises(MigrationError, match="type mismatch"):
        plan_migration([Msg], existing_schema=existing)


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
