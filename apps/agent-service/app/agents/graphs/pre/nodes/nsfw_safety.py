"""NSFW 内容检测节点

对未成年 persona（绫奈）拦截 NSFW 内容，对成年 persona 仅记录放行。
"""

import logging

from pydantic import BaseModel, Field

from app.agents.graphs.pre.state import BlockReason, PreState, SafetyResult
from app.agents.infra.langfuse_client import get_prompt
from app.agents.infra.model_builder import ModelBuilder

logger = logging.getLogger(__name__)

# 需要拦截 NSFW 内容的 persona（未成年）
NSFW_BLOCKED_PERSONAS = frozenset({"ayana"})


class NsfwCheckResult(BaseModel):
    """NSFW 内容检测结果"""

    is_nsfw: bool = Field(description="是否包含色情、性暗示或其他成人内容")
    confidence: float = Field(description="置信度 0-1", ge=0, le=1)


async def check_nsfw_content(state: PreState, config) -> dict:
    """NSFW 内容检测节点

    - 未成年 persona + NSFW → 拦截
    - 成年 persona + NSFW → 记录，放行
    - 非 NSFW → 放行
    """
    message = state["message_content"]
    persona_id = state["persona_id"]

    try:
        langfuse_prompt = get_prompt("guard_nsfw_content")
        messages = langfuse_prompt.compile(message=message)

        model = await ModelBuilder.build_chat_model(
            "guard-model", reasoning_effort="low"
        )
        structured_model = model.with_structured_output(NsfwCheckResult)

        result: NsfwCheckResult = await structured_model.ainvoke(
            messages, config=config
        )

        if result.is_nsfw and result.confidence >= 0.75:
            if persona_id in NSFW_BLOCKED_PERSONAS:
                logger.warning(
                    f"NSFW 内容拦截: persona={persona_id}, confidence={result.confidence}"
                )
                return {
                    "safety_results": [
                        SafetyResult(
                            blocked=True,
                            reason=BlockReason.NSFW_CONTENT,
                            detail=f"confidence={result.confidence}",
                        )
                    ]
                }
            else:
                logger.info(
                    f"NSFW 内容记录(放行): persona={persona_id}, confidence={result.confidence}"
                )
                return {
                    "safety_results": [
                        SafetyResult(
                            blocked=False,
                            detail=f"nsfw_logged:persona={persona_id},confidence={result.confidence}",
                        )
                    ]
                }

        return {"safety_results": [SafetyResult(blocked=False)]}

    except Exception as e:
        logger.error(f"NSFW 内容检测失败: {e}")
        return {"safety_results": [SafetyResult(blocked=False)]}
