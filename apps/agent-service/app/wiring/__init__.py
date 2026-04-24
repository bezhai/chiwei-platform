"""Import all wiring submodules so their ``wire(...)`` calls run on package import."""
from app.wiring import memory  # noqa: F401
