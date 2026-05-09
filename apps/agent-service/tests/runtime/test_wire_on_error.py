"""Phase 7b Gap 18: wire(...).on_error() builder."""
from typing import Annotated

import pytest

from app.runtime.data import Data, Key
from app.runtime.wire import clear_wiring, wire


class _D(Data):
    id: Annotated[str, Key]


def setup_function(_fn):
    clear_wiring()


def test_default_on_error_is_dlq():
    builder = wire(_D)
    assert builder._spec.on_error == "dlq"


def test_on_error_sets_policy():
    builder = wire(_D).durable().on_error("ignore-duplicate")
    assert builder._spec.on_error == "ignore-duplicate"


def test_on_error_returns_builder_for_chaining():
    builder = wire(_D).durable()
    same = builder.on_error("manual-review")
    assert same is builder


@pytest.mark.parametrize("policy", ["dlq", "ignore-duplicate", "manual-review"])
def test_on_error_accepts_valid_policies(policy):
    wire(_D).durable().on_error(policy)


def test_on_error_rejects_invalid_policy():
    with pytest.raises(ValueError, match="on_error policy must be one of"):
        wire(_D).durable().on_error("retry")  # was a v0 idea; rejected at compile time


def test_on_error_rejects_typo():
    with pytest.raises(ValueError):
        wire(_D).durable().on_error("dql")
