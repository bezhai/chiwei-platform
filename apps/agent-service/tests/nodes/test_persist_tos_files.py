"""Phase 6 v4 Gap 5: persist_tos_files_node end-to-end."""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.domain.chat_events import ConversationMessageContentSynced
from app.nodes.persist_tos_files import persist_tos_files_node


@asynccontextmanager
async def _session_cm(s):
    yield s


@pytest.mark.asyncio
async def test_persist_tos_files_skips_when_no_messages():
    """No messages -> no DB session opened."""
    fake_session = MagicMock()
    fake_session.commit = AsyncMock()

    with patch(
        "app.nodes.persist_tos_files.async_session",
        return_value=_session_cm(fake_session),
    ) as session_factory:
        await persist_tos_files_node(
            ConversationMessageContentSynced(
                message_id="m1",
                messages_json=[],
                image_key_to_file={},
            )
        )

    session_factory.assert_not_called()
    fake_session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_persist_tos_files_swallows_exceptions():
    """Internal failure logs but does not raise (Gap 5 fire-and-forget semantics)."""
    with patch(
        "app.nodes.persist_tos_files.async_session",
        side_effect=RuntimeError("db down"),
    ):
        # Must not raise.
        await persist_tos_files_node(
            ConversationMessageContentSynced(
                message_id="m1",
                messages_json=[
                    {
                        "message_id": "x",
                        "content": '{"v":2,"text":"[image:k1]","items":[{"type":"image","value":"k1"}]}',
                    }
                ],
                image_key_to_file={"k1": "tos_file_k1"},
            )
        )
