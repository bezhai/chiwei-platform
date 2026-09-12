"""Transcript store — a replay-able ``Message`` sequence in durable PG.

Two operations over Data :class:`~app.domain.session_transcript.SessionTranscript`:
:func:`load_session` reads the newest stored version, :func:`replace_session`
writes a new one. Nothing else — the store holds what it is handed, in the order
it was handed, and hands it back verbatim.

**Losslessness is the whole point.** A stored message must feed back to the model
byte-for-byte, tool calls, tool results and each provider's private blobs (gemini
``thought_signature``) included, so serialisation goes through
``Message.to_replay_dict`` / ``from_replay_dict`` rather than the langfuse-facing
``to_dict`` (which drops the signature). The whole transcript is one JSON text
column (``transcript_json``): a transcript is naturally one opaque blob and a TEXT
column is the clean fit.

**No trimming here.** Trimming is a policy decision about what she keeps and for
how long, and it lives in exactly one place — :mod:`app.living.continuity`. A
second regime in this layer would silently drop messages the policy meant to keep,
and it would do so behind the caller's back (the returned value would look fine).
So the caps this module used to apply are gone; the caller decides what the full
new transcript is and writes that.

**Every write is a CAS.** :func:`replace_session` takes the ``expected_ver``
returned by :func:`load_session` and only lands when the stored version is still
that one. Under the caller's serialisation guarantee the check never fires; when it
does fire, the guarantee is broken (a second process, a bypassed lock) and the
caller must treat it as a failure rather than let one writer's whole transcript be
overwritten by another's.

Old versions are retained as durable history — every round leaves a full copy, so a
day's thinking can be read back with SQL.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.agent.neutral import Message
from app.domain.session_transcript import SessionTranscript
from app.runtime.persist import insert_append, select_latest

logger = logging.getLogger(__name__)


async def load_session(session_id: str) -> tuple[list[Message], int]:
    """The newest stored transcript plus the version it was read at.

    A missing row (first wake / cleared db) is a cold start: ``([], 0)``, never an
    error. Version ``0`` is the base ``insert_append`` compares against, so a cold
    start's write lands as version 1.

    A corrupt value (should not happen — we write it) is logged and also read as a
    cold start, but reports the row's real version: the caller is looking at *that*
    version, however unreadable its payload, and must CAS against it.
    """
    row = await select_latest(SessionTranscript, {"session_id": session_id})
    if row is None:
        return [], 0
    try:
        payload = json.loads(row.transcript_json)
        return [Message.from_replay_dict(d) for d in payload], row.ver
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.warning(
            "agent session %s transcript unreadable, cold-starting: %s",
            session_id,
            exc,
        )
        return [], row.ver


async def replace_session(
    session_id: str,
    messages: list[Message],
    *,
    expected_ver: int,
    session: Any = None,
) -> bool:
    """Write ``messages`` as the next version; report whether it landed.

    The caller has already computed the full replacement (this is not a
    read-modify-write). ``expected_ver`` is the version :func:`load_session`
    returned: the write only lands if the stored version is still exactly that,
    judged inside the INSERT itself. ``False`` means somebody wrote in between —
    nothing was written, and what that means is the caller's call.

    ``session`` runs the write on the caller's ``AsyncSession`` so it commits
    atomically with whatever else that transaction did.

    Empty ``messages`` is rejected: it would store an empty transcript, i.e. wipe
    her context, and no caller ever means that.
    """
    if not messages:
        raise ValueError(
            f"session {session_id}: refusing to store an empty transcript — "
            f"that erases her context, and no caller means it"
        )
    transcript_json = json.dumps(
        [m.to_replay_dict() for m in messages], ensure_ascii=False
    )
    written = await insert_append(
        SessionTranscript(session_id=session_id, transcript_json=transcript_json),
        expected_current_ver=expected_ver,
        session=session,
    )
    return written == 1
