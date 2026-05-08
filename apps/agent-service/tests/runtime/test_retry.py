"""Retry decision logic (Gap 7.2/7.3)."""

from __future__ import annotations

from app.runtime.retry import (
    DELIVERY_COUNT_HEADER,
    decide_retry,
    delivery_count,
)
from app.runtime.wire import RetryPolicy


class TestDeliveryCount:
    def test_no_header_returns_zero(self) -> None:
        assert delivery_count({}) == 0
        assert delivery_count(None) == 0

    def test_explicit_header(self) -> None:
        assert delivery_count({DELIVERY_COUNT_HEADER: 3}) == 3

    def test_non_int_header_treated_as_zero(self) -> None:
        assert delivery_count({DELIVERY_COUNT_HEADER: "x"}) == 0
        assert delivery_count({DELIVERY_COUNT_HEADER: -1}) == 0
        assert delivery_count({DELIVERY_COUNT_HEADER: None}) == 0


class TestDecideRetry:
    def test_no_policy_returns_dlq(self) -> None:
        d = decide_retry(headers={}, policy=None)
        assert d.action == "dlq"
        assert d.delay_ms == 0
        assert d.attempt == 0

    def test_under_n_returns_retry(self) -> None:
        p = RetryPolicy(
            n=3, backoff="exponential",
            base_delay_ms=500, max_delay_ms=30_000, lease_ms=300_000,
        )
        d = decide_retry(headers={DELIVERY_COUNT_HEADER: 0}, policy=p)
        assert d.action == "retry"
        assert d.attempt == 1
        assert d.delay_ms == 500

    def test_at_n_returns_dlq(self) -> None:
        p = RetryPolicy(
            n=3, backoff="exponential",
            base_delay_ms=500, max_delay_ms=30_000, lease_ms=300_000,
        )
        d = decide_retry(headers={DELIVERY_COUNT_HEADER: 3}, policy=p)
        assert d.action == "dlq"

    def test_over_n_returns_dlq(self) -> None:
        p = RetryPolicy(
            n=3, backoff="exponential",
            base_delay_ms=500, max_delay_ms=30_000, lease_ms=300_000,
        )
        d = decide_retry(headers={DELIVERY_COUNT_HEADER: 99}, policy=p)
        assert d.action == "dlq"

    def test_exponential_backoff_progression(self) -> None:
        p = RetryPolicy(
            n=5, backoff="exponential",
            base_delay_ms=500, max_delay_ms=30_000, lease_ms=300_000,
        )
        delays = [
            decide_retry(headers={DELIVERY_COUNT_HEADER: i}, policy=p).delay_ms
            for i in range(3)
        ]
        assert delays == [500, 1000, 2000]

    def test_linear_backoff_progression(self) -> None:
        p = RetryPolicy(
            n=5, backoff="linear",
            base_delay_ms=500, max_delay_ms=30_000, lease_ms=300_000,
        )
        delays = [
            decide_retry(headers={DELIVERY_COUNT_HEADER: i}, policy=p).delay_ms
            for i in range(3)
        ]
        assert delays == [500, 1000, 1500]

    def test_max_delay_clamps(self) -> None:
        p = RetryPolicy(
            n=20, backoff="exponential",
            base_delay_ms=500, max_delay_ms=5_000, lease_ms=300_000,
        )
        d = decide_retry(headers={DELIVERY_COUNT_HEADER: 10}, policy=p)
        assert d.delay_ms == 5_000

    def test_attempt_is_next_delivery(self) -> None:
        p = RetryPolicy(
            n=5, backoff="exponential",
            base_delay_ms=500, max_delay_ms=30_000, lease_ms=300_000,
        )
        # current delivery_count=2 means upcoming attempt is the 3rd delivery
        d = decide_retry(headers={DELIVERY_COUNT_HEADER: 2}, policy=p)
        assert d.attempt == 3
