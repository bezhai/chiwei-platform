"""Banned-words capability — Phase 7d Gap 14, C5 cutover.

Wraps the ``banned_words`` Redis SET behind a domain function so business
nodes never reach into Redis directly. Single API:

    matched = await contains(text)
    if matched:
        ...  # block; ``matched`` is the offending word

Behavior preserved 1:1 from the original ``nodes/safety._check_banned_word``:
strip whitespace + lowercase before substring check.

C5: backed by ``RedisCapability``, which passes keys through verbatim: the
SET is read under the bare key ``banned_words`` on every lane, with no
``{lane}:`` prefix (the capability stopped adding one in the 2026-05-13
hotfix; see ``app.capabilities.redis``). Lanes see different blocklists
only when they connect to different Redis instances: a ``coe-*`` lane has
its own, a ``ppe-*`` lane reads prod's.
"""
from __future__ import annotations

from app.capabilities.redis import get_redis_capability

_KEY = "banned_words"


async def contains(text: str) -> str | None:
    """Return the matched banned word, or None if the text is clean.

    contract-allowed None (§4.8): "no match" is a business outcome, not a
    capability failure. Redis failures bubble up as typed
    ``CapabilityCallFailed`` / ``CapabilityTimeout`` (from the capability).
    """
    cap = await get_redis_capability()
    words = await cap.smembers(_KEY)
    if not words:
        return None
    normalized = text.replace(" ", "").lower()
    for word in words:
        if isinstance(word, bytes):
            word = word.decode("utf-8")
        if word in normalized:
            return word
    return None
