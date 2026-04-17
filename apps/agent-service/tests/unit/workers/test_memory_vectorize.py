"""Test memory_vectorize queue consumer routing."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.workers.vectorize import handle_memory_vectorize


def _make_message(payload: dict) -> MagicMock:
    msg = MagicMock()
    msg.body = json.dumps(payload).encode()
    msg.process = MagicMock()
    msg.process.return_value.__aenter__ = AsyncMock()
    msg.process.return_value.__aexit__ = AsyncMock()
    return msg


@pytest.mark.asyncio
async def test_handle_memory_fragment_task_calls_vectorize_fragment():
    msg = _make_message({"kind": "fragment", "id": "f_1"})
    with patch("app.workers.vectorize.vectorize_fragment", new=AsyncMock(return_value=True)) as vf:
        await handle_memory_vectorize(msg)
    vf.assert_awaited_once_with("f_1")


@pytest.mark.asyncio
async def test_handle_memory_abstract_task_calls_vectorize_abstract():
    msg = _make_message({"kind": "abstract", "id": "a_1"})
    with patch("app.workers.vectorize.vectorize_abstract", new=AsyncMock(return_value=True)) as va:
        await handle_memory_vectorize(msg)
    va.assert_awaited_once_with("a_1")


@pytest.mark.asyncio
async def test_handle_memory_missing_id_logs_and_returns():
    msg = _make_message({"kind": "fragment"})  # no id
    # should not raise, should not call vectorize
    with patch("app.workers.vectorize.vectorize_fragment", new=AsyncMock()) as vf:
        await handle_memory_vectorize(msg)
    vf.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_memory_unknown_kind_logs_and_returns():
    msg = _make_message({"kind": "???", "id": "x"})
    with patch("app.workers.vectorize.vectorize_fragment", new=AsyncMock()) as vf:
        with patch("app.workers.vectorize.vectorize_abstract", new=AsyncMock()) as va:
            await handle_memory_vectorize(msg)
    vf.assert_not_awaited()
    va.assert_not_awaited()
