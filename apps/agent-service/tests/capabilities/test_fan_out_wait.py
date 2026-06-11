"""Tests for ``fan_out_wait`` helper (B2 / plan §B2).

Process-local concurrency helper: dispatch a group of coroutines, wait for
them all (or up to a total timeout), collect typed results. Replaces the
hand-rolled ``asyncio.gather(... return_exceptions=True)`` +
``asyncio.wait_for(...)`` patterns in:

* ``app/chat/_context_images.py`` (homogeneous list, no timeout)
* ``app/nodes/safety.py`` (3 LLM checks, 20s total timeout)

Behaviour contract pinned here:

* dict input → dict output (keys preserved, position-independent)
* list input → list output (order preserved, indices match)
* ``timeout_s`` is a *total* deadline; not-yet-done coros are **cancelled**
  and surface as ``TimeoutError`` in the result slot
* exception types are preserved (not wrapped) so callers can catch
  the typed ``CapabilityError`` subclasses
* ``return_exceptions=False`` re-raises the first failure as-is

The acceptance scenario from the plan ("safety 三个 LLM 检查并发跑、其中一个
慢响应、剩两个超时不被拖累") is encoded as
``test_acceptance_safety_two_fast_one_slow_total_timeout``.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.capabilities.concurrency import fan_out_wait

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _delayed(value: Any, delay: float = 0.0) -> Any:
    await asyncio.sleep(delay)
    return value


async def _raise(exc: BaseException, delay: float = 0.0) -> Any:
    await asyncio.sleep(delay)
    raise exc


# ---------------------------------------------------------------------------
# Happy path — list / dict shapes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_all_succeed_returns_results_in_order() -> None:
    results = await fan_out_wait(
        [_delayed("a"), _delayed("b"), _delayed("c")],
    )
    assert results == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_dict_all_succeed_returns_results_by_key() -> None:
    results = await fan_out_wait(
        {"x": _delayed(1), "y": _delayed(2), "z": _delayed(3)},
    )
    assert results == {"x": 1, "y": 2, "z": 3}


@pytest.mark.asyncio
async def test_empty_list_returns_empty_list() -> None:
    results = await fan_out_wait([])
    assert results == []


@pytest.mark.asyncio
async def test_empty_dict_returns_empty_dict() -> None:
    results = await fan_out_wait({})
    assert results == {}


# ---------------------------------------------------------------------------
# Exceptions — return_exceptions=True (default)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_one_raises_others_succeed_returns_exception_in_slot() -> None:
    err = RuntimeError("boom")
    results = await fan_out_wait(
        [_delayed("a"), _raise(err), _delayed("c")],
    )
    assert results[0] == "a"
    assert isinstance(results[1], RuntimeError)
    assert results[1] is err  # exact instance preserved, not wrapped
    assert results[2] == "c"


@pytest.mark.asyncio
async def test_dict_one_raises_others_succeed_returns_exception_by_key() -> None:
    err = ValueError("nope")
    results = await fan_out_wait(
        {"ok": _delayed("a"), "bad": _raise(err), "ok2": _delayed("c")},
    )
    assert results["ok"] == "a"
    assert isinstance(results["bad"], ValueError)
    assert results["bad"] is err
    assert results["ok2"] == "c"


# ---------------------------------------------------------------------------
# Exceptions — return_exceptions=False
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_one_raises_return_exceptions_false_raises() -> None:
    err = RuntimeError("boom")
    with pytest.raises(RuntimeError) as excinfo:
        await fan_out_wait(
            [_delayed("a"), _raise(err), _delayed("c")],
            return_exceptions=False,
        )
    assert excinfo.value is err


@pytest.mark.asyncio
async def test_dict_one_raises_return_exceptions_false_raises() -> None:
    err = ValueError("nope")
    with pytest.raises(ValueError) as excinfo:
        await fan_out_wait(
            {"a": _delayed("ok"), "b": _raise(err)},
            return_exceptions=False,
        )
    assert excinfo.value is err


# ---------------------------------------------------------------------------
# Timeout — total deadline, unfinished cancelled, TimeoutError in slot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_total_timeout_fast_completed_slow_cancelled() -> None:
    """Slow task does not block fast ones; slow surfaces as TimeoutError."""
    results = await fan_out_wait(
        [
            _delayed("fast1", delay=0.01),
            _delayed("slow", delay=5.0),
            _delayed("fast2", delay=0.01),
        ],
        timeout_s=0.2,
    )
    assert results[0] == "fast1"
    assert isinstance(results[1], TimeoutError)
    assert results[2] == "fast2"


@pytest.mark.asyncio
async def test_total_timeout_dict_preserves_keys() -> None:
    results = await fan_out_wait(
        {
            "fast": _delayed("ok", delay=0.01),
            "slow": _delayed("late", delay=5.0),
        },
        timeout_s=0.2,
    )
    assert results["fast"] == "ok"
    assert isinstance(results["slow"], TimeoutError)


@pytest.mark.asyncio
async def test_total_timeout_unfinished_coros_are_cancelled() -> None:
    """Slow tasks must observe CancelledError; verify with a counter."""
    cancelled = {"n": 0}

    async def _trackable_slow() -> str:
        try:
            await asyncio.sleep(5.0)
            return "should not reach"
        except asyncio.CancelledError:
            cancelled["n"] += 1
            raise

    results = await fan_out_wait(
        [_delayed("ok", delay=0.01), _trackable_slow(), _trackable_slow()],
        timeout_s=0.2,
    )
    assert results[0] == "ok"
    assert isinstance(results[1], TimeoutError)
    assert isinstance(results[2], TimeoutError)

    # Give the event loop a tick for cancellations to propagate.
    await asyncio.sleep(0.05)
    assert cancelled["n"] == 2, "both slow coros must have observed CancelledError"


@pytest.mark.asyncio
async def test_total_timeout_return_exceptions_false_raises_timeout() -> None:
    """When the deadline trips and return_exceptions=False, raise TimeoutError."""
    with pytest.raises(TimeoutError):
        await fan_out_wait(
            [_delayed("fast", delay=0.01), _delayed("slow", delay=5.0)],
            timeout_s=0.2,
            return_exceptions=False,
        )


# ---------------------------------------------------------------------------
# Acceptance scenario from plan: safety pipeline replay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acceptance_safety_two_fast_one_slow_total_timeout() -> None:
    """演练剧本: 三个 LLM 检查并发跑、一个慢响应、剩两个不被拖累.

    Mirrors ``app/nodes/safety.py:_run_pre_audit``: injection + politics +
    nsfw run in parallel under a total timeout. The slow one must not
    delay the fast ones returning their verdicts.
    """
    async def _injection_fast() -> dict[str, Any]:
        await asyncio.sleep(0.02)
        return {"name": "injection", "verdict": "pass"}

    async def _politics_fast() -> dict[str, Any]:
        await asyncio.sleep(0.02)
        return {"name": "politics", "verdict": "pass"}

    async def _nsfw_slow() -> dict[str, Any]:
        await asyncio.sleep(5.0)  # would exceed deadline
        return {"name": "nsfw", "verdict": "pass"}

    results = await fan_out_wait(
        {
            "injection": _injection_fast(),
            "politics": _politics_fast(),
            "nsfw": _nsfw_slow(),
        },
        timeout_s=0.2,
    )

    assert results["injection"] == {"name": "injection", "verdict": "pass"}
    assert results["politics"] == {"name": "politics", "verdict": "pass"}
    assert isinstance(results["nsfw"], TimeoutError)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_not_reached_returns_all_results() -> None:
    """Timeout is generous; all tasks complete normally."""
    results = await fan_out_wait(
        [_delayed("a", delay=0.01), _delayed("b", delay=0.02)],
        timeout_s=5.0,
    )
    assert results == ["a", "b"]


@pytest.mark.asyncio
async def test_typed_capability_exception_preserved() -> None:
    """Typed capability errors flow through unwrapped so callers can catch them."""
    from app.capabilities._errors import CapabilityTimeout

    err = CapabilityTimeout("upstream LLM timed out")
    results = await fan_out_wait([_delayed("ok"), _raise(err)])
    assert results[0] == "ok"
    assert isinstance(results[1], CapabilityTimeout)
    assert results[1] is err
