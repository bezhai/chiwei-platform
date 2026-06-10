"""Import all wiring submodules so their ``wire(...)`` calls run on package import."""

from app.wiring import (  # noqa: F401
    admin,
    agent_tool_events,
    chat,
    fetch_dataflow,
    life_dataflow,
    memory_triggers,
    memory_vectorize,
    safety,
)
