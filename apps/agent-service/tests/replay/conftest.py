"""Fixtures for the behaviour replays: a real Postgres (red, not skipped, without docker — a
baseline that silently skips guards nothing) and the :class:`Replay` that owns the boundaries."""

from __future__ import annotations

from datetime import datetime

import pytest

from app.infra.cst_time import CST
from tests.living.conftest import real_pg_required  # noqa: F401
from tests.replay.harness import Replay
from tests.runtime.conftest import test_db, test_db_dsn  # noqa: F401

# The lane every replayed process runs in.
LANE = "coe-replay"

# Where the frozen clock starts when a scenario gives no time.
DEFAULT_START = datetime(2026, 7, 25, 13, 0, tzinfo=CST)


@pytest.fixture
async def replay(real_pg_required, test_db, monkeypatch, tmp_path):  # noqa: F811
    r = Replay(
        engine=test_db,
        monkeypatch=monkeypatch,
        root=tmp_path,
        lane=LANE,
        start=DEFAULT_START,
    )
    await r.open()
    try:
        yield r
    finally:
        await r.close()
