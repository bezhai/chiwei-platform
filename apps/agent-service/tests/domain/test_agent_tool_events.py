"""Phase 6 v4 Gap 3: agent tool event Data classes."""
from __future__ import annotations


def test_data_classes_register():
    from app.domain.agent_tool_events import (
        AbstractMemoryCommitted,
        NoteCreated,
    )
    from app.runtime.data import DATA_REGISTRY

    assert AbstractMemoryCommitted in DATA_REGISTRY
    assert NoteCreated in DATA_REGISTRY


def test_schedule_revision_created_deleted():
    """update_schedule tool 删除后 ScheduleRevisionCreated 不再存在。"""
    from app.domain import agent_tool_events as evt

    assert not hasattr(evt, "ScheduleRevisionCreated")


def test_abstract_memory_committed_fields():
    from app.domain.agent_tool_events import AbstractMemoryCommitted

    e = AbstractMemoryCommitted(abstract_id="a_1", persona_id="akao-001")
    assert e.abstract_id == "a_1"
    assert e.persona_id == "akao-001"
    assert e.chat_id is None

    e2 = AbstractMemoryCommitted(abstract_id="a_2", persona_id="akao-001", chat_id="oc_xx")
    assert e2.chat_id == "oc_xx"


def test_note_created_fields():
    from app.domain.agent_tool_events import NoteCreated

    e = NoteCreated(note_id="n_1", persona_id="akao-001")
    assert e.note_id == "n_1"
