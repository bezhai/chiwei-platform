"""run_heavy_review_for_persona 是 Phase 4 graph fan-out 的入口."""
from __future__ import annotations


def test_run_heavy_review_for_persona_is_importable():
    from app.memory.reviewer.heavy import run_heavy_review_for_persona
    import inspect
    assert inspect.iscoroutinefunction(run_heavy_review_for_persona)
    sig = inspect.signature(run_heavy_review_for_persona)
    assert "persona_id" in sig.parameters
