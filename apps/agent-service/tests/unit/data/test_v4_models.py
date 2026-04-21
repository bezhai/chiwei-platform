"""Test v4 memory schema ORM models."""

from __future__ import annotations

from app.data.models import (
    AbstractMemory,
    Fragment,
    MemoryEdge,
    Note,
    ScheduleRevision,
)


def test_fragment_defaults():
    f = Fragment(id="f_test", persona_id="chiwei", content="hello", source="manual")
    assert f.id == "f_test"
    assert f.persona_id == "chiwei"
    # clarity default set at DB level, not python; instantiated value is None until flush
    assert f.chat_id is None


def test_abstract_memory_instantiation():
    a = AbstractMemory(
        id="a_test",
        persona_id="chiwei",
        subject="user:u1",
        content="他是程序员",
        created_by="chiwei",
    )
    assert a.subject == "user:u1"
    assert a.created_by == "chiwei"


def test_memory_edge_instantiation():
    e = MemoryEdge(
        id="e_test",
        persona_id="chiwei",
        from_id="f_1",
        from_type="fact",
        to_id="a_1",
        to_type="abstract",
        edge_type="supports",
        created_by="chiwei",
    )
    assert e.edge_type == "supports"


def test_note_active_state():
    n = Note(id="n_test", persona_id="chiwei", content="周五看电影")
    assert n.resolved_at is None
    assert n.resolution is None


def test_schedule_revision_instantiation():
    sr = ScheduleRevision(
        id="sr_test",
        persona_id="chiwei",
        content="今天...",
        reason="first draft",
        created_by="cron_morning",
    )
    assert sr.created_by == "cron_morning"
