"""Re-export real-pg fixtures so life tests can use ``test_db``.

The session-scoped ``test_db_dsn`` and function-scoped ``test_db`` fixtures
live in ``tests/runtime/conftest.py``. pytest only auto-discovers conftest
fixtures along the path from rootdir to the test file, so tests under
``tests/life/`` cannot see them directly. Re-export here keeps the fixture
definition single-sourced under ``tests/runtime/`` while making it visible
to life tests too (mirrors ``tests/world/conftest.py``).
"""
from __future__ import annotations

from tests.runtime.conftest import test_db, test_db_dsn  # noqa: F401
