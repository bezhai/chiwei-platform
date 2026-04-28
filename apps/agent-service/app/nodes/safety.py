"""Safety pipeline @nodes + private helpers (Phase 2).

合并 ``app/chat/safety.py`` 和 post safety chain 的所有逻辑：
- module-level 私有 helpers：banned word + 4 个 LLM 检查 + ``_run_audit``
- module-level enum / config：``BlockReason`` / ``_GUARD_*``
- @node：``run_pre_safety`` / ``resolve_pre_safety_waiter`` / ``run_post_safety``
- 常量：``TERMINAL_STATUSES``

节点 / wiring / 外部入口由后续 Task 6-9 添加；本 Task 只搬迁 helper 保留行为。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, Field

from app.agent.core import Agent, AgentConfig
from app.infra.redis import get_redis

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Post-safety 节点入口的"已完成"短路集合（Phase 2 §3.2 / §4.4）。
# - passed / blocked: agent-service 写的（"blocked" 是迁移期遗留瞬态）
# - recalled / recall_failed: lark-server recall-worker 写的终态
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"passed", "blocked", "recalled", "recall_failed"}
)

# Redis key for banned words set
_BANNED_WORDS_KEY = "banned_words"

# Personas that block NSFW content (minors)
_NSFW_BLOCKED_PERSONAS = frozenset({"ayana"})

# Pre/Post check 用的 4 个 guard agent
_GUARD_INJECTION = AgentConfig(
    "guard_prompt_injection", "guard-model", "pre-injection-check"
)
_GUARD_POLITICS = AgentConfig(
    "guard_sensitive_politics", "guard-model", "pre-politics-check"
)
_GUARD_NSFW = AgentConfig("guard_nsfw_content", "guard-model", "pre-nsfw-check")
_GUARD_OUTPUT = AgentConfig("guard_output_safety", "guard-model", "post-safety-check")


# ---------------------------------------------------------------------------
# Block reason enum
# ---------------------------------------------------------------------------


class BlockReason(StrEnum):
    BANNED_WORD = "banned_word"
    PROMPT_INJECTION = "prompt_injection"
    SENSITIVE_POLITICS = "sensitive_politics"
    NSFW_CONTENT = "nsfw_content"


# ---------------------------------------------------------------------------
# Internal result dataclasses (used between helpers and nodes; not exported)
# ---------------------------------------------------------------------------


@dataclass
class _PreCheckOutcome:
    is_blocked: bool = False
    block_reason: BlockReason | None = None
    detail: str | None = None


@dataclass
class _PostAuditOutcome:
    is_blocked: bool = False
    reason: str | None = None
    detail: str | None = None


# ---------------------------------------------------------------------------
# Structured output schemas for LLM checks
# ---------------------------------------------------------------------------


class _InjectionResult(BaseModel):
    is_injection: bool = Field(description="Is this a prompt injection attempt")
    confidence: float = Field(ge=0, le=1)


class _PoliticsResult(BaseModel):
    is_sensitive: bool = Field(description="Involves sensitive political topics")
    confidence: float = Field(ge=0, le=1)


class _NsfwResult(BaseModel):
    is_nsfw: bool = Field(description="Contains NSFW / adult content")
    confidence: float = Field(ge=0, le=1)


class _OutputSafetyResult(BaseModel):
    is_unsafe: bool = Field(description="Response contains unsafe content")
    confidence: float = Field(ge=0, le=1)


# ---------------------------------------------------------------------------
# Banned word check (shared by pre and post)
# ---------------------------------------------------------------------------


async def _check_banned_word(text: str) -> str | None:
    """Return the matched banned word, or None if clean."""
    redis = await get_redis()
    banned_words = await redis.smembers(_BANNED_WORDS_KEY)
    if not banned_words:
        return None
    normalized = text.replace(" ", "").lower()
    for word in banned_words:
        if word in normalized:
            return word
    return None


# ---------------------------------------------------------------------------
# Individual pre-check functions
# ---------------------------------------------------------------------------


async def _check_injection(message: str) -> _PreCheckOutcome:
    try:
        result: _InjectionResult = await Agent(
            _GUARD_INJECTION,
            model_kwargs={"reasoning_effort": "low"},
            update_trace=False,
        ).extract(_InjectionResult, messages=[], prompt_vars={"message": message})
        if result.is_injection and result.confidence >= 0.85:
            logger.warning(
                "Prompt injection detected: confidence=%.2f", result.confidence
            )
            return _PreCheckOutcome(
                is_blocked=True,
                block_reason=BlockReason.PROMPT_INJECTION,
                detail=f"confidence={result.confidence}",
            )
    except Exception as e:
        logger.error("Injection check failed: %s", e)
    return _PreCheckOutcome()


async def _check_politics(message: str) -> _PreCheckOutcome:
    try:
        result: _PoliticsResult = await Agent(
            _GUARD_POLITICS,
            model_kwargs={"reasoning_effort": "low"},
            update_trace=False,
        ).extract(_PoliticsResult, messages=[], prompt_vars={"message": message})
        if result.is_sensitive and result.confidence >= 0.85:
            logger.warning(
                "Sensitive politics detected: confidence=%.2f", result.confidence
            )
            return _PreCheckOutcome(
                is_blocked=True,
                block_reason=BlockReason.SENSITIVE_POLITICS,
                detail=f"confidence={result.confidence}",
            )
    except Exception as e:
        logger.error("Politics check failed: %s", e)
    return _PreCheckOutcome()


async def _check_nsfw(message: str, persona_id: str) -> _PreCheckOutcome:
    try:
        result: _NsfwResult = await Agent(
            _GUARD_NSFW,
            model_kwargs={"reasoning_effort": "low"},
            update_trace=False,
        ).extract(_NsfwResult, messages=[], prompt_vars={"message": message})
        if result.is_nsfw and result.confidence >= 0.75:
            if persona_id in _NSFW_BLOCKED_PERSONAS:
                logger.warning(
                    "NSFW blocked: persona=%s, confidence=%.2f",
                    persona_id,
                    result.confidence,
                )
                return _PreCheckOutcome(
                    is_blocked=True,
                    block_reason=BlockReason.NSFW_CONTENT,
                    detail=f"confidence={result.confidence}",
                )
            logger.info(
                "NSFW logged (pass): persona=%s, confidence=%.2f",
                persona_id,
                result.confidence,
            )
    except Exception as e:
        logger.error("NSFW check failed: %s", e)
    return _PreCheckOutcome()


async def _run_pre_audit(
    message_content: str, persona_id: str
) -> _PreCheckOutcome:
    """跑 4 个 pre-check（banned word + 3 个 LLM 并行），20s 超时 fail-open。

    跟旧 ``app/chat/safety.py:run_pre_check`` 行为一致。
    """
    # Fast path: banned word
    try:
        banned = await _check_banned_word(message_content)
        if banned:
            logger.warning("Banned word hit: %s", banned)
            return _PreCheckOutcome(
                is_blocked=True,
                block_reason=BlockReason.BANNED_WORD,
                detail=banned,
            )
    except Exception as e:
        logger.error("Banned word check failed: %s", e)

    try:
        results = await asyncio.wait_for(
            asyncio.gather(
                _check_injection(message_content),
                _check_politics(message_content),
                _check_nsfw(message_content, persona_id),
                return_exceptions=True,
            ),
            timeout=20.0,
        )
    except TimeoutError:
        logger.warning("Pre-check exceeded 20s, passing through")
        return _PreCheckOutcome()

    for r in results:
        if isinstance(r, _PreCheckOutcome) and r.is_blocked:
            return r
        if isinstance(r, Exception):
            logger.error("Pre-check sub-task failed: %s", r)

    return _PreCheckOutcome()


# ---------------------------------------------------------------------------
# Post-check helpers
# ---------------------------------------------------------------------------


async def _check_output(response_text: str) -> _PostAuditOutcome:
    """LLM output safety audit。"""
    try:
        result: _OutputSafetyResult = await Agent(
            _GUARD_OUTPUT,
            model_kwargs={"reasoning_effort": "low"},
            update_trace=False,
        ).extract(
            _OutputSafetyResult, messages=[], prompt_vars={"response": response_text}
        )
        if result.is_unsafe and result.confidence >= 0.7:
            logger.warning("Output unsafe: confidence=%.2f", result.confidence)
            return _PostAuditOutcome(
                is_blocked=True,
                reason="output_unsafe",
                detail=f"confidence={result.confidence}",
            )
    except Exception as e:
        logger.error("Output safety LLM check failed: %s", e)
    return _PostAuditOutcome()


async def _run_audit(response_text: str) -> _PostAuditOutcome:
    """跑 banned word + LLM output audit；fail-open（跟旧 run_post_check 一致）。"""
    if not response_text or not response_text.strip():
        return _PostAuditOutcome()

    # Step 1: banned word
    try:
        banned = await _check_banned_word(response_text)
        if banned:
            logger.warning("Output banned word hit: %s", banned)
            return _PostAuditOutcome(
                is_blocked=True, reason="output_banned_word", detail=banned
            )
    except Exception as e:
        logger.error("Output banned word check failed: %s", e)

    # Step 2: LLM audit
    return await _check_output(response_text)
