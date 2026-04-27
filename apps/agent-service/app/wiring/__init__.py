"""Import all wiring submodules so their ``wire(...)`` calls run on package import."""
from app.wiring import memory, memory_vectorize  # noqa: F401
