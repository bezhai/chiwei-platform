from typing import Annotated

from app.runtime.data import Data, Key
from app.runtime.node import node
from app.runtime.source import Source
from app.runtime.wire import WIRING_REGISTRY, clear_wiring, wire


class Msg(Data):
    mid: Annotated[str, Key]


@node
async def f(msg: Msg) -> None: ...


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


def test_wire_from_source():
    wire(Msg).from_(Source.interval(60))
    assert WIRING_REGISTRY[0].sources[0].kind == "interval"
