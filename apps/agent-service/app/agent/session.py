"""Agent session续接 store — a replay-able conversation cache in Redis.

An ``Agent.run(..., session_id=...)`` call is *stateful*: the run reads the
transcript stored under ``session_id``, prepends it so the model continues from
where it left off, and on completion appends this round's new messages back. The
store is a working memory cache (decision 2): TTL 24h, hard facts live in PG, so
a lost/expired key just means "记不太清刚才聊啥" — a cold start, never an error.

What makes this correct is *losslessness* (decision 4): a stored message must
feed back to the model verbatim, including tool calls, tool results, and each
provider's private blobs (gemini ``thought_signature``). That is why we go
through ``Message.to_replay_dict`` / ``from_replay_dict`` rather than the
langfuse-facing ``to_dict`` (which drops the signature).

Concurrency: this module does NOT lock. The read-modify-write is correct only
under the caller's "same session_id is called serially" guarantee (spec 并发边界
— world/life串行化 is Task 2/3's job, not here).

Safety valve: a single transcript is capped on TWO axes that fire
whichever-first — ``TRANSCRIPT_MAX_MESSAGES`` (turn count) and
``TRANSCRIPT_MAX_BYTES`` (serialised size). Count alone is not enough: a single
long tool result or instruction can pin a huge Redis value / blow the replay
context while the message count stays tiny. Past either cap we drop the oldest
messages (never silently — log.warning) so one runaway session can't pin a huge
Redis value or blow the model's context.
"""

from __future__ import annotations

import json
import logging

from app.agent.neutral import Message, Role
from app.capabilities.redis import RedisCapability, get_redis_capability

logger = logging.getLogger(__name__)

# 24h working-memory window (decision 2). Refreshed on every successful write.
SESSION_TTL_SECONDS = 24 * 60 * 60

# Safety valve (decision 2 "别炸"), axis 1 — message count. The model context
# budget is partly a function of how many turns it must re-read, and "drop the
# oldest few rounds" is naturally expressed in messages. world/life rounds are
# short (~user stimulus + assistant + a tool call/result pair ≈ a few messages),
# so 200 messages ≈ ~50 recent rounds — comfortably inside a one-hour session's
# "几百轮" while bounding the Redis value and replay context. First刀不压缩;
# this only stops失控.
TRANSCRIPT_MAX_MESSAGES = 200

# Safety valve axis 2 — serialised bytes. Message count alone doesn't bound size:
# one long tool result (a big recall dump, a multi-KB sandbox stdout) or a long
# instruction can pin a multi-MB Redis value and bloat the replay context while
# the message count stays tiny. We cap the serialised transcript at 256 KiB —
# generous next to a normal world/life round (a few KB), so it never trips on
# healthy traffic, yet small relative to both a healthy Redis string value and
# the model's context budget, so a runaway long-result session is bounded. Like
# the count cap it drops oldest + logs; it never silently truncates. (Measured
# against the JSON we actually write — ``to_replay_dict`` + ``json.dumps`` UTF-8.)
TRANSCRIPT_MAX_BYTES = 256 * 1024

_KEY_PREFIX = "agent:session:"


def session_key(session_id: str) -> str:
    """Redis key for a session's transcript.

    Namespaced under ``agent:session:`` so it never collides with other Redis
    users (image_registry, debounce, ...). Keys pass through the capability
    verbatim — cross-lane isolation is the ConfigBundle's job (see
    ``capabilities/redis.py`` module docstring), not a key prefix.
    """
    return f"{_KEY_PREFIX}{session_id}"


async def _resolve_cap(cap: RedisCapability | None) -> RedisCapability:
    return cap if cap is not None else await get_redis_capability()


async def load_session(
    session_id: str, *, cap: RedisCapability | None = None
) -> list[Message]:
    """Read the stored transcript, deserialised losslessly.

    A missing key (expired / first唤醒 / Redis restart) is a cold start: return
    ``[]`` so the caller continues from PG hard facts (decision 6), never an
    error. A corrupt value (should not happen — we write it) is treated the same
    way, logged, rather than crashing the run.
    """
    redis = await _resolve_cap(cap)
    raw = await redis.get(session_key(session_id))
    if not raw:
        return []
    try:
        payload = json.loads(raw)
        return [Message.from_replay_dict(d) for d in payload]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.warning(
            "agent session %s transcript unreadable, cold-starting: %s",
            session_id,
            exc,
        )
        return []


async def append_session(
    session_id: str,
    new_messages: list[Message],
    *,
    cap: RedisCapability | None = None,
) -> None:
    """Append this round's new messages and write the transcript back.

    Read-modify-write under the caller's serialisation guarantee. The combined
    transcript is capped (see ``_cap_transcript``), serialised losslessly, and
    written with a refreshed 24h TTL so an active session never expires mid-life.
    """
    if not new_messages:
        return
    redis = await _resolve_cap(cap)
    existing = await load_session(session_id, cap=redis)
    combined = _cap_transcript(existing + new_messages, session_id)
    payload = json.dumps([m.to_replay_dict() for m in combined], ensure_ascii=False)
    await redis.set_with_ttl(
        session_key(session_id), payload, ttl_seconds=SESSION_TTL_SECONDS
    )


def _replay_bytes(messages: list[Message]) -> int:
    """Serialised UTF-8 byte size of the transcript, exactly as it lands in Redis.

    Measures the same ``to_replay_dict`` + ``json.dumps`` form ``append_session``
    writes, so the byte cap bounds the *actual* stored value, not an approximation.
    """
    return len(
        json.dumps(
            [m.to_replay_dict() for m in messages], ensure_ascii=False
        ).encode("utf-8")
    )


def _cap_transcript(messages: list[Message], session_id: str) -> list[Message]:
    """Trim to fit BOTH the message-count and byte caps, dropping oldest.

    Two caps fire whichever-first (spec safety valve):
      * ``TRANSCRIPT_MAX_MESSAGES`` — at most this many turns,
      * ``TRANSCRIPT_MAX_BYTES`` — the serialised value stays under this size.

    Three rules on the trim:
      * keep the newest messages (recency matters for续接),
      * never start the kept transcript on an orphaned TOOL result — a tool
        message whose ASSISTANT tool-call request was dropped is rejected by
        providers. Advance the cut forward to the next non-TOOL boundary,
      * never trim to empty: if a single (newest) message already exceeds the
        byte cap we keep it rather than lose this whole round — the cap bounds
        runaway *accumulation*, it is not a guillotine on one legitimately large
        turn.

    Dropping is logged (never silent) so an oversized session is observable.
    """
    over_count = len(messages) > TRANSCRIPT_MAX_MESSAGES
    over_bytes = _replay_bytes(messages) > TRANSCRIPT_MAX_BYTES
    if not over_count and not over_bytes:
        return messages

    # Start from the count-driven cut, then advance further until the kept tail
    # also fits the byte cap. Both caps drop from the oldest end.
    cut = max(0, len(messages) - TRANSCRIPT_MAX_MESSAGES)
    # never drop the single newest message (that would risk an empty transcript)
    while cut < len(messages) - 1 and _replay_bytes(messages[cut:]) > TRANSCRIPT_MAX_BYTES:
        cut += 1
    # advance past any leading TOOL results so we don't begin on an orphan
    while cut < len(messages) - 1 and messages[cut].role == Role.TOOL:
        cut += 1
    kept = messages[cut:]
    logger.warning(
        "agent session %s transcript hit cap (%d msgs / %d bytes); dropped %d "
        "oldest, kept %d msgs / %d bytes",
        session_id,
        len(messages),
        _replay_bytes(messages),
        cut,
        len(kept),
        _replay_bytes(kept),
    )
    return kept
