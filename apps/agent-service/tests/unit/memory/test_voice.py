"""Tests for app.memory.voice — voice reads the new LifeState snapshot.

Voice generation reads her current subjective snapshot (current_state /
response_mood) from the new lane-keyed ``LifeState`` (Task 3), not the old
``life_engine_state`` table. Schedules are no longer generated, so voice no
longer reads any schedule. Voice still writes the reply-style log
(``insert_reply_style``) — chat reads it back via ``find_latest_reply_style``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.memory.voice import generate_voice

MODULE = "app.memory.voice"


def _persona():
    return SimpleNamespace(
        display_name="赤尾", persona_lite="赤尾是一个高中生", persona_core=""
    )


@pytest.mark.asyncio
async def test_voice_reads_new_lifestate_by_lane():
    """generate_voice reads find_life_state(lane=..., persona_id=...) for state/mood."""
    snap = SimpleNamespace(
        current_state="在写作业", response_mood="有点烦", activity_type="study"
    )
    find = AsyncMock(return_value=snap)

    captured: dict = {}

    class _FakeResult:
        def text(self):
            return "<voice>内心独白</voice>"

    class _FakeAgent:
        def __init__(self, *a, **k):
            pass

        async def run(self, *, prompt_vars, messages):
            captured["prompt_vars"] = prompt_vars
            return _FakeResult()

    with (
        patch(f"{MODULE}.load_persona", new=AsyncMock(return_value=_persona())),
        patch(f"{MODULE}.find_life_state", new=find),
        patch(f"{MODULE}.current_deployment_lane", return_value="coe-x"),
        patch(f"{MODULE}.list_today_fragments", new=AsyncMock(return_value=[])),
        patch(f"{MODULE}.insert_reply_style", new=AsyncMock()) as ins,
        patch(f"{MODULE}.Agent", _FakeAgent),
    ):
        out = await generate_voice("akao")

    assert out == "<voice>内心独白</voice>"
    # lane口径 == 写入端
    assert find.await_args.kwargs == {"lane": "coe-x", "persona_id": "akao"}
    # snapshot fields flow into the prompt
    assert captured["prompt_vars"]["current_state"] == "在写作业"
    assert captured["prompt_vars"]["response_mood"] == "有点烦"
    # no schedule_segment var anymore (schedules removed)
    assert "schedule_segment" not in captured["prompt_vars"]
    # voice still persists the reply-style log (chat reads it back)
    ins.assert_awaited_once()


@pytest.mark.asyncio
async def test_voice_handles_missing_snapshot():
    """No snapshot yet → falls back to placeholder, still generates."""
    class _FakeResult:
        def text(self):
            return "<voice>x</voice>"

    class _FakeAgent:
        def __init__(self, *a, **k):
            pass

        async def run(self, *, prompt_vars, messages):
            return _FakeResult()

    with (
        patch(f"{MODULE}.load_persona", new=AsyncMock(return_value=_persona())),
        patch(f"{MODULE}.find_life_state", new=AsyncMock(return_value=None)),
        patch(f"{MODULE}.current_deployment_lane", return_value=None),
        patch(f"{MODULE}.list_today_fragments", new=AsyncMock(return_value=[])),
        patch(f"{MODULE}.insert_reply_style", new=AsyncMock()),
        patch(f"{MODULE}.Agent", _FakeAgent),
    ):
        out = await generate_voice("akao")

    assert out == "<voice>x</voice>"
