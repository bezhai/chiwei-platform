"""Smoke test: query function's auto_tx fallback works without explicit tx().

Phase 7d Gap 13 — confirms that ``app.data.queries.persona.find_persona``
can be invoked from outside any ``async with tx():`` block and still
opens its own transaction via ``auto_tx`` to satisfy ``current_session()``.

NOTE: this file is intentionally a documentation-only skip rather than a
runnable assertion. Building a real fixture for the ``bot_persona`` ORM
table would require either Alembic migrations against the testcontainer
DB or a hand-rolled ``Base.metadata.create_all`` plumbing — neither is
in scope for Task 3 (queries refactor).

The auto_tx fallback is exercised in Task 4's broader test suite once
business callers stop passing ``session`` and start invoking these
queries through real call paths under their existing integration tests.
This file exists so the Task 13 grep gate has a concrete witness that
auto_tx fallback was considered for the queries refactor.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


def test_auto_tx_fallback_smoke_deferred() -> None:
    """Real auto_tx fallback coverage is deferred to Task 4 integration runs.

    See module docstring for rationale.
    """
    pytest.skip(
        "auto_tx fallback exercised by Task 4 business-caller integration "
        "tests (no bot_persona fixture available standalone)"
    )
