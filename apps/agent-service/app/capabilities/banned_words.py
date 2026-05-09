"""Banned-words capability — Phase 7d Gap 14.

Wraps the ``banned_words`` Redis SET behind a domain function so business
nodes never reach into Redis directly. Single API:

    matched = await contains(text)
    if matched:
        ...  # block; ``matched`` is the offending word

Behavior preserved 1:1 from the original ``nodes/safety._check_banned_word``:
strip whitespace + lowercase before substring check.
"""
from __future__ import annotations

from app.infra.redis import get_redis

_KEY = "banned_words"


async def contains(text: str) -> str | None:
    """Return the matched banned word, or None if the text is clean."""
    redis = await get_redis()
    words = await redis.smembers(_KEY)
    if not words:
        return None
    normalized = text.replace(" ", "").lower()
    for word in words:
        if isinstance(word, bytes):
            word = word.decode("utf-8")
        if word in normalized:
            return word
    return None
