"""安全检测节点

从原 guard 迁移，包含：
1. 关键词检测（快速，无 LLM）
2. 提示词注入检测（LLM）
3. 敏感政治检测（LLM）
"""

import logging

from pydantic import BaseModel, Field

from app.agents.graphs.pre.state import BlockReason, PreState, SafetyResult
from app.agents.infra.langfuse_client import get_prompt
from app.agents.infra.model_builder import ModelBuilder
from app.services.banned_word import check_banned_word

logger = logging.getLogger(__name__)


class PromptInjectionResult(BaseModel):
    """提示词注入检测结果"""

    is_injection: bool = Field(description="是否尝试获取系统提示词或进行提示词注入")
    confidence: float = Field(description="置信度 0-1", ge=0, le=1)


class PoliticsCheckResult(BaseModel):
    """敏感政治检测结果"""

    is_sensitive: bool = Field(description="是否涉及敏感政治话题")
    confidence: float = Field(description="置信度 0-1", ge=0, le=1)


async def check_banned_word_node(state: PreState) -> dict:
    """关键词检测节点（快速，无 LLM）"""
    message = state["message_content"]

    try:
        banned_word = await check_banned_word(message)

        if banned_word:
            logger.warning(f"检测到封禁词: {banned_word}")
            return {
                "safety_results": [
                    SafetyResult(
                        blocked=True,
                        reason=BlockReason.BANNED_WORD,
                        detail=banned_word,
                    )
                ]
            }

        return {"safety_results": [SafetyResult(blocked=False)]}

    except Exception as e:
        logger.error(f"关键词检测失败: {e}")
        return {"safety_results": [SafetyResult(blocked=False)]}


async def check_prompt_injection(state: PreState, config) -> dict:
    """提示词注入检测节点"""
    message = state["message_content"]

    try:
        langfuse_prompt = get_prompt("guard_prompt_injection")
        messages = langfuse_prompt.compile(message=message)

        model = await ModelBuilder.build_chat_model(
            "guard-model", reasoning_effort="low"
        )
        structured_model = model.with_structured_output(PromptInjectionResult)

        result: PromptInjectionResult = await structured_model.ainvoke(
            messages, config=config
        )

        if result.is_injection and result.confidence >= 0.7:
            logger.warning(f"检测到提示词注入: confidence={result.confidence}")
            return {
                "safety_results": [
                    SafetyResult(
                        blocked=True,
                        reason=BlockReason.PROMPT_INJECTION,
                        detail=f"confidence={result.confidence}",
                    )
                ]
            }

        return {"safety_results": [SafetyResult(blocked=False)]}

    except Exception as e:
        logger.error(f"提示词注入检测失败: {e}")
        return {"safety_results": [SafetyResult(blocked=False)]}


async def check_sensitive_politics(state: PreState, config) -> dict:
    """敏感政治检测节点"""
    message = state["message_content"]

    try:
        langfuse_prompt = get_prompt("guard_sensitive_politics")
        messages = langfuse_prompt.compile(message=message)

        model = await ModelBuilder.build_chat_model(
            "guard-model", reasoning_effort="low"
        )
        structured_model = model.with_structured_output(PoliticsCheckResult)

        result: PoliticsCheckResult = await structured_model.ainvoke(
            messages, config=config
        )

        if result.is_sensitive and result.confidence >= 0.7:
            logger.warning(f"检测到敏感政治话题: confidence={result.confidence}")
            return {
                "safety_results": [
                    SafetyResult(
                        blocked=True,
                        reason=BlockReason.SENSITIVE_POLITICS,
                        detail=f"confidence={result.confidence}",
                    )
                ]
            }

        return {"safety_results": [SafetyResult(blocked=False)]}

    except Exception as e:
        logger.error(f"敏感政治检测失败: {e}")
        return {"safety_results": [SafetyResult(blocked=False)]}


def aggregate_results(state: PreState) -> dict:
    """聚合所有检测结果"""
    results = state["safety_results"]

    blocked_result = next((r for r in results if r.blocked), None)

    if blocked_result:
        logger.info(
            f"消息被拦截: reason={blocked_result.reason}, detail={blocked_result.detail}"
        )
        return {
            "is_blocked": True,
            "block_reason": blocked_result.reason,
        }

    return {
        "is_blocked": False,
        "block_reason": None,
    }
