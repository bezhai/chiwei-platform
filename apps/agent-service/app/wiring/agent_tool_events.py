"""Agent tool side-effect wires.

Each mutation tool emits a tool-event Data; this module declares the
default downstream subscribers. Future subscribers (reviewer, cache
invalidation, etc.) plug in here without changing tool bodies.

Phase 6 v4 Gap 3 closure.
"""
from app.domain.agent_tool_events import AbstractMemoryCommitted
from app.nodes.memory_pipelines import on_abstract_committed
from app.runtime import wire

# tool emits AbstractMemoryCommitted -> on_abstract_committed re-emits
# MemoryAbstractRequest (which has Source.mq + cross-process consumer).
wire(AbstractMemoryCommitted).to(on_abstract_committed)

# NoteCreated has no default subscriber yet — Data class registered so
# downstream code can subscribe later without redefining the type.
