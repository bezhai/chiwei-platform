"""Tests for ``@retry`` decorator (B3 / plan §B3).

Process-local retry for one-shot async network/LLM calls. Sibling to
``app/runtime/retry.py`` (cross-process broker re-delivery, different
abstraction). Behaviour contract pinned here:

* white-list semantics — only ``retry_on`` exceptions retry,
  everything else propagates immediately,
* exponential vs linear backoff math + ``max_delay_s`` clamp,
* last attempt re-raises the original typed exception (no wrapping),
* decorator only accepts ``async def`` — sync function raises
  ``TypeError`` at decoration time,
* per-attempt ``logging.warning`` carries function name / attempt /
  delay / exception type.

``asyncio.sleep`` is patched out so the suite stays sub-second; the
delays the decorator *would* have slept are recorded for assertions.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from app.capabilities._errors import (
    CapabilityCallFailed,
    CapabilityInvalidArg,
    CapabilityRateLimited,
    CapabilityTimeout,
)
from app.capabilities.retry import retry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace ``asyncio.sleep`` inside the retry module with a recorder."""
    recorded: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        recorded.append(delay)

    monkeypatch.setattr("app.capabilities.retry.asyncio.sleep", _fake_sleep)
    return recorded


def _make_flaky(*exceptions: Exception, final: Any = "ok") -> Any:
    """Return an async function that raises each ``exception`` in order, then ``final``."""
    queue = list(exceptions)
    calls = {"n": 0}

    async def _fn(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if queue:
            raise queue.pop(0)
        return final

    _fn.calls = calls  # type: ignore[attr-defined]
    return _fn


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_call_succeeds_no_retry(sleeps: list[float]) -> None:
    fn = _make_flaky(final="ok")
    wrapped = retry(attempts=3)(fn)

    result = await wrapped()

    assert result == "ok"
    assert fn.calls["n"] == 1
    assert sleeps == []  # never slept


# ---------------------------------------------------------------------------
# Whitelist behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retries_capability_timeout_until_success(sleeps: list[float]) -> None:
    fn = _make_flaky(
        CapabilityTimeout("t1"),
        CapabilityTimeout("t2"),
        final="ok",
    )
    wrapped = retry(attempts=3, base_delay_s=0.1)(fn)

    result = await wrapped()

    assert result == "ok"
    assert fn.calls["n"] == 3
    assert len(sleeps) == 2  # slept twice (after attempt 1 and 2)


@pytest.mark.asyncio
async def test_retries_mixed_whitelist_exceptions(sleeps: list[float]) -> None:
    fn = _make_flaky(
        CapabilityRateLimited("rl"),
        CapabilityCallFailed("5xx"),
        CapabilityTimeout("t"),
        final="ok",
    )
    wrapped = retry(attempts=5, base_delay_s=0.1)(fn)

    result = await wrapped()

    assert result == "ok"
    assert fn.calls["n"] == 4


@pytest.mark.asyncio
async def test_non_whitelisted_exception_propagates_without_retry(
    sleeps: list[float],
) -> None:
    fn = _make_flaky(CapabilityInvalidArg("bad arg"))
    wrapped = retry(attempts=3)(fn)

    with pytest.raises(CapabilityInvalidArg) as ei:
        await wrapped()

    assert str(ei.value) == "bad arg"
    assert fn.calls["n"] == 1  # no retry
    assert sleeps == []


@pytest.mark.asyncio
async def test_custom_retry_on_overrides_default(sleeps: list[float]) -> None:
    class MyErr(Exception):
        pass

    fn = _make_flaky(MyErr("boom"), final="ok")
    wrapped = retry(attempts=3, base_delay_s=0.1, retry_on=(MyErr,))(fn)

    result = await wrapped()

    assert result == "ok"
    assert fn.calls["n"] == 2


# ---------------------------------------------------------------------------
# Exhaustion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exhausts_attempts_raises_last_typed_exception(
    sleeps: list[float],
) -> None:
    last = CapabilityTimeout("final timeout", meta={"url": "x"})
    fn = _make_flaky(
        CapabilityTimeout("t1"),
        CapabilityTimeout("t2"),
        last,
    )
    wrapped = retry(attempts=3, base_delay_s=0.1)(fn)

    with pytest.raises(CapabilityTimeout) as ei:
        await wrapped()

    assert ei.value is last  # same instance, not wrapped
    assert str(ei.value) == "final timeout"
    assert ei.value.meta == {"url": "x"}
    assert fn.calls["n"] == 3
    assert len(sleeps) == 2  # slept between attempts only


# ---------------------------------------------------------------------------
# Backoff math
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exponential_backoff_delays(sleeps: list[float]) -> None:
    fn = _make_flaky(
        CapabilityTimeout("1"),
        CapabilityTimeout("2"),
        CapabilityTimeout("3"),
        final="ok",
    )
    wrapped = retry(
        attempts=5,
        backoff="exponential",
        base_delay_s=0.5,
        max_delay_s=30.0,
    )(fn)

    await wrapped()

    # base * 2^(N-1) for N=1,2,3 → 0.5, 1.0, 2.0
    assert sleeps == [0.5, 1.0, 2.0]


@pytest.mark.asyncio
async def test_linear_backoff_delays(sleeps: list[float]) -> None:
    fn = _make_flaky(
        CapabilityTimeout("1"),
        CapabilityTimeout("2"),
        CapabilityTimeout("3"),
        final="ok",
    )
    wrapped = retry(
        attempts=5,
        backoff="linear",
        base_delay_s=0.5,
        max_delay_s=30.0,
    )(fn)

    await wrapped()

    # base * N for N=1,2,3 → 0.5, 1.0, 1.5
    assert sleeps == [0.5, 1.0, 1.5]


@pytest.mark.asyncio
async def test_exponential_clamped_to_max_delay(sleeps: list[float]) -> None:
    fn = _make_flaky(
        CapabilityTimeout("1"),
        CapabilityTimeout("2"),
        CapabilityTimeout("3"),
        final="ok",
    )
    wrapped = retry(
        attempts=5,
        backoff="exponential",
        base_delay_s=10.0,
        max_delay_s=15.0,
    )(fn)

    await wrapped()

    # 10, 20→15, 40→15
    assert sleeps == [10.0, 15.0, 15.0]


@pytest.mark.asyncio
async def test_invalid_backoff_strategy_rejected() -> None:
    async def _fn() -> str:
        return "ok"

    with pytest.raises(ValueError):
        retry(backoff="quadratic")(_fn)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Decoration-time guards
# ---------------------------------------------------------------------------


def test_sync_function_rejected_at_decoration() -> None:
    def _sync_fn() -> str:
        return "no"

    with pytest.raises(TypeError):
        retry(attempts=3)(_sync_fn)  # type: ignore[arg-type]


def test_decorator_preserves_wrapped_metadata() -> None:
    async def my_func(x: int) -> int:
        """docstring."""
        return x

    decorated = retry(attempts=3)(my_func)

    assert decorated.__wrapped__ is my_func  # type: ignore[attr-defined]
    assert decorated.__name__ == "my_func"
    assert decorated.__doc__ == "docstring."


def test_attempts_must_be_positive() -> None:
    async def _fn() -> str:
        return "ok"

    with pytest.raises(ValueError):
        retry(attempts=0)(_fn)
    with pytest.raises(ValueError):
        retry(attempts=-1)(_fn)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_logs_warning_per_attempt(
    sleeps: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    fn = _make_flaky(
        CapabilityTimeout("first"),
        CapabilityRateLimited("second"),
        final="ok",
    )
    wrapped = retry(attempts=3, base_delay_s=0.1)(fn)

    with caplog.at_level(logging.WARNING, logger="app.capabilities.retry"):
        await wrapped()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    msg0 = warnings[0].getMessage()
    msg1 = warnings[1].getMessage()
    # function name appears
    assert "_fn" in msg0
    # attempt indicator appears
    assert "1" in msg0 and "3" in msg0
    # exception type + message appears
    assert "CapabilityTimeout" in msg0 and "first" in msg0
    assert "CapabilityRateLimited" in msg1 and "second" in msg1


# ---------------------------------------------------------------------------
# Args / kwargs pass-through
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_args_and_kwargs_passed_through(sleeps: list[float]) -> None:
    captured: dict[str, Any] = {}

    @retry(attempts=2, base_delay_s=0.1)
    async def _fn(a: int, *, b: str) -> str:
        captured["a"] = a
        captured["b"] = b
        return f"{a}-{b}"

    result = await _fn(7, b="x")

    assert result == "7-x"
    assert captured == {"a": 7, "b": "x"}


@pytest.mark.asyncio
async def test_real_asyncio_sleep_invoked_with_zero_delay() -> None:
    """End-to-end smoke: real asyncio.sleep, base_delay_s=0 keeps it fast."""
    fn = _make_flaky(CapabilityTimeout("t"), final="ok")
    wrapped = retry(attempts=2, base_delay_s=0.0, max_delay_s=0.0)(fn)

    result = await asyncio.wait_for(wrapped(), timeout=1.0)

    assert result == "ok"
