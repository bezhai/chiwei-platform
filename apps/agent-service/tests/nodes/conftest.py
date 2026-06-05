"""Re-export real-pg fixtures so node tests can back the session store.

The session transcript store is now durable PG (Data ``SessionTranscript``), so
the few node tests that exercise a real ``append_session`` / ``load_session``
round-trip need the ``test_db`` fixture. pytest only auto-discovers conftest
fixtures along the path from rootdir to the test file, so tests under
``tests/nodes/`` cannot see the ones defined in ``tests/runtime/conftest.py``
directly. Re-export here keeps the fixture definition single-sourced under
``tests/runtime/`` while making it visible to node tests too.
"""
from __future__ import annotations

from tests.runtime.conftest import test_db, test_db_dsn  # noqa: F401
