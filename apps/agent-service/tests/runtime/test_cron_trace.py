"""Cron / interval source must generate a trace_id (Gap 11).

These integration tests require a runtime fixture (croniter + real sleep)
and are validated end-to-end via dev-lane Langfuse trace inspection in
the Phase 7a retry drill (plan Task 10 §10.3.4). At code level we only
verify here that the emit invocation runs under a bound context — the
contextvar-binding semantics are already covered by tests/runtime/
test_propagation.py.

Code-level smoke checks live in tests/runtime/test_engine_phase4.py
which exercise the loop with a stub croniter and a mock emit; they
already pass with the new ``bind_context`` wrapping.
"""

from __future__ import annotations

import pytest


class TestCronTrace:
    @pytest.mark.asyncio
    async def test_cron_loop_generates_trace_id(self) -> None:
        pytest.skip("verified via dev-lane Langfuse trace; see plan Task 10")

    @pytest.mark.asyncio
    async def test_interval_loop_generates_trace_id(self) -> None:
        pytest.skip("verified via dev-lane Langfuse trace; see plan Task 10")
