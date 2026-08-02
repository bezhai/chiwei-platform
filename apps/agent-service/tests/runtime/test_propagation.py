"""Contract tests for runtime/propagation.py — Gap 11 primitive."""

from __future__ import annotations

import pytest

from app.api.middleware import lane_var, trace_id_var
from app.runtime.propagation import (
    Context,
    bind_context,
    extract_context,
    inject_context,
    outbound_context,
)


class TestExtractContext:
    def test_strings_pass_through(self) -> None:
        ctx = extract_context({"trace_id": "abc", "lane": "feat-x"})
        assert ctx.trace_id == "abc"
        assert ctx.lane == "feat-x"

    def test_empty_strings_become_none(self) -> None:
        ctx = extract_context({"trace_id": "", "lane": ""})
        assert ctx.trace_id is None
        assert ctx.lane is None

    def test_non_string_values_become_none(self) -> None:
        ctx = extract_context({"trace_id": 123, "lane": ["x"]})
        assert ctx.trace_id is None
        assert ctx.lane is None

    def test_missing_keys_become_none(self) -> None:
        ctx = extract_context({})
        assert ctx.trace_id is None
        assert ctx.lane is None

    def test_none_headers(self) -> None:
        ctx = extract_context(None)
        assert ctx.trace_id is None
        assert ctx.lane is None


class TestInjectContext:
    def test_writes_strings(self) -> None:
        h = inject_context({}, Context(trace_id="t1", lane="prod"))
        assert h == {"trace_id": "t1", "lane": "prod"}

    def test_none_becomes_empty_string(self) -> None:
        h = inject_context({}, Context(trace_id=None, lane=None))
        assert h == {"trace_id": "", "lane": ""}

    def test_preserves_existing_headers(self) -> None:
        h = inject_context(
            {"data_type": "Foo"}, Context(trace_id="t", lane=None)
        )
        assert h == {"data_type": "Foo", "trace_id": "t", "lane": ""}

    def test_reads_from_contextvars_when_no_arg(self) -> None:
        t_tok = trace_id_var.set("from-cv")
        l_tok = lane_var.set("lane-cv")
        try:
            h = inject_context({})
        finally:
            trace_id_var.reset(t_tok)
            lane_var.reset(l_tok)
        assert h == {"trace_id": "from-cv", "lane": "lane-cv"}

    def test_no_args_with_unset_contextvars_yields_empty_strings(self) -> None:
        # contextvars default to None when not set in this scope
        h = inject_context(None)
        assert h == {"trace_id": "", "lane": ""}


class TestOutboundContext:
    """Producer-side lane must be resolved the way ``mq.publish`` picks the
    queue — ``current_lane()``: contextvar first, ``LANE`` env second.

    This is deliberately NOT symmetric with the consuming side, which is
    header-only: a prod pod drains lane messages that TTL'd back from a lane
    queue, so consuming with an env fallback would relabel them as prod.
    A producer has no such ambiguity — the process publishes to its own lane.
    """

    def test_contextvar_wins(self, monkeypatch) -> None:
        monkeypatch.setenv("LANE", "ppe-env")
        l_tok = lane_var.set("ppe-ctx")
        t_tok = trace_id_var.set("t1")
        try:
            ctx = outbound_context()
        finally:
            lane_var.reset(l_tok)
            trace_id_var.reset(t_tok)
        assert ctx == Context(trace_id="t1", lane="ppe-ctx")

    def test_falls_back_to_lane_env_when_contextvar_is_empty(
        self, monkeypatch
    ) -> None:
        monkeypatch.setenv("LANE", "ppe-env")
        ctx = outbound_context()
        assert ctx.lane == "ppe-env"

    def test_prod_and_missing_lane_are_none(self, monkeypatch) -> None:
        monkeypatch.setenv("LANE", "prod")
        assert outbound_context().lane is None
        monkeypatch.delenv("LANE")
        assert outbound_context().lane is None

    def test_fallback_lane_beats_env_but_not_contextvar(
        self, monkeypatch
    ) -> None:
        """``fallback_lane`` is the Data-carried lane used by sink dispatch;
        it ranks between the contextvar and the process env."""
        monkeypatch.setenv("LANE", "ppe-env")
        assert outbound_context(fallback_lane="ppe-body").lane == "ppe-body"
        l_tok = lane_var.set("ppe-ctx")
        try:
            assert outbound_context(fallback_lane="ppe-body").lane == "ppe-ctx"
        finally:
            lane_var.reset(l_tok)

    def test_fallback_lane_used_when_nothing_else_set(self, monkeypatch) -> None:
        monkeypatch.delenv("LANE", raising=False)
        assert outbound_context(fallback_lane="ppe-body").lane == "ppe-body"


class TestBindContext:
    @pytest.mark.asyncio
    async def test_sets_and_resets(self) -> None:
        prev_t = trace_id_var.get()
        prev_l = lane_var.get()
        async with bind_context(Context(trace_id="t1", lane="feat-x")):
            assert trace_id_var.get() == "t1"
            assert lane_var.get() == "feat-x"
        assert trace_id_var.get() == prev_t
        assert lane_var.get() == prev_l

    @pytest.mark.asyncio
    async def test_resets_on_exception(self) -> None:
        prev_t = trace_id_var.get()
        with pytest.raises(RuntimeError):
            async with bind_context(Context(trace_id="t1", lane=None)):
                raise RuntimeError("boom")
        assert trace_id_var.get() == prev_t

    @pytest.mark.asyncio
    async def test_none_context_clears_vars(self) -> None:
        t_tok = trace_id_var.set("outer")
        try:
            async with bind_context(Context(trace_id=None, lane=None)):
                assert trace_id_var.get() is None
                assert lane_var.get() is None
        finally:
            trace_id_var.reset(t_tok)
