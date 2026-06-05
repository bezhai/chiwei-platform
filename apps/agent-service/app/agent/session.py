"""Agent session续接 store — a replay-able conversation transcript in PG.

An ``Agent.run(..., session_id=...)`` call is *stateful*: the run reads the
transcript stored under ``session_id``, prepends it so the model continues from
where it left off, and on completion appends this round's new messages back. The
transcript is a *durable* PG store (Data ``SessionTranscript``): a missing /
cleared row just means "记不太清刚才聊啥" — a cold start, never an error. Durable
PG (not Redis) so it survives pod restarts (意识流不丢), can be wiped with ops-db
for a clean cold-start verification, and can be SQL-queried to read back exactly
how she thought through a day.

What makes this correct is *losslessness* (decision 4): a stored message must
feed back to the model verbatim, including tool calls, tool results, and each
provider's private blobs (gemini ``thought_signature``). That is why we go
through ``Message.to_replay_dict`` / ``from_replay_dict`` rather than the
langfuse-facing ``to_dict`` (which drops the signature). The whole transcript is
serialised to a single JSON-text column (``transcript_json``) — the framework
persist layer cannot store list/dict fields, and a transcript is naturally one
opaque blob, so a TEXT column is the clean fit.

Storage: each ``append_session`` writes a new ``SessionTranscript`` version via
``insert_append`` (Version auto-increment, advisory-lock serialised per key);
``load_session`` reads the newest version via ``select_latest``. Old versions are
retained as durable history.

Concurrency: this module does NOT add its own lock. The read-modify-write is
correct only under the caller's "same session_id is called serially" guarantee
(world/life串行化 is the engines' job, not here).

Safety valve: a single transcript is capped on TWO axes that fire
whichever-first — ``TRANSCRIPT_MAX_MESSAGES`` (turn count) and
``TRANSCRIPT_MAX_BYTES`` (serialised size). Count alone is not enough: a single
long tool result or instruction can pin a huge value / blow the replay context
while the message count stays tiny. Past either cap we drop the oldest messages
(never silently — log.warning) so one runaway session can't pin a huge row or
blow the model's context.
"""

from __future__ import annotations

import json
import logging

from app.agent.neutral import Message, Role
from app.domain.session_transcript import SessionTranscript
from app.runtime.persist import insert_append, select_latest

logger = logging.getLogger(__name__)

# Safety valve (decision 2 "别炸"), axis 1 — message count. The model context
# budget is partly a function of how many turns it must re-read, and "drop the
# oldest few rounds" is naturally expressed in messages. world/life rounds are
# short (~user stimulus + assistant + a tool call/result pair ≈ a few messages),
# so 200 messages ≈ ~50 recent rounds — comfortably inside a one-hour session's
# "几百轮" while bounding the stored row and replay context. First刀不压缩;
# this only stops失控.
TRANSCRIPT_MAX_MESSAGES = 200

# Safety valve axis 2 — serialised bytes. Message count alone doesn't bound size:
# one long tool result (a big recall dump, a multi-KB sandbox stdout) or a long
# instruction can pin a multi-MB row and bloat the replay context while the
# message count stays tiny. We cap the serialised transcript at 256 KiB —
# generous next to a normal world/life round (a few KB), so it never trips on
# healthy traffic, yet small relative to the model's context budget, so a runaway
# long-result session is bounded. Like the count cap it drops oldest + logs; it
# never silently truncates. (Measured against the JSON we actually write —
# ``to_replay_dict`` + ``json.dumps`` UTF-8.)
TRANSCRIPT_MAX_BYTES = 256 * 1024


async def load_session(session_id: str) -> list[Message]:
    """Read the stored transcript, deserialised losslessly.

    A missing row (first唤醒 / cleared db) is a cold start: return ``[]`` so the
    caller continues from PG hard facts (decision 6), never an error. A corrupt
    value (should not happen — we write it) is treated the same way, logged,
    rather than crashing the run.
    """
    row = await select_latest(SessionTranscript, {"session_id": session_id})
    if row is None:
        return []
    try:
        payload = json.loads(row.transcript_json)
        return [Message.from_replay_dict(d) for d in payload]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.warning(
            "agent session %s transcript unreadable, cold-starting: %s",
            session_id,
            exc,
        )
        return []


async def append_session(session_id: str, new_messages: list[Message]) -> None:
    """Append this round's new messages and write the transcript back.

    Read-modify-write under the caller's serialisation guarantee. The combined
    transcript is capped (see ``_cap_transcript``), serialised losslessly, and
    appended as a new ``SessionTranscript`` version (``insert_append`` assigns the
    next ver and serialises concurrent writers per key with an advisory lock).
    """
    if not new_messages:
        return
    existing = await load_session(session_id)
    combined = _cap_transcript(existing + new_messages, session_id)
    transcript_json = json.dumps(
        [m.to_replay_dict() for m in combined], ensure_ascii=False
    )
    await insert_append(
        SessionTranscript(session_id=session_id, transcript_json=transcript_json)
    )


def _replay_bytes(messages: list[Message]) -> int:
    """Serialised UTF-8 byte size of the transcript, exactly as it lands in PG.

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
