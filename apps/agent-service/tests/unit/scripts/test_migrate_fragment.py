"""Test migrate_fragment_to_fragment core logic."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from scripts.migrate_fragment_to_fragment import copy_one_row


@pytest.mark.asyncio
async def test_copy_one_row_preserves_timestamps():
    row = SimpleNamespace(
        id=42,
        persona_id="chiwei",
        content="啊今天和浩南聊了很久",
        source_chat_id="oc_xxx",
        created_at=datetime(2026, 4, 11, 10, 0, tzinfo=UTC),
    )
    with patch("scripts.migrate_fragment_to_fragment.insert_fragment", new=AsyncMock()) as ins:
        with patch("scripts.migrate_fragment_to_fragment.enqueue_fragment_vectorize", new=AsyncMock()):
            with patch("scripts.migrate_fragment_to_fragment.get_session") as mock_get_sess:
                mock_get_sess.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
                mock_get_sess.return_value.__aexit__ = AsyncMock(return_value=None)
                ok = await copy_one_row(row, dry_run=False)
    assert ok is True
    call_kwargs = ins.await_args.kwargs
    assert call_kwargs["id"] == "f_mig_42"
    assert call_kwargs["source"] == "afterthought"
    assert call_kwargs["chat_id"] == "oc_xxx"
    assert call_kwargs["created_at"] == row.created_at


@pytest.mark.asyncio
async def test_copy_one_row_dry_run_skips_write():
    row = SimpleNamespace(
        id=1,
        persona_id="chiwei",
        content="x",
        source_chat_id="oc_a",
        created_at=datetime(2026, 4, 11, tzinfo=UTC),
    )
    with patch("scripts.migrate_fragment_to_fragment.insert_fragment", new=AsyncMock()) as ins:
        with patch("scripts.migrate_fragment_to_fragment.enqueue_fragment_vectorize", new=AsyncMock()) as enq:
            ok = await copy_one_row(row, dry_run=True)
    assert ok is True
    ins.assert_not_awaited()
    enq.assert_not_awaited()


@pytest.mark.asyncio
async def test_copy_one_row_failure_returns_false():
    row = SimpleNamespace(
        id=1, persona_id="chiwei", content="x",
        source_chat_id="oc_a", created_at=datetime(2026, 4, 11, tzinfo=UTC),
    )
    with patch("scripts.migrate_fragment_to_fragment.insert_fragment", new=AsyncMock(side_effect=RuntimeError("db down"))):
        with patch("scripts.migrate_fragment_to_fragment.get_session") as mock_get_sess:
            mock_get_sess.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
            mock_get_sess.return_value.__aexit__ = AsyncMock(return_value=None)
            ok = await copy_one_row(row, dry_run=False)
    assert ok is False
