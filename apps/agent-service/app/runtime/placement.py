"""Placement layer: bind @node functions to specific deployment apps.

Model:
  * the default app is ``agent-service``;
  * unbound ``@node`` functions fall to the default app;
  * each node can be bound to at most one app — rebinding raises ``RuntimeError``.

Usage::

    bind(worker_node).to_app("vectorize-worker")
    nodes_for_app("vectorize-worker")  # -> {worker_node}
    nodes_for_app("agent-service")     # -> every unbound @node + any bound to "agent-service"
"""

from __future__ import annotations

from collections.abc import Callable

from app.runtime.node import NODE_REGISTRY

DEFAULT_APP = "agent-service"
_BINDINGS: dict[Callable, str] = {}


def clear_bindings() -> None:
    _BINDINGS.clear()


class _Binder:
    def __init__(self, fn: Callable) -> None:
        self._fn = fn

    def to_app(self, app_name: str) -> None:
        if self._fn in _BINDINGS:
            raise RuntimeError(
                f"{self._fn.__name__} already bound to {_BINDINGS[self._fn]}"
            )
        _BINDINGS[self._fn] = app_name


def bind(fn: Callable) -> _Binder:
    return _Binder(fn)


def nodes_for_app(app_name: str) -> set[Callable]:
    explicit = {n for n, a in _BINDINGS.items() if a == app_name}
    if app_name == DEFAULT_APP:
        unbound = NODE_REGISTRY - set(_BINDINGS.keys())
        return explicit | unbound
    return explicit
