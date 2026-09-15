"""Run message concurrency tests in disposable PostgreSQL, bound to loopback.

From repo root:
uv run --project apps/agent-service python apps/lark-service/scripts/test-message-persistence.py
"""
import os
from pathlib import Path
import subprocess
import sys

from testcontainers.postgres import PostgresContainer

container = PostgresContainer("postgres:16-alpine")
container.ports = {5432: ("127.0.0.1", None)}
with container as postgres:
    url = postgres.get_connection_url().replace("postgresql+psycopg2", "postgresql")
    result = subprocess.run(
        ["bun", "test", "src/lark/message-persistence.pg.test.ts"],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "LARK_TEST_DATABASE_URL": url},
    )
    sys.exit(result.returncode)
