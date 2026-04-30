"""Import all wiring submodules so their ``wire(...)`` calls run on package import."""
from app.wiring import (  # noqa: F401
    life_dataflow,
    memory,
    memory_triggers,
    memory_vectorize,
    safety,
)
