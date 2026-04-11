"""test_pre_nsfw.py — NSFW 节点在 Pre Graph 中的集成测试

验证 NSFW 检测在完整 Pre Graph 中的 persona-aware 行为：
- 未成年 persona + NSFW → 拦截
- 成年 persona + NSFW → 放行
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.graphs.pre.state import BlockReason

pytestmark = pytest.mark.integration


def _make_extract_side_effect(result_map: dict):
    """创建 LLMService.extract 的 side_effect，根据 schema 参数返回不同结果。"""

    async def _side_effect(prompt_id, prompt_vars, messages, schema, **kwargs):
        if schema in result_map:
            return result_map[schema]
        raise RuntimeError(f"No mock for {schema}")

    return _side_effect


@pytest.fixture(autouse=True)
def _clear_pre_graph_cache():
    """每个测试前清空 pre graph 的 lru_cache"""
    from app.agents.graphs.pre.graph import get_pre_graph

    get_pre_graph.cache_clear()
    yield
    get_pre_graph.cache_clear()


class TestPreGraphNsfwMinorBlocked:
    """NSFW 内容 + 未成年 persona → 拦截"""

    async def test_nsfw_blocks_ayana(self):
        from app.agents.graphs.pre.nodes.nsfw_safety import NsfwCheckResult
        from app.agents.graphs.pre.nodes.safety import (
            PoliticsCheckResult,
            PromptInjectionResult,
        )

        result_map = {
            PromptInjectionResult: PromptInjectionResult(
                is_injection=False, confidence=0.1
            ),
            PoliticsCheckResult: PoliticsCheckResult(
                is_sensitive=False, confidence=0.1
            ),
            NsfwCheckResult: NsfwCheckResult(is_nsfw=True, confidence=0.9),
        }

        with (
            patch(
                "app.agents.graphs.pre.nodes.safety.check_banned_word",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "app.agents.graphs.pre.nodes.safety.LLMService.extract",
                new_callable=AsyncMock,
                side_effect=_make_extract_side_effect(result_map),
            ),
            patch(
                "app.agents.graphs.pre.nodes.nsfw_safety.LLMService.extract",
                new_callable=AsyncMock,
                side_effect=_make_extract_side_effect(result_map),
            ),
            patch(
                "app.agents.graphs.pre.nodes.safety.get_prompt",
                return_value=MagicMock(
                    compile=MagicMock(return_value="mocked_messages")
                ),
            ),
            patch(
                "app.agents.graphs.pre.nodes.nsfw_safety.get_prompt",
                return_value=MagicMock(
                    compile=MagicMock(return_value="mocked_messages")
                ),
            ),
            patch(
                "app.agents.graphs.pre.graph.CallbackHandler",
                return_value=MagicMock(),
            ),
        ):
            from app.agents.graphs.pre.graph import run_pre

            result = await run_pre("NSFW 内容", persona_id="ayana")

        assert result["is_blocked"] is True
        assert result["block_reason"] == BlockReason.NSFW_CONTENT


class TestPreGraphNsfwAdultPasses:
    """NSFW 内容 + 成年 persona → 放行"""

    async def test_nsfw_passes_akao(self):
        from app.agents.graphs.pre.nodes.nsfw_safety import NsfwCheckResult
        from app.agents.graphs.pre.nodes.safety import (
            PoliticsCheckResult,
            PromptInjectionResult,
        )

        result_map = {
            PromptInjectionResult: PromptInjectionResult(
                is_injection=False, confidence=0.1
            ),
            PoliticsCheckResult: PoliticsCheckResult(
                is_sensitive=False, confidence=0.1
            ),
            NsfwCheckResult: NsfwCheckResult(is_nsfw=True, confidence=0.9),
        }

        with (
            patch(
                "app.agents.graphs.pre.nodes.safety.check_banned_word",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "app.agents.graphs.pre.nodes.safety.LLMService.extract",
                new_callable=AsyncMock,
                side_effect=_make_extract_side_effect(result_map),
            ),
            patch(
                "app.agents.graphs.pre.nodes.nsfw_safety.LLMService.extract",
                new_callable=AsyncMock,
                side_effect=_make_extract_side_effect(result_map),
            ),
            patch(
                "app.agents.graphs.pre.nodes.safety.get_prompt",
                return_value=MagicMock(
                    compile=MagicMock(return_value="mocked_messages")
                ),
            ),
            patch(
                "app.agents.graphs.pre.nodes.nsfw_safety.get_prompt",
                return_value=MagicMock(
                    compile=MagicMock(return_value="mocked_messages")
                ),
            ),
            patch(
                "app.agents.graphs.pre.graph.CallbackHandler",
                return_value=MagicMock(),
            ),
        ):
            from app.agents.graphs.pre.graph import run_pre

            result = await run_pre("NSFW 内容", persona_id="akao")

        assert result["is_blocked"] is False
