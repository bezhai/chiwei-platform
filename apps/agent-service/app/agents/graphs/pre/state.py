"""Pre Graph 状态定义"""

from dataclasses import dataclass
from enum import Enum
from typing import Annotated

from typing_extensions import TypedDict


class BlockReason(str, Enum):
    """拦截原因"""

    BANNED_WORD = "banned_word"
    PROMPT_INJECTION = "prompt_injection"
    SENSITIVE_POLITICS = "sensitive_politics"
    NSFW_CONTENT = "nsfw_content"


@dataclass
class SafetyResult:
    """安全检测结果"""

    blocked: bool = False
    reason: BlockReason | None = None
    detail: str | None = None


def merge_safety_results(
    existing: list[SafetyResult], new: list[SafetyResult]
) -> list[SafetyResult]:
    """合并安全检测结果"""
    return existing + new


class Complexity(str, Enum):
    """任务复杂度等级"""

    SIMPLE = "simple"
    COMPLEX = "complex"
    SUPER_COMPLEX = "super_complex"


@dataclass
class ComplexityResult:
    """复杂度分类结果"""

    complexity: Complexity = Complexity.SIMPLE
    confidence: float = 0.5


class PreState(TypedDict):
    """Pre Graph 状态"""

    # 输入
    message_content: str
    persona_id: str

    # 安全检测结果（并行合并）
    safety_results: Annotated[list[SafetyResult], merge_safety_results]

    # 复杂度分类
    complexity_result: ComplexityResult | None

    # 最终输出
    is_blocked: bool
    block_reason: BlockReason | None
