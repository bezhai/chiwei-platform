"""hydrate_message @node: MessageRequest -> Message | None.

Two branches to cover:
  * common row present in pg -> returns ``Message`` with every field
    pass-through via ``Message.from_record``;
  * row missing -> returns ``None`` (runtime drops None before the
    durable edge, so the next node never sees a stale request).

The DB session context manager and ``find_message_by_id`` are patched at
the ``app.nodes.hydrate_message`` namespace (where the names are bound)
so we don't need a real postgres.
"""
from __future__ import annotations

import importlib
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from app.data.message_record import CommonMessageRecord
from app.domain.message import Message
from app.domain.message_request import MessageRequest

# Force-load the submodule so ``hydrate_mod.hydrate_message`` refers to
# the function object without going through the ``app.nodes`` package
# re-export.
hydrate_mod = importlib.import_module("app.nodes.hydrate_message")


@asynccontextmanager
async def _fake_tx():
    yield


def _record() -> CommonMessageRecord:
    """Full common message record so every field pass-through is covered."""
    return CommonMessageRecord(
        message_id="m1",
        user_id="u1",
        username="alice",
        content="hello",
        role="user",
        root_message_id="r1",
        reply_message_id=None,
        chat_id="c1",
        chat_type="p2p",
        create_time=1234567890,
        message_type="text",
        bot_name=None,
        response_id=None,
    )


@pytest.mark.asyncio
async def test_hydrates_existing_message():
    with patch(
        "app.nodes.hydrate_message.find_message_by_id",
        new_callable=AsyncMock,
        return_value=_record(),
    ):
        msg = await hydrate_mod.hydrate_message(MessageRequest(message_id="m1"))

    assert isinstance(msg, Message)
    assert msg.message_id == "m1"
    assert msg.user_id == "u1"
    assert msg.content == "hello"
    assert msg.role == "user"
    assert msg.root_message_id == "r1"
    assert msg.reply_message_id is None
    assert msg.chat_id == "c1"
    assert msg.chat_type == "p2p"
    assert msg.create_time == 1234567890
    assert msg.message_type == "text"
    assert msg.bot_name is None
    assert msg.response_id is None


@pytest.mark.asyncio
async def test_missing_message_returns_none():
    with patch(
        "app.nodes.hydrate_message.find_message_by_id",
        new_callable=AsyncMock,
        return_value=None,
    ):
        result = await hydrate_mod.hydrate_message(MessageRequest(message_id="missing"))

    assert result is None
