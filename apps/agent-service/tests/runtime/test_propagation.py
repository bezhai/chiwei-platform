"""Contract tests for runtime/propagation.py — Gap 11 primitive."""

from __future__ import annotations

import pytest

from app.api.middleware import lane_var, trace_id_var
from app.runtime.propagation import (
    Context,
    bind_context,
    extract_context,
    inject_context,
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
