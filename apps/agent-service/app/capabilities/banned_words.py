"""Banned-words capability — Phase 7d Gap 14, C5 cutover.

Wraps the ``banned_words`` Redis SET behind a domain function so business
nodes never reach into Redis directly. Single API:

    matched = await contains(text)
    if matched:
        ...  # block; ``matched`` is the offending word

Behavior preserved 1:1 from the original ``nodes/safety._check_banned_word``:
strip whitespace + lowercase before substring check.

C5: backed by ``RedisCapability`` so the ``banned_words`` SET key
auto-prefixes with ``{lane}:`` on non-prod lanes (test lanes don't see
prod's blocklist, and writes from one lane don't leak into another).
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
