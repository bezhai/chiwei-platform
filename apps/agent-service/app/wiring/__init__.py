"""Import all wiring submodules so their ``wire(...)`` calls run on package import."""

from app.wiring import (  # noqa: F401
    admin,
    agent_tool_events,
    chat,
    life_dataflow,
    memory,
    memory_triggers,
    memory_vectorize,
    safety,
)
