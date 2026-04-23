from __future__ import annotations

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
    existing = {
        "data_msg": {"mid": "text", "text": "text", "obsolete": "int"}
    }
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
