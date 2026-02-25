"""test_pre_state.py — merge_safety_results 纯函数测试"""

import pytest

from app.agents.graphs.pre.state import (
    BlockReason,
    SafetyResult,
    merge_safety_results,
)

pytestmark = pytest.mark.unit


class TestMergeSafetyResults:
    """merge_safety_results 合并逻辑测试"""

    def test_merge_two_empty_lists(self):
        assert merge_safety_results([], []) == []

    def test_merge_empty_with_non_empty(self):
        new = [SafetyResult(blocked=False)]
        result = merge_safety_results([], new)
        assert result == new

    def test_merge_non_empty_with_empty(self):
        existing = [SafetyResult(blocked=True, reason=BlockReason.BANNED_WORD)]
        result = merge_safety_results(existing, [])
        assert result == existing

    def test_merge_preserves_order(self):
        a = SafetyResult(blocked=False)
        b = SafetyResult(blocked=True, reason=BlockReason.PROMPT_INJECTION)
        c = SafetyResult(blocked=True, reason=BlockReason.SENSITIVE_POLITICS)

        result = merge_safety_results([a], [b, c])
        assert result == [a, b, c]

    def test_merge_does_not_deduplicate(self):
        """同一个 SafetyResult 出现在两侧时不做去重"""
        item = SafetyResult(blocked=False)
        result = merge_safety_results([item], [item])
        assert len(result) == 2

    def test_merge_returns_new_list(self):
        """返回的列表是新对象，不修改输入"""
        existing = [SafetyResult(blocked=False)]
        new = [SafetyResult(blocked=True, reason=BlockReason.BANNED_WORD)]
        result = merge_safety_results(existing, new)
        assert result is not existing
        assert result is not new
        assert len(existing) == 1  # 原列表未被修改
        assert len(new) == 1

    def test_merge_multiple_blocked_results(self):
        """多个被拦截结果都应保留"""
        results_a = [
            SafetyResult(
                blocked=True,
                reason=BlockReason.BANNED_WORD,
                detail="word1",
            )
        ]
        results_b = [
            SafetyResult(
                blocked=True,
                reason=BlockReason.PROMPT_INJECTION,
                detail="confidence=0.9",
            )
        ]
        merged = merge_safety_results(results_a, results_b)
        blocked = [r for r in merged if r.blocked]
        assert len(blocked) == 2
