"""What each app exposes once it has started: routes, auth per route, ``/metrics``, tables.

Each app is started the way production starts it: ``app.main:app`` (uvicorn's target) with its
real lifespan, in a fresh process with ``APP_NAME`` set (:mod:`tests.apps.surface_probe`), so
nothing another app or another test imported leaks in. The process runs in a coe lane (so the
lifespan builds the business schema and skips the interval clocks) without a broker
(``RABBITMQ_URL`` empty: no consumers, no inboxes), against a database of its own in the test
Postgres container. How auth is probed is in the probe's docstring.

The expected values were captured on the code as it was before the plugin-host refactor (HEAD
83998a29) and are literals: a difference is either intended, and changed here with the reason in
the commit, or a regression.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.apps.surface_probe import LANE, SECRET
from tests.living.conftest import real_pg_required  # noqa: F401
from tests.runtime.conftest import test_db_dsn  # noqa: F401

SERVICE_ROOT = Path(__file__).resolve().parents[2]

# What a route requires before it validates a request (see the probe).
OPEN: list[str] = []
GUARDED = ["inner_secret", "lane_match", "answers_with_lane"]

# FastAPI's own docs routes and ``/health``: every app has them.
_COMMON_ROUTES = {
    "GET /docs": OPEN,
    "HEAD /docs": OPEN,
    "GET /docs/oauth2-redirect": OPEN,
    "HEAD /docs/oauth2-redirect": OPEN,
    "GET /openapi.json": OPEN,
    "HEAD /openapi.json": OPEN,
    "GET /redoc": OPEN,
    "HEAD /redoc": OPEN,
    "GET /health": OPEN,
}

EXPECTED_ROUTES: dict[str, dict[str, list[str]]] = {
    "agent-service": {
        **_COMMON_ROUTES,
        # Ops routes: no auth.
        "POST /admin/search": OPEN,
        "POST /admin/dlq/inspect": OPEN,
        "POST /admin/dlq/clear-idempotent": OPEN,
        "POST /admin/dlq/dry-run": OPEN,
        "POST /admin/dlq/requeue": OPEN,
        # The messaging mechanism's operator routes.
        "POST /admin/messaging/send": GUARDED,
        "POST /admin/messaging/ask": GUARDED,
        "POST /admin/messaging/send-at": GUARDED,
        "GET /admin/messaging/record": GUARDED,
        "GET /admin/messaging/dead-letters": GUARDED,
        "POST /admin/messaging/dead-letters/replay": GUARDED,
    },
    "world": {
        **_COMMON_ROUTES,
        # Reading and writing world's records by hand.
        "GET /admin/world/records": GUARDED,
        "GET /admin/world/records/document": GUARDED,
        "PUT /admin/world/records/document": GUARDED,
        "DELETE /admin/world/records/document": GUARDED,
    },
}

EXPECTED_TABLES: dict[str, list[str]] = {
    "agent-service": [
        "bot_persona",
        "common_agent_response",
        "common_conversation",
        "common_message",
        "common_user",
        "data_dlq_clear_idempotent_request",
        "data_dlq_clear_idempotent_response",
        "data_dlq_dry_run_request",
        "data_dlq_dry_run_response",
        "data_dlq_inspect_request",
        "data_dlq_inspect_response",
        "data_dlq_requeue_request",
        "data_dlq_requeue_response",
        "data_file_picked_up",
        "data_file_read",
        "data_happening",
        "data_life_moment",
        "data_living_day_page",
        "data_loose_end",
        "data_nudge_begun",
        "data_outgoing_message",
        "data_outgoing_result",
        "data_outgoing_up_to",
        "data_persona_version",
        "data_phone_read",
        "data_picture",
        "data_received_message",
        "data_received_read",
        "data_session_transcript",
        "data_spoken_outbound",
        "data_thinking_tokens_spent",
        "data_whereabouts",
        "message_record",
        "model_mappings",
        "model_provider",
        "runtime_dlq_audit",
        "runtime_inflight",
    ],
    "world": [
        "bot_persona",
        "common_agent_response",
        "common_conversation",
        "common_message",
        "common_user",
        "data_session_transcript",
        "data_thinking_tokens_spent",
        "message_record",
        "model_mappings",
        "model_provider",
        "runtime_dlq_audit",
        "runtime_inflight",
    ],
}


async def _fresh_database(dsn: str, name: str) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    admin = create_async_engine(dsn, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await admin.dispose()


async def _public_tables(dsn: str) -> list[str]:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(dsn)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                    "ORDER BY tablename"
                )
            )
            return [row[0] for row in result]
    finally:
        await engine.dispose()


def _run_probe(app_name: str, db_url, world_data_dir: Path) -> dict:
    env = dict(os.environ)
    env.pop("DATAFLOW_ENABLE_TIME_SOURCES", None)
    env.pop("SKILLS_DIR", None)
    env.update(
        APP_NAME=app_name,
        LANE=LANE,
        RABBITMQ_URL="",
        INNER_HTTP_SECRET=SECRET,
        POSTGRES_HOST=db_url.host,
        POSTGRES_PORT=str(db_url.port),
        POSTGRES_USER=db_url.username,
        POSTGRES_PASSWORD=db_url.password,
        POSTGRES_DB=db_url.database,
        WORLD_DATA_DIR=str(world_data_dir),
    )
    proc = subprocess.run(
        [sys.executable, "-m", "tests.apps.surface_probe"],
        cwd=SERVICE_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("app_name", ["agent-service", "world"])
async def test_what_the_app_exposes_after_start(
    app_name,
    real_pg_required,  # noqa: F811
    test_db_dsn,  # noqa: F811
    tmp_path,
):
    from sqlalchemy.engine import make_url

    database = "surface_" + app_name.replace("-", "_")
    await _fresh_database(test_db_dsn, database)
    db_url = make_url(test_db_dsn).set(database=database)

    surface = _run_probe(app_name, db_url, tmp_path / "world")
    tables = await _public_tables(db_url.render_as_string(hide_password=False))

    assert surface["routes"] == EXPECTED_ROUTES[app_name]
    assert surface["metrics"]["status"] == 200
    assert surface["metrics"]["content_type"].startswith("text/plain")
    assert tables == EXPECTED_TABLES[app_name]
