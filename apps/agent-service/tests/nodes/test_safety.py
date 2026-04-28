"""Tests for nodes/safety.py (Phase 2)."""
from __future__ import annotations


def test_module_imports():
    """烟囱测试：模块能加载，含必要 helper / 常量。"""
    from app.nodes import safety as m

    assert hasattr(m, "_check_banned_word")
    assert hasattr(m, "_check_injection")
    assert hasattr(m, "_check_politics")
    assert hasattr(m, "_check_nsfw")
    assert hasattr(m, "_check_output")
    assert hasattr(m, "_run_audit")
    assert hasattr(m, "BlockReason")
    assert hasattr(m, "TERMINAL_STATUSES")
    # TERMINAL_STATUSES 内容
    assert m.TERMINAL_STATUSES == frozenset(
        {"passed", "blocked", "recalled", "recall_failed"}
    )
