"""Self-tests of the replay harness: guarantees a baseline relies on that no scenario would notice
breaking, because a broken one still passes.

* ``script_runs_out`` — a model call finds its agent's script empty. The harness raises
  ``ScriptExhausted``, but product code may swallow it (here the output check treats it as "the
  check failed" and sends anyway), so the step passes. The replay must still fail, naming the
  agent and call number, and must neither compare nor record a baseline.
* ``redis_is_per_replay`` — what one scenario puts in Redis is not there in the next. fakeredis
  picks its server by a random host name from ``uuid4``, which the replay makes deterministic.
"""

from __future__ import annotations

import contextlib
from datetime import datetime

import pytest

from app.capabilities import banned_words
from app.infra.cst_time import CST, now_cst
from app.living.moment import run_moment
from tests.replay import seeds
from tests.replay.conftest import DEFAULT_START, LANE
from tests.replay.harness import Replay, Reply, ToolUse
from tests.replay.harness import baseline as baseline_mod

pytestmark = pytest.mark.integration


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, 25, hour, minute, tzinfo=CST)


async def test_script_runs_out(replay, monkeypatch):
    compared: list[str] = []
    monkeypatch.setattr(
        baseline_mod, "compare_or_record", lambda name, document: compared.append(name)
    )
    await seeds.seed_household()
    await seeds.seed_akaos_phone()
    replay.broker.declare_inbox(
        "world", answers=lambda question: "客厅里只有你一个人。"
    )
    await replay.start("agent-service")
    await seeds.bezhai_says("在吗？", at=_at(13, 55), name="dm-1")

    replay.model.script(
        "living_life_moment",
        Reply(
            tools=(
                ToolUse(
                    "send_message",
                    {"what": "在呢。", "channel_id": str(seeds.DM_WITH_BEZHAI)},
                ),
            )
        ),
        Reply(tools=(ToolUse("stop_for_now", {}),)),
    )
    # No reply for the output check: its model call finds the script empty.
    await replay.step(
        "she sends; the output check's model call has no scripted reply",
        lambda: run_moment(lane=replay.lane, persona_id="akao", clock=now_cst),
        at=_at(14, 0),
    )

    with pytest.raises(AssertionError, match=r"'guard_output_safety' call #1\b"):
        replay.check("harness/script_runs_out")
    assert compared == [], "a replay whose script ran out was compared or recorded"


_DROP_TABLES = """
DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' LOOP
        EXECUTE 'DROP TABLE IF EXISTS public.' || quote_ident(r.tablename) || ' CASCADE';
    END LOOP;
END $$;
"""


@contextlib.asynccontextmanager
async def _scenario(engine, root):
    """One replay, opened and closed the way the ``replay`` fixture does for each scenario, and
    on an empty database afterwards, as the next scenario gets it (``test_db``'s teardown)."""
    from sqlalchemy import text

    with pytest.MonkeyPatch.context() as mp:
        r = Replay(
            engine=engine, monkeypatch=mp, root=root, lane=LANE, start=DEFAULT_START
        )
        await r.open()
        try:
            yield r
        finally:
            await r.close()
    async with engine.begin() as conn:
        await conn.execute(text(_DROP_TABLES))


async def test_redis_is_per_replay(real_pg_required, test_db, tmp_path):
    async with _scenario(test_db, tmp_path / "first") as first:
        await first.redis.sadd("banned_words", "darkroom")
        assert await banned_words.contains("在 Dark Room 里") == "darkroom"

    async with _scenario(test_db, tmp_path / "second"):
        assert await banned_words.contains("在 Dark Room 里") is None
