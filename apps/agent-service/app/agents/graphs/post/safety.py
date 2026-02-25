"""输出端安全检测

顺序执行: 封禁词检测 → LLM 内容审核
"""

import logging
from dataclasses import dataclass

from langfuse.langchain import CallbackHandler
from pydantic import BaseModel, Field

from app.agents.infra.langfuse_client import get_prompt
from app.agents.infra.model_builder import ModelBuilder
from app.services.banned_word import check_banned_word

logger = logging.getLogger(__name__)


class OutputSafetyResult(BaseModel):
    """LLM 输出安全审核结构化结果"""

    is_unsafe: bool = Field(description="回复是否包含不安全内容")
    confidence: float = Field(description="置信度 0-1", ge=0, le=1)


@dataclass
class PostSafetyResult:
    blocked: bool = False
    reason: str | None = None
    detail: str | None = None


async def run_post_safety(response_text: str) -> PostSafetyResult:
    """对 AI 生成的回复执行输出安全检测

    检测顺序:
    1. 封禁词匹配（快速，无 LLM）
    2. LLM 内容审核（structured output）

    遵循 fail-open 策略：检测异常时放行。
    """
    if not response_text or not response_text.strip():
        return PostSafetyResult(blocked=False)

    # Step 1: 封禁词
    try:
        banned = await check_banned_word(response_text)
        if banned:
            logger.warning("输出封禁词命中: %s", banned)
            return PostSafetyResult(
                blocked=True, reason="output_banned_word", detail=banned
            )
    except Exception as e:
        logger.error("输出封禁词检测失败: %s", e)

    # Step 2: LLM 内容审核
    try:
        langfuse_prompt = get_prompt("guard_output_safety")
        messages = langfuse_prompt.compile(response=response_text)

        model = await ModelBuilder.build_chat_model(
            "guard-model", reasoning_effort="low"
        )
        structured_model = model.with_structured_output(OutputSafetyResult)
        langfuse_config = {
            "callbacks": [CallbackHandler()],
            "run_name": "post-safety-check",
        }
        result: OutputSafetyResult = await structured_model.ainvoke(
            messages, config=langfuse_config
        )

        if result.is_unsafe and result.confidence >= 0.7:
            logger.warning("输出安全检测: unsafe, confidence=%.2f", result.confidence)
            return PostSafetyResult(
                blocked=True,
                reason="output_unsafe",
                detail=f"confidence={result.confidence}",
            )
    except Exception as e:
        logger.error("输出安全 LLM 检测失败: %s", e)
        # Fail open

    return PostSafetyResult(blocked=False)
