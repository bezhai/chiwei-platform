from typing import Annotated

from app.data.schema import Data, Key
from app.runtime.node import node
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
