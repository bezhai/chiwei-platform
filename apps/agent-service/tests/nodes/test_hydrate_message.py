"""hydrate_message @node: MessageRequest -> Message | None.

Two branches to cover:
  * row present in pg -> returns ``Message`` with every field pass-through
    via ``Message.from_cm`` (same mapping as ``emit_legacy_message``);
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

from app.data.models import ConversationMessage
from app.domain.message import Message
from app.domain.message_request import MessageRequest

# Force-load the submodule so ``hydrate_mod.hydrate_message`` refers to
# the function object without going through the ``app.nodes`` package
# re-export.
hydrate_mod = importlib.import_module("app.nodes.hydrate_message")


def _stub_session():
    @asynccontextmanager
    async def _cm():
        yield AsyncMock()

    return _cm


def _cm() -> ConversationMessage:
    """Full 13-field CM so we can assert every field is carried through."""
    return ConversationMessage(
        message_id="m1",
        user_id="u1",
        content="hello",
        role="user",
        root_message_id="r1",
        reply_message_id=None,
        chat_id="c1",
        chat_type="p2p",
        create_time=1234567890,
        message_type="text",
        vector_status="pending",
        bot_name=None,
        response_id=None,
    )


@pytest.mark.asyncio
async def test_hydrates_existing_message():
    with patch(
        "app.nodes.hydrate_message.get_session", _stub_session()
    ), patch(
        "app.nodes.hydrate_message.find_message_by_id",
        new_callable=AsyncMock,
        return_value=_cm(),
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
    assert msg.vector_status == "pending"
    assert msg.bot_name is None
    assert msg.response_id is None


@pytest.mark.asyncio
async def test_missing_message_returns_none():
    with patch(
        "app.nodes.hydrate_message.get_session", _stub_session()
    ), patch(
        "app.nodes.hydrate_message.find_message_by_id",
        new_callable=AsyncMock,
        return_value=None,
    ):
        result = await hydrate_mod.hydrate_message(MessageRequest(message_id="missing"))

    assert result is None
