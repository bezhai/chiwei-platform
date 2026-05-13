"""Tests for capability exception hierarchy (A3 / contract §4.8).

The five capability exception classes plus the base ``CapabilityError``
are the typed contract that capability/infra layers MUST raise instead
of returning ``False`` / ``None`` / stringifying. These tests pin down:

* construction (positional message + optional ``meta`` dict),
* ``str()`` round-trips the human-readable message,
* every subclass is ``isinstance`` of ``CapabilityError`` and ``Exception``,
* ``meta`` defaults to an empty dict and propagates through ``raise/except``.
"""
from __future__ import annotations

import pytest

from app.capabilities._errors import (
    CapabilityCallFailed,
    CapabilityError,
    CapabilityInvalidArg,
    CapabilityNotFound,
    CapabilityRateLimited,
    CapabilityTimeout,
)


# ---------------------------------------------------------------------------
# Base class behaviour
# ---------------------------------------------------------------------------


def test_base_message_only() -> None:
    e = CapabilityError("boom")
    assert str(e) == "boom"
    assert e.message == "boom"
    assert e.meta == {}


def test_base_with_meta() -> None:
    e = CapabilityError("boom", meta={"reason": "x"})
    assert e.meta == {"reason": "x"}


def test_meta_default_isolated_per_instance() -> None:
    """Default {} must not be shared across instances (mutable-default trap)."""
    a = CapabilityError("a")
    b = CapabilityError("b")
    a.meta["k"] = 1
    assert b.meta == {}


# ---------------------------------------------------------------------------
# Five concrete subclasses — isinstance chain
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cls",
    [
        CapabilityInvalidArg,
        CapabilityNotFound,
        CapabilityTimeout,
        CapabilityRateLimited,
        CapabilityCallFailed,
    ],
)
def test_subclass_isinstance_chain(cls: type[CapabilityError]) -> None:
    e = cls("oops", meta={"k": "v"})
    assert isinstance(e, CapabilityError)
    assert isinstance(e, Exception)
    assert str(e) == "oops"
    assert e.meta == {"k": "v"}


def test_subclasses_are_distinct() -> None:
    """Each subclass must be its own type (not aliases of the base)."""
    klasses = [
        CapabilityInvalidArg,
        CapabilityNotFound,
        CapabilityTimeout,
        CapabilityRateLimited,
        CapabilityCallFailed,
    ]
    assert len({k for k in klasses}) == 5
    # No subclass equals another even on equal message.
    assert not isinstance(CapabilityInvalidArg("x"), CapabilityNotFound)
    assert not isinstance(CapabilityNotFound("x"), CapabilityTimeout)


# ---------------------------------------------------------------------------
# raise / except plumbing
# ---------------------------------------------------------------------------


def test_can_catch_specific_via_base() -> None:
    with pytest.raises(CapabilityError) as ei:
        raise CapabilityTimeout("slow", meta={"upstream": "qdrant"})
    assert isinstance(ei.value, CapabilityTimeout)
    assert ei.value.meta == {"upstream": "qdrant"}


def test_can_catch_concrete_only() -> None:
    """CapabilityNotFound must not be caught by CapabilityTimeout."""
    with pytest.raises(CapabilityNotFound):
        try:
            raise CapabilityNotFound("missing", meta={"id": "u_1"})
        except CapabilityTimeout:  # pragma: no cover — must not catch
            pytest.fail("CapabilityTimeout should not catch CapabilityNotFound")


def test_meta_optional_keyword_only() -> None:
    """meta must be keyword-only so positional args don't collide."""
    with pytest.raises(TypeError):
        CapabilityError("msg", {"reason": "x"})  # type: ignore[misc]
