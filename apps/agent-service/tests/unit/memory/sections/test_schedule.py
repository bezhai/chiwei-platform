"""Test schedule section.

Schedules are no longer generated (life engine schedule pipeline removed).
``build_schedule_section`` is now a no-op that always returns an empty string,
so the composer never appends a schedule section and never errors.
"""

from __future__ import annotations

import pytest

from app.memory.sections.schedule import build_schedule_section


@pytest.mark.asyncio
async def test_schedule_section_always_empty():
    """No schedule generated → empty string, no DB read, no error."""
    text = await build_schedule_section(persona_id="chiwei")
    assert text == ""
