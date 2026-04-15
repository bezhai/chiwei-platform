"""Tests for chat_request MQ consumer idempotency."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.workers.chat_consumer import handle_chat_request

MODULE = "app.workers.chat_consumer"


class _ProcessContext:
    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _SessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeIncomingMessage:
    def __init__(self, payload: dict):
        self.body = json.dumps(payload).encode()
        self.process_requeue = None

    def process(self, *, requeue: bool):
        self.process_requeue = requeue
        return _ProcessContext()


def _payload(**overrides) -> dict:
    payload = {
        "session_id": "session-1",
        "message_id": "message-1",
        "chat_id": "chat-1",
        "is_p2p": False,
        "bot_name": "ayana",
        "mentions": [],
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_completed_redelivery_is_skipped():
    msg = FakeIncomingMessage(_payload())

    with (
        patch(f"{MODULE}.get_session", return_value=_SessionContext()),
        patch(
            f"{MODULE}.is_chat_request_completed",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_completed,
        patch(f"{MODULE}.MessageRouter") as mock_router,
        patch(f"{MODULE}._process_for_persona", new_callable=AsyncMock) as mock_process,
    ):
        await handle_chat_request(msg)

    assert msg.process_requeue is False
    mock_completed.assert_awaited_once()
    mock_router.assert_not_called()
    mock_process.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_request_is_processed():
    msg = FakeIncomingMessage(_payload())
    router = AsyncMock()
    router.route = AsyncMock(return_value=["ayana"])

    with (
        patch(f"{MODULE}.get_session", return_value=_SessionContext()),
        patch(
            f"{MODULE}.is_chat_request_completed",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(f"{MODULE}.MessageRouter", return_value=router),
        patch(f"{MODULE}._process_for_persona", new_callable=AsyncMock) as mock_process,
    ):
        await handle_chat_request(msg)

    assert msg.process_requeue is False
    router.route.assert_awaited_once()
    mock_process.assert_awaited_once()


@pytest.mark.asyncio
async def test_proactive_redelivery_uses_proactive_completion_check():
    msg = FakeIncomingMessage(_payload(is_proactive=True))

    with (
        patch(f"{MODULE}.get_session", return_value=_SessionContext()),
        patch(
            f"{MODULE}.is_chat_request_completed",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_completed,
        patch(f"{MODULE}.MessageRouter") as mock_router,
    ):
        await handle_chat_request(msg)

    _, _, kwargs = mock_completed.mock_calls[0]
    assert kwargs["is_proactive"] is True
    mock_router.assert_not_called()
