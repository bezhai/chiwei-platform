"""Test migrate_relationship_to_abstract core logic (batch processor)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from scripts.migrate_relationship_to_abstract import process_one_row


@pytest.mark.asyncio
async def test_process_one_row_creates_fact_abstract_and_edges():
    row = SimpleNamespace(
        persona_id="chiwei",
        user_id="u1",
        core_facts="事实1\n事实2",
        impression="他很认真",
    )
    with patch("scripts.migrate_relationship_to_abstract.llm_rewrite_impression", new=AsyncMock(return_value="他做事认真")):
        with patch("scripts.migrate_relationship_to_abstract.insert_fragment", new=AsyncMock()) as ins_f:
            with patch("scripts.migrate_relationship_to_abstract.insert_abstract_memory", new=AsyncMock()) as ins_a:
                with patch("scripts.migrate_relationship_to_abstract.insert_memory_edge", new=AsyncMock()) as ins_e:
                    with patch("scripts.migrate_relationship_to_abstract.enqueue_fragment_vectorize", new=AsyncMock()):
                        with patch("scripts.migrate_relationship_to_abstract.enqueue_abstract_vectorize", new=AsyncMock()):
                            with patch("scripts.migrate_relationship_to_abstract.get_session") as mock_get_sess:
                                mock_get_sess.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
                                mock_get_sess.return_value.__aexit__ = AsyncMock(return_value=None)
                                ok = await process_one_row(row, dry_run=False)
    assert ok is True
    assert ins_f.await_count == 2  # two facts
    assert ins_a.await_count == 1  # one abstract
    assert ins_e.await_count == 2  # two supports edges


@pytest.mark.asyncio
async def test_process_one_row_llm_failure_skips():
    row = SimpleNamespace(
        persona_id="chiwei", user_id="u1",
        core_facts="事实1", impression="他很认真",
    )
    with patch("scripts.migrate_relationship_to_abstract.llm_rewrite_impression", new=AsyncMock(side_effect=RuntimeError("llm down"))):
        with patch("scripts.migrate_relationship_to_abstract.insert_fragment", new=AsyncMock()) as ins_f:
            ok = await process_one_row(row, dry_run=False)
    assert ok is False
    ins_f.assert_not_awaited()  # didn't write partial data


@pytest.mark.asyncio
async def test_process_one_row_dry_run_does_not_write():
    row = SimpleNamespace(
        persona_id="chiwei", user_id="u1",
        core_facts="事实A\n事实B", impression="印象",
    )
    with patch("scripts.migrate_relationship_to_abstract.llm_rewrite_impression", new=AsyncMock(return_value="rewritten")):
        with patch("scripts.migrate_relationship_to_abstract.insert_fragment", new=AsyncMock()) as ins_f:
            with patch("scripts.migrate_relationship_to_abstract.insert_abstract_memory", new=AsyncMock()) as ins_a:
                ok = await process_one_row(row, dry_run=True)
    assert ok is True
    ins_f.assert_not_awaited()
    ins_a.assert_not_awaited()
