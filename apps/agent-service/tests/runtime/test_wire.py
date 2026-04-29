from typing import Annotated

import pytest

from app.runtime.data import Data, Key
from app.runtime.node import node
from app.runtime.source import Source
from app.runtime.wire import WIRING_REGISTRY, clear_wiring, wire


class Msg(Data):
    mid: Annotated[str, Key]


class State(Data):
    pid: Annotated[str, Key]
    v: int


@node
async def f(msg: Msg) -> None: ...


@node
async def g(msg: Msg, state: State) -> None: ...


def setup_function():
    clear_wiring()


def test_wire_to_registers():
    wire(Msg).to(f)
    assert len(WIRING_REGISTRY) == 1
    w = WIRING_REGISTRY[0]
    assert w.data_type is Msg
    assert w.consumers == [f]


def test_wire_durable():
    wire(Msg).to(f).durable()
    assert WIRING_REGISTRY[0].durable is True


def test_wire_as_latest():
    wire(State).to(f).as_latest()
    assert WIRING_REGISTRY[0].as_latest is True


def test_wire_from_source():
    wire(Msg).from_(Source.cron("*/5 * * * *"))
    assert WIRING_REGISTRY[0].sources[0].kind == "cron"


def test_wire_with_latest_pulls_extra_data():
    wire(Msg).to(g).with_latest(State)
    assert WIRING_REGISTRY[0].with_latest == (State,)


def test_wire_when_predicate():
    wire(Msg).to(f).when(lambda m: m.mid == "x")
    w = WIRING_REGISTRY[0]
    assert w.predicate is not None
    assert w.predicate(Msg(mid="x")) is True
    assert w.predicate(Msg(mid="y")) is False


def test_wire_debounce():
    wire(Msg).to(f).debounce(
        seconds=10,
        max_buffer=5,
        key_by=lambda m: m.mid,
    )
    w = WIRING_REGISTRY[0]
    assert w.debounce == {"seconds": 10, "max_buffer": 5}
    assert w.debounce_key_by is not None
    assert w.debounce_key_by(Msg(mid="abc")) == "abc"


def test_debounce_stores_key_by():
    wire(Msg).to(f).debounce(
        seconds=60,
        max_buffer=5,
        key_by=lambda m: f"k:{m.mid}",
    )
    spec = WIRING_REGISTRY[-1]
    assert spec.debounce == {"seconds": 60, "max_buffer": 5}
    assert spec.debounce_key_by is not None
    assert spec.debounce_key_by(Msg(mid="abc")) == "k:abc"


def test_debounce_requires_key_by_keyword():
    # key_by 必填且 keyword-only
    with pytest.raises(TypeError, match="key_by"):
        wire(Msg).to(f).debounce(seconds=60, max_buffer=5)  # type: ignore[call-arg]


