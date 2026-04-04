"""test_pre_nsfw.py — NSFW 节点在 Pre Graph 中的集成测试

验证 NSFW 检测在完整 Pre Graph 中的 persona-aware 行为：
- 未成年 persona + NSFW → 拦截
- 成年 persona + NSFW → 放行
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.graphs.pre.state import BlockReason

pytestmark = pytest.mark.integration


def _make_smart_model(result_map: dict):
    """根据 with_structured_output 参数返回对应 mock 结果"""

    def _with_structured_output(cls):
        mock_structured = MagicMock()
        if cls in result_map:
            mock_structured.ainvoke = AsyncMock(return_value=result_map[cls])
        else:
            # 未知类型，返回安全默认值
            mock_structured.ainvoke = AsyncMock(
                side_effect=RuntimeError(f"No mock for {cls}")
            )
        return mock_structured

    mock_model = MagicMock()
    mock_model.with_structured_output.side_effect = _with_structured_output
    return mock_model


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
        smart_model = _make_smart_model(result_map)

        with (
            patch(
                "app.agents.graphs.pre.nodes.safety.check_banned_word",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "app.agents.graphs.pre.nodes.safety.ModelBuilder.build_chat_model",
                new_callable=AsyncMock,
                return_value=smart_model,
            ),
            patch(
                "app.agents.graphs.pre.nodes.nsfw_safety.ModelBuilder.build_chat_model",
                new_callable=AsyncMock,
                return_value=smart_model,
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
        smart_model = _make_smart_model(result_map)

        with (
            patch(
                "app.agents.graphs.pre.nodes.safety.check_banned_word",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "app.agents.graphs.pre.nodes.safety.ModelBuilder.build_chat_model",
                new_callable=AsyncMock,
                return_value=smart_model,
            ),
            patch(
                "app.agents.graphs.pre.nodes.nsfw_safety.ModelBuilder.build_chat_model",
                new_callable=AsyncMock,
                return_value=smart_model,
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
