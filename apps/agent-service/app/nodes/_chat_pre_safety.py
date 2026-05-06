"""chat_node pre-safety segment helper.

Internal to chat_node — 调用方只有 ``app.nodes.chat_node``。下划线前缀
表示这是 chat_node 的实现细节，对外保持 chat_node 模块一个入口。

设计参见 specs/2026-05-06-dataflow-phase-5-chat-pipeline-design.md
（pre-safety BLOCK 段边界等 verdict + fail-open 语义）。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class _PreSafetyResult:
    blocked: bool
    content: str  # ALLOW: 原 part；BLOCK: 不用，由调用方 emit guard


async def _resolve_pre_safety_for_part(
    part: str,
    pre_task: asyncio.Task,
    guard_message: str,
    timeout: float = 5.0,
) -> _PreSafetyResult:
    """段边界等 verdict（已 done 即立刻返回，未 done 则带 timeout 等）。

    fail-open（pre_task 抛 / timeout）-> ALLOW（保持与 Phase 2 pre-safety
    设计一致的 fail-open 语义）。
    """
    if not pre_task.done():
        try:
            await asyncio.wait_for(pre_task, timeout=timeout)
        except TimeoutError:
            logger.warning("pre_safety timeout (%.1fs), fail-open", timeout)
            return _PreSafetyResult(blocked=False, content=part)
        except Exception as e:
            logger.error("pre_safety exception (fail-open): %s", e)
            return _PreSafetyResult(blocked=False, content=part)
    try:
        verdict = pre_task.result()
    except Exception as e:
        logger.error("pre_safety result raise (fail-open): %s", e)
        return _PreSafetyResult(blocked=False, content=part)
    if verdict.is_blocked:
        return _PreSafetyResult(blocked=True, content=guard_message)
    return _PreSafetyResult(blocked=False, content=part)
