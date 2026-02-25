"""test_post_safety.py — 输出安全检测纯函数 / mock 测试"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.graphs.post.safety import (
    OutputSafetyResult,
    run_post_safety,
)

pytestmark = pytest.mark.unit


class TestPostSafety:
    """run_post_safety 测试"""

    async def test_empty_text_returns_not_blocked(self):
        result = await run_post_safety("")
        assert result.blocked is False

    async def test_whitespace_only_returns_not_blocked(self):
        result = await run_post_safety("   ")
        assert result.blocked is False

    @patch("app.agents.graphs.post.safety.check_banned_word")
    async def test_banned_word_hit_returns_blocked(self, mock_check):
        mock_check.return_value = "敏感词"
        result = await run_post_safety("包含敏感词的回复")
        assert result.blocked is True
        assert result.reason == "output_banned_word"
        assert result.detail == "敏感词"

    @patch("app.agents.graphs.post.safety.check_banned_word")
    async def test_no_banned_word_proceeds_to_llm(self, mock_check):
        mock_check.return_value = None

        mock_model = AsyncMock()
        mock_model.ainvoke.return_value = OutputSafetyResult(
            is_unsafe=False, confidence=0.1
        )

        mock_structured = MagicMock()
        mock_structured.with_structured_output.return_value = mock_model

        with (
            patch(
                "app.agents.graphs.post.safety.ModelBuilder.build_chat_model",
                new_callable=AsyncMock,
                return_value=mock_structured,
            ),
            patch(
                "app.agents.graphs.post.safety.get_prompt",
                return_value=MagicMock(compile=MagicMock(return_value=[])),
            ),
        ):
            result = await run_post_safety("正常的回复内容")
            assert result.blocked is False

    @patch("app.agents.graphs.post.safety.check_banned_word")
    async def test_llm_detects_unsafe_content(self, mock_check):
        mock_check.return_value = None

        mock_model = AsyncMock()
        mock_model.ainvoke.return_value = OutputSafetyResult(
            is_unsafe=True, confidence=0.9
        )

        mock_structured = MagicMock()
        mock_structured.with_structured_output.return_value = mock_model

        with (
            patch(
                "app.agents.graphs.post.safety.ModelBuilder.build_chat_model",
                new_callable=AsyncMock,
                return_value=mock_structured,
            ),
            patch(
                "app.agents.graphs.post.safety.get_prompt",
                return_value=MagicMock(compile=MagicMock(return_value=[])),
            ),
        ):
            result = await run_post_safety("不安全的回复内容")
            assert result.blocked is True
            assert result.reason == "output_unsafe"

    @patch("app.agents.graphs.post.safety.check_banned_word")
    async def test_llm_low_confidence_passes(self, mock_check):
        mock_check.return_value = None

        mock_model = AsyncMock()
        mock_model.ainvoke.return_value = OutputSafetyResult(
            is_unsafe=True, confidence=0.5
        )

        mock_structured = MagicMock()
        mock_structured.with_structured_output.return_value = mock_model

        with (
            patch(
                "app.agents.graphs.post.safety.ModelBuilder.build_chat_model",
                new_callable=AsyncMock,
                return_value=mock_structured,
            ),
            patch(
                "app.agents.graphs.post.safety.get_prompt",
                return_value=MagicMock(compile=MagicMock(return_value=[])),
            ),
        ):
            result = await run_post_safety("低置信度内容")
            assert result.blocked is False

    @patch("app.agents.graphs.post.safety.check_banned_word")
    async def test_llm_exception_fails_open(self, mock_check):
        mock_check.return_value = None

        with (
            patch(
                "app.agents.graphs.post.safety.ModelBuilder.build_chat_model",
                new_callable=AsyncMock,
                side_effect=Exception("LLM error"),
            ),
            patch(
                "app.agents.graphs.post.safety.get_prompt",
                return_value=MagicMock(compile=MagicMock(return_value=[])),
            ),
        ):
            result = await run_post_safety("任意内容")
            assert result.blocked is False

    @patch("app.agents.graphs.post.safety.check_banned_word")
    async def test_banned_word_exception_fails_open(self, mock_check):
        mock_check.side_effect = Exception("Redis error")

        mock_model = AsyncMock()
        mock_model.ainvoke.return_value = OutputSafetyResult(
            is_unsafe=False, confidence=0.1
        )

        mock_structured = MagicMock()
        mock_structured.with_structured_output.return_value = mock_model

        with (
            patch(
                "app.agents.graphs.post.safety.ModelBuilder.build_chat_model",
                new_callable=AsyncMock,
                return_value=mock_structured,
            ),
            patch(
                "app.agents.graphs.post.safety.get_prompt",
                return_value=MagicMock(compile=MagicMock(return_value=[])),
            ),
        ):
            result = await run_post_safety("任意内容")
            assert result.blocked is False
