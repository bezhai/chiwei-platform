"""Re-export real-pg fixtures so agent unit tests can back the session store.

The session transcript store is now durable PG (Data ``SessionTranscript``), so
``Agent.run / stream`` session-continuation tests need the ``test_db`` fixture
to exercise a real round-trip. pytest only auto-discovers conftest fixtures along
the path from rootdir to the test file, so tests under ``tests/unit/agent/``
cannot see the ones defined in ``tests/runtime/conftest.py`` directly. Re-export
here keeps the fixture definition single-sourced under ``tests/runtime/`` while
making it visible to agent unit tests too.
"""
from __future__ import annotations

from tests.runtime.conftest import test_db, test_db_dsn  # noqa: F401
