"""Tests for app.chat.safety — pre-check and post-check.

Covers:
  - Banned word detection (pre + post)
  - Pre-check result aggregation (first blocked wins)
  - Empty/blank text handling in post-check
  - BlockReason enum values
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.chat.safety import (
    BlockReason,
    PreCheckResult,
    _check_banned_word,
    run_post_check,
    run_pre_check,
)

# ---------------------------------------------------------------------------
# Banned word tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_banned_word_hit():
    mock_redis = AsyncMock()
    mock_redis.smembers.return_value = {"badword", "evil"}

    with patch("app.chat.safety.get_redis", return_value=mock_redis):
        result = await _check_banned_word("this contains badword here")
        assert result == "badword"


@pytest.mark.asyncio
async def test_banned_word_miss():
    mock_redis = AsyncMock()
    mock_redis.smembers.return_value = {"badword", "evil"}

    with patch("app.chat.safety.get_redis", return_value=mock_redis):
        result = await _check_banned_word("this is clean")
        assert result is None


@pytest.mark.asyncio
async def test_banned_word_empty_set():
    mock_redis = AsyncMock()
    mock_redis.smembers.return_value = set()

    with patch("app.chat.safety.get_redis", return_value=mock_redis):
        result = await _check_banned_word("anything")
        assert result is None


@pytest.mark.asyncio
async def test_banned_word_case_insensitive():
    mock_redis = AsyncMock()
    mock_redis.smembers.return_value = {"badword"}

    with patch("app.chat.safety.get_redis", return_value=mock_redis):
        result = await _check_banned_word("BADWORD is here")
        assert result == "badword"


@pytest.mark.asyncio
async def test_banned_word_ignores_spaces():
    mock_redis = AsyncMock()
    mock_redis.smembers.return_value = {"badword"}

    with patch("app.chat.safety.get_redis", return_value=mock_redis):
        result = await _check_banned_word("b a d w o r d")
        assert result == "badword"


# ---------------------------------------------------------------------------
# Pre-check tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_check_banned_word_blocks():
    """Banned word in pre-check should block immediately."""
    mock_redis = AsyncMock()
    mock_redis.smembers.return_value = {"forbidden"}

    with patch("app.chat.safety.get_redis", return_value=mock_redis):
        result = await run_pre_check("this is forbidden content")

    assert result.is_blocked
    assert result.block_reason == BlockReason.BANNED_WORD
    assert result.detail == "forbidden"


@pytest.mark.asyncio
async def test_pre_check_clean_message():
    """Clean message should pass all checks."""
    mock_redis = AsyncMock()
    mock_redis.smembers.return_value = set()

    with (
        patch("app.chat.safety.get_redis", return_value=mock_redis),
        patch("app.chat.safety._check_injection", return_value=PreCheckResult()),
        patch("app.chat.safety._check_politics", return_value=PreCheckResult()),
        patch("app.chat.safety._check_nsfw", return_value=PreCheckResult()),
    ):
        result = await run_pre_check("hello how are you")

    assert not result.is_blocked
    assert result.block_reason is None


@pytest.mark.asyncio
async def test_pre_check_llm_failure_passes():
    """LLM check failure should not block (fail-open)."""
    mock_redis = AsyncMock()
    mock_redis.smembers.return_value = set()

    with (
        patch("app.chat.safety.get_redis", return_value=mock_redis),
        patch("app.chat.safety._check_injection", side_effect=RuntimeError("boom")),
        patch("app.chat.safety._check_politics", return_value=PreCheckResult()),
        patch("app.chat.safety._check_nsfw", return_value=PreCheckResult()),
    ):
        result = await run_pre_check("test message")

    assert not result.is_blocked


# ---------------------------------------------------------------------------
# Post-check tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_check_empty_text():
    result = await run_post_check("")
    assert not result.blocked

    result = await run_post_check("   ")
    assert not result.blocked


@pytest.mark.asyncio
async def test_post_check_banned_word_blocks():
    mock_redis = AsyncMock()
    mock_redis.smembers.return_value = {"forbidden"}

    with patch("app.chat.safety.get_redis", return_value=mock_redis):
        result = await run_post_check("the response contains forbidden content")

    assert result.blocked
    assert result.reason == "output_banned_word"


@pytest.mark.asyncio
async def test_post_check_clean_text():
    mock_redis = AsyncMock()
    mock_redis.smembers.return_value = set()

    with (
        patch("app.chat.safety.get_redis", return_value=mock_redis),
        patch("app.chat.safety.Agent") as mock_agent_cls,
    ):
        mock_agent = MagicMock()
        mock_agent_cls.return_value = mock_agent
        mock_result = MagicMock()
        mock_result.is_unsafe = False
        mock_result.confidence = 0.1
        mock_agent.extract = AsyncMock(return_value=mock_result)

        result = await run_post_check("this is a safe response")

    assert not result.blocked


# ---------------------------------------------------------------------------
# BlockReason enum
# ---------------------------------------------------------------------------


class TestBlockReason:
    def test_values(self):
        assert BlockReason.BANNED_WORD == "banned_word"
        assert BlockReason.PROMPT_INJECTION == "prompt_injection"
        assert BlockReason.SENSITIVE_POLITICS == "sensitive_politics"
        assert BlockReason.NSFW_CONTENT == "nsfw_content"

    def test_all_are_str(self):
        for reason in BlockReason:
            assert isinstance(reason, str)
