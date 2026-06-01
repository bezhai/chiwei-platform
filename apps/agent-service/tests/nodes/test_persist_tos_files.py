"""Phase 6 v4 Gap 5: persist_tos_files_node end-to-end."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.domain.chat_events import CommonMessageContentSynced
from app.nodes.persist_tos_files import persist_tos_files_node


@pytest.mark.asyncio
async def test_persist_tos_files_skips_when_no_messages():
    """No messages -> no DB query issued."""
    with patch(
        "app.nodes.persist_tos_files.update_messages_tos_files",
        new=AsyncMock(return_value=0),
    ) as update_mock:
        await persist_tos_files_node(
            CommonMessageContentSynced(
                message_id="m1",
                messages_json=[],
                image_key_to_file={},
            )
        )

    update_mock.assert_not_called()


@pytest.mark.asyncio
async def test_persist_tos_files_swallows_exceptions():
    """Internal failure logs but does not raise (Gap 5 fire-and-forget semantics)."""
    with patch(
        "app.nodes.persist_tos_files.update_messages_tos_files",
        new=AsyncMock(side_effect=RuntimeError("db down")),
    ):
        # Must not raise.
        await persist_tos_files_node(
            CommonMessageContentSynced(
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
