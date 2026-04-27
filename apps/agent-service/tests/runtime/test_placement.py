from __future__ import annotations

from typing import Annotated

import pytest

from app.runtime.data import Data, Key
from app.runtime.node import node
from app.runtime.placement import bind, clear_bindings, nodes_for_app


class M(Data):
    mid: Annotated[str, Key]


@node
async def worker_node(m: M) -> None: ...


@node
async def main_node(m: M) -> None: ...


def setup_function():
    clear_bindings()


def test_bind_to_app():
    bind(worker_node).to_app("vectorize-worker")
    assert worker_node in nodes_for_app("vectorize-worker")


def test_unbound_nodes_go_to_agent_service():
    bind(worker_node).to_app("vectorize-worker")
    assert main_node in nodes_for_app("agent-service")
    assert main_node not in nodes_for_app("vectorize-worker")


def test_rebind_rejected():
    bind(worker_node).to_app("vectorize-worker")
    with pytest.raises(RuntimeError, match="already bound"):
        bind(worker_node).to_app("arq-worker")
