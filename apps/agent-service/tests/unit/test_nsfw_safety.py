"""test_nsfw_safety.py — NSFW 检测节点单元测试"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.graphs.pre.state import BlockReason, SafetyResult

pytestmark = pytest.mark.unit


class TestCheckNsfwContent:
    """NSFW 内容检测节点"""

    async def test_nsfw_detected_minor_persona_blocked(self):
        """NSFW 内容 + 未成年 persona → 拦截"""
        from app.agents.graphs.pre.nodes.nsfw_safety import (
            NsfwCheckResult,
            check_nsfw_content,
        )

        state = {
            "message_content": "一些 NSFW 内容",
            "persona_id": "ayana",
            "safety_results": [],
        }

        with (
            patch(
                "app.agents.graphs.pre.nodes.nsfw_safety.LLMService.extract",
                new_callable=AsyncMock,
                return_value=NsfwCheckResult(is_nsfw=True, confidence=0.9),
            ),
            patch(
                "app.agents.graphs.pre.nodes.nsfw_safety.get_prompt",
                return_value=MagicMock(
                    compile=MagicMock(return_value="mocked_messages")
                ),
            ),
        ):
            result = await check_nsfw_content(state, config={"callbacks": []})

        safety = result["safety_results"][0]
        assert safety.blocked is True
        assert safety.reason == BlockReason.NSFW_CONTENT

    async def test_nsfw_detected_adult_persona_logged_not_blocked(self):
        """NSFW 内容 + 成年 persona → 记录但放行"""
        from app.agents.graphs.pre.nodes.nsfw_safety import (
            NsfwCheckResult,
            check_nsfw_content,
        )

        state = {
            "message_content": "一些 NSFW 内容",
            "persona_id": "akao",
            "safety_results": [],
        }

        with (
            patch(
                "app.agents.graphs.pre.nodes.nsfw_safety.LLMService.extract",
                new_callable=AsyncMock,
                return_value=NsfwCheckResult(is_nsfw=True, confidence=0.9),
            ),
            patch(
                "app.agents.graphs.pre.nodes.nsfw_safety.get_prompt",
                return_value=MagicMock(
                    compile=MagicMock(return_value="mocked_messages")
                ),
            ),
        ):
            result = await check_nsfw_content(state, config={"callbacks": []})

        safety = result["safety_results"][0]
        assert safety.blocked is False
        assert "nsfw_logged" in (safety.detail or "")

    async def test_no_nsfw_content_passes(self):
        """非 NSFW 内容 → 放行"""
        from app.agents.graphs.pre.nodes.nsfw_safety import (
            NsfwCheckResult,
            check_nsfw_content,
        )

        state = {
            "message_content": "正常聊天内容",
            "persona_id": "ayana",
            "safety_results": [],
        }

        with (
            patch(
                "app.agents.graphs.pre.nodes.nsfw_safety.LLMService.extract",
                new_callable=AsyncMock,
                return_value=NsfwCheckResult(is_nsfw=False, confidence=0.1),
            ),
            patch(
                "app.agents.graphs.pre.nodes.nsfw_safety.get_prompt",
                return_value=MagicMock(
                    compile=MagicMock(return_value="mocked_messages")
                ),
            ),
        ):
            result = await check_nsfw_content(state, config={"callbacks": []})

        safety = result["safety_results"][0]
        assert safety.blocked is False

    async def test_llm_failure_passes(self):
        """LLM 异常 → fail-open 放行"""
        from app.agents.graphs.pre.nodes.nsfw_safety import check_nsfw_content

        state = {
            "message_content": "任何内容",
            "persona_id": "ayana",
            "safety_results": [],
        }

        with (
            patch(
                "app.agents.graphs.pre.nodes.nsfw_safety.LLMService.extract",
                new_callable=AsyncMock,
                side_effect=RuntimeError("LLM down"),
            ),
            patch(
                "app.agents.graphs.pre.nodes.nsfw_safety.get_prompt",
                return_value=MagicMock(
                    compile=MagicMock(return_value="mocked_messages")
                ),
            ),
        ):
            result = await check_nsfw_content(state, config={"callbacks": []})

        safety = result["safety_results"][0]
        assert safety.blocked is False
