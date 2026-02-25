"""复杂度分类节点

轻量级分类，判断任务复杂度：
- simple: 直接回答或单次工具调用
- complex: 需要多步推理或多次工具调用
- super_complex: 预留，��来可启用子 Agent
"""

import logging

from pydantic import BaseModel, Field

from app.agents.graphs.pre.state import (
    Complexity,
    ComplexityResult,
    PreState,
)
from app.agents.infra.langfuse_client import get_prompt
from app.agents.infra.model_builder import ModelBuilder

logger = logging.getLogger(__name__)


class ComplexityClassification(BaseModel):
    """复杂度分类输出"""

    complexity: str = Field(description="复杂度: simple/complex/super_complex")
    confidence: float = Field(
        description="置信度 0-1",
        ge=0,
        le=1,
    )


async def classify_complexity(state: PreState, config) -> dict:
    """复杂度分类节点"""
    message = state["message_content"]

    try:
        langfuse_prompt = get_prompt("pre_complexity_classification")
        messages = langfuse_prompt.compile(message=message)

        model = await ModelBuilder.build_chat_model(
            "pre-complexity-model", reasoning_effort="low"
        )
        structured_model = model.with_structured_output(ComplexityClassification)

        result: ComplexityClassification = await structured_model.ainvoke(
            messages, config=config
        )

        # 映射到枚举，处理无效值
        try:
            complexity = Complexity(result.complexity)
        except ValueError:
            complexity = Complexity.SIMPLE

        logger.info(
            f"复杂度分类: complexity={complexity.value}, confidence={result.confidence}"
        )

        return {
            "complexity_result": ComplexityResult(
                complexity=complexity,
                confidence=result.confidence,
            )
        }

    except Exception as e:
        logger.error(f"复杂度分类失败: {e}")
        # 失败时返回默认值
        return {
            "complexity_result": ComplexityResult(
                complexity=Complexity.SIMPLE,
                confidence=0.5,
            )
        }
