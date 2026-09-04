"""Safety pipeline @nodes + private helpers (Phase 2).

- module-level 私有 helpers：3 个 pre-check LLM 检查 + ``_run_pre_audit``
- module-level enum / config：``BlockReason`` / ``_GUARD_*``
- @node：``run_pre_safety`` / ``run_post_safety``
- 常量：``TERMINAL_STATUSES``

**判一段输出安不安全那一块不在这里**，它在 :mod:`app.capabilities.output_safety`：
她自己开口那条链（生活引擎的嘴）在**发出去之前**判同一件事，两条链共用一份判据。
留在这个模块里就意味着另一条链要么 import 一个 ``_`` 开头的私有函数、要么再写一份。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from app.agent.core import Agent, AgentConfig
from app.agent.trace import turn_trace
from app.api.middleware import get_lane
from app.capabilities import banned_words
from app.capabilities.concurrency import fan_out_wait
from app.capabilities.output_safety import audit_output
from app.data.queries import get_safety_status, set_safety_status
from app.domain.safety import (
    PostSafetyRequest,
    PreSafetyRequest,
    PreSafetyVerdict,
    Recall,
)
from app.runtime import node

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Post-safety 节点入口的"已完成"短路集合（Phase 2 §3.2 / §4.4）。
# - passed / blocked: agent-service 写的（"blocked" 是迁移期遗留瞬态）
# - recalled / recall_failed: 消费 recall_{channel} 的渠道服务写的终态
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"passed", "blocked", "recalled", "recall_failed"}
)

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
        banned = await banned_words.contains(message_content)
        if banned:
            logger.warning("Banned word hit: %s", banned)
            return _PreCheckOutcome(
                is_blocked=True,
                block_reason=BlockReason.BANNED_WORD,
                detail=banned,
            )
    except Exception as e:
        logger.error("Banned word check failed: %s", e)

    # fan_out_wait cancels in-flight checks on deadline trip (improvement
    # over the legacy ``wait_for(gather(...))`` which leaked the slow
    # task). Slow checks surface as TimeoutError in their result slot;
    # fail-open keeps the original pass-through verdict.
    results = await fan_out_wait(
        [
            _check_injection(message_content),
            _check_politics(message_content),
            _check_nsfw(message_content, persona_id),
        ],
        timeout_s=20.0,
    )

    if any(isinstance(r, TimeoutError) for r in results):
        logger.warning("Pre-check exceeded 20s, passing through")

    for r in results:
        if isinstance(r, _PreCheckOutcome) and r.is_blocked:
            return r
        if isinstance(r, Exception) and not isinstance(r, TimeoutError):
            logger.error("Pre-check sub-task failed: %s", r)

    return _PreCheckOutcome()


# ---------------------------------------------------------------------------
# Public @node entries
# ---------------------------------------------------------------------------


@node
async def run_post_safety(req: PostSafetyRequest) -> Recall | None:
    """Audit + 决定是否撤回，单节点完成（Phase 2 §3.2）。

    幂等用 ``safety_status`` 短路：
      - row 不存在 → raise → DLQ（channel-server INSERT 链路问题）
      - 已 ``TERMINAL_STATUSES``（passed/blocked/recalled/recall_failed） → return None
      - pending → 跑 audit；blocked 路径 return Recall（@node 自动 emit -> sink），
        passed 路径写 status="passed"
    blocked 路径**不写 status**——撤回的消费方会写最终 recalled / recall_failed，
    避免 race（spec §3.2）。
    """
    current = await get_safety_status(req.session_id)
    if current is None:
        raise RuntimeError(
            f"common_agent_response row missing for session_id={req.session_id}; "
            f"channel-server must INSERT before agent-service emits "
            f"PostSafetyRequest"
        )
    if current in TERMINAL_STATUSES:
        logger.info(
            "post safety short-circuit: session_id=%s already %s",
            req.session_id, current,
        )
        return None

    # 事后审计这条链上不给期限：它跑在回复发出去之后的后台节点里，慢一点只是慢一
    # 点，没有人在等它。（她自己开口那条链在一缝里同步等，必须给期限。）
    verdict = await audit_output(req.response_text)
    checked_at = datetime.now(UTC).isoformat()

    if not verdict.ok:
        return Recall(
            session_id=req.session_id,
            channel=req.channel,
            chat_id=req.chat_id,
            trigger_message_id=req.trigger_message_id,
            reason=verdict.reason or "unknown",
            detail=verdict.detail,
            lane=get_lane(),
        )

    # ``checked`` 一起落库：这一关坏掉时是 fail-open 放行的，状态照样写 "passed"，
    # 但那条其实没判过。不记下来的话，"那段时间漏了多少"事后答不出来。
    await set_safety_status(
        req.session_id,
        "passed",
        {"checked_at": checked_at, "checked": verdict.checked},
    )
    return None


@node
async def run_pre_safety(req: PreSafetyRequest) -> PreSafetyVerdict:
    """跑 4 个并行 pre-check，返回 verdict.

    内部调 ``_run_pre_audit`` 复用 banned word + 3 个 LLM 检查；
    fail-open 已在 audit 内部处理（超时 / 异常 → 通过 verdict）。
    """
    # Same turn seed as the main stream (chat_node's turn_trace around
    # render_chat_turn) so the 3 pre-check guard spans land in this turn's
    # langfuse trace, not 3 separate top-level traces.
    with turn_trace(f"{req.message_id}:{req.persona_id}"):
        outcome = await _run_pre_audit(req.message_content, req.persona_id)
    return PreSafetyVerdict(
        pre_request_id=req.pre_request_id,
        message_id=req.message_id,
        is_blocked=outcome.is_blocked,
        block_reason=str(outcome.block_reason) if outcome.block_reason else None,
        detail=outcome.detail,
    )


# B1: ``resolve_pre_safety_waiter`` removed. The PreSafetyVerdict auto-
# emitted by ``run_pre_safety`` is now picked up generically by
# ``emit_and_wait``'s notify() hook in app/runtime/emit.py — no
# dedicated reply-side node is required.
