"""Import all wiring submodules so their ``wire(...)`` calls run on package import."""
from app.wiring import memory, memory_triggers, memory_vectorize, safety  # noqa: F401
