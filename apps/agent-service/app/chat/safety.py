"""Pre-check and post-check safety pipeline.

Pre-check (input):
  1. Banned word (fast, no LLM)
  2. Prompt injection detection (LLM, structured output)
  3. Sensitive politics detection (LLM, structured output)
  4. NSFW content detection (LLM, persona-aware block/log)

Post-check (output):
  1. Banned word on response text
  2. LLM output audit (structured output)

All checks follow fail-open: on error, the message passes through.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, Field

from app.agent.core import Agent
from app.agent.prompts import get_prompt
from app.infra.redis import get_redis

logger = logging.getLogger(__name__)

# Redis key for banned words set
_BANNED_WORDS_KEY = "banned_words"

# Personas that block NSFW content (minors)
_NSFW_BLOCKED_PERSONAS = frozenset({"ayana"})


# ---------------------------------------------------------------------------
# Block reason enum
# ---------------------------------------------------------------------------


class BlockReason(StrEnum):
    BANNED_WORD = "banned_word"
    PROMPT_INJECTION = "prompt_injection"
    SENSITIVE_POLITICS = "sensitive_politics"
    NSFW_CONTENT = "nsfw_content"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class PreCheckResult:
    """Aggregated pre-safety result."""

    is_blocked: bool = False
    block_reason: BlockReason | None = None
    detail: str | None = None


@dataclass
class PostCheckResult:
    """Post-safety audit result."""

    blocked: bool = False
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


async def _check_injection(message: str) -> PreCheckResult:
    try:
        prompt = get_prompt("guard_prompt_injection")
        messages = prompt.compile(message=message)
        result: _InjectionResult = await Agent(
            "guard-injection", model_kwargs={"reasoning_effort": "low"}
        ).extract(_InjectionResult, messages=messages)
        if result.is_injection and result.confidence >= 0.85:
            logger.warning(
                "Prompt injection detected: confidence=%.2f", result.confidence
            )
            return PreCheckResult(
                is_blocked=True,
                block_reason=BlockReason.PROMPT_INJECTION,
                detail=f"confidence={result.confidence}",
            )
    except Exception as e:
        logger.error("Injection check failed: %s", e)
    return PreCheckResult()


async def _check_politics(message: str) -> PreCheckResult:
    try:
        prompt = get_prompt("guard_sensitive_politics")
        messages = prompt.compile(message=message)
        result: _PoliticsResult = await Agent(
            "guard-politics", model_kwargs={"reasoning_effort": "low"}
        ).extract(_PoliticsResult, messages=messages)
        if result.is_sensitive and result.confidence >= 0.85:
            logger.warning(
                "Sensitive politics detected: confidence=%.2f", result.confidence
            )
            return PreCheckResult(
                is_blocked=True,
                block_reason=BlockReason.SENSITIVE_POLITICS,
                detail=f"confidence={result.confidence}",
            )
    except Exception as e:
        logger.error("Politics check failed: %s", e)
    return PreCheckResult()


async def _check_nsfw(message: str, persona_id: str) -> PreCheckResult:
    try:
        prompt = get_prompt("guard_nsfw_content")
        messages = prompt.compile(message=message)
        result: _NsfwResult = await Agent(
            "guard-nsfw", model_kwargs={"reasoning_effort": "low"}
        ).extract(_NsfwResult, messages=messages)
        if result.is_nsfw and result.confidence >= 0.75:
            if persona_id in _NSFW_BLOCKED_PERSONAS:
                logger.warning(
                    "NSFW blocked: persona=%s, confidence=%.2f",
                    persona_id,
                    result.confidence,
                )
                return PreCheckResult(
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
    return PreCheckResult()


# ---------------------------------------------------------------------------
# Public pre-check entry point
# ---------------------------------------------------------------------------


async def run_pre_check(message_content: str, persona_id: str = "") -> PreCheckResult:
    """Run all pre-safety checks in parallel.

    Banned word is synchronous-fast, LLM checks run concurrently.
    First blocked result wins.
    """
    # Fast path: banned word
    try:
        banned = await _check_banned_word(message_content)
        if banned:
            logger.warning("Banned word hit: %s", banned)
            return PreCheckResult(
                is_blocked=True,
                block_reason=BlockReason.BANNED_WORD,
                detail=banned,
            )
    except Exception as e:
        logger.error("Banned word check failed: %s", e)

    # LLM checks in parallel
    results = await asyncio.gather(
        _check_injection(message_content),
        _check_politics(message_content),
        _check_nsfw(message_content, persona_id),
        return_exceptions=True,
    )

    for r in results:
        if isinstance(r, PreCheckResult) and r.is_blocked:
            return r
        if isinstance(r, Exception):
            logger.error("Pre-check sub-task failed: %s", r)

    return PreCheckResult()


# ---------------------------------------------------------------------------
# Public post-check entry point
# ---------------------------------------------------------------------------


async def run_post_check(response_text: str) -> PostCheckResult:
    """Audit AI-generated response for safety.

    1. Banned word (fast)
    2. LLM output audit (structured output)

    Fail-open: errors result in pass-through.
    """
    if not response_text or not response_text.strip():
        return PostCheckResult()

    # Step 1: banned word
    try:
        banned = await _check_banned_word(response_text)
        if banned:
            logger.warning("Output banned word hit: %s", banned)
            return PostCheckResult(
                blocked=True, reason="output_banned_word", detail=banned
            )
    except Exception as e:
        logger.error("Output banned word check failed: %s", e)

    # Step 2: LLM audit
    try:
        prompt = get_prompt("guard_output_safety")
        messages = prompt.compile(response=response_text)
        result: _OutputSafetyResult = await Agent(
            "guard-output", model_kwargs={"reasoning_effort": "low"}
        ).extract(_OutputSafetyResult, messages=messages)
        if result.is_unsafe and result.confidence >= 0.7:
            logger.warning("Output unsafe: confidence=%.2f", result.confidence)
            return PostCheckResult(
                blocked=True,
                reason="output_unsafe",
                detail=f"confidence={result.confidence}",
            )
    except Exception as e:
        logger.error("Output safety LLM check failed: %s", e)

    return PostCheckResult()
