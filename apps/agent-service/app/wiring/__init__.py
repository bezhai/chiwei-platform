"""Import all wiring submodules so their ``wire(...)`` calls run on package import."""

from app.wiring import (  # noqa: F401
    admin,
    book,
    chat,
    fetch_dataflow,
    life_dataflow,
    persona_review_dataflow,
    review_dataflow,
    safety,
)
