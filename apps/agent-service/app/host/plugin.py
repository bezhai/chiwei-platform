"""What a plugin is, and what the host shows about what it registered."""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.host.host import Context

# Takes back one registration. Calling it again does nothing.
Disposer = Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class Plugin:
    """One feature of an app.

    ``setup`` is synchronous and only registers (clocks, tasks, routes, inboxes, wires, services)
    through the :class:`~app.host.host.Context` it is given; it does no IO, so a manifest the host
    cannot run fails before anything touches the database or the broker. ``requires`` names the
    services ``setup`` may take with ``ctx.service``, ``provides`` the ones it must hand over with
    ``ctx.provide``; the host sets a plugin up after the plugins providing what it requires.
    """

    name: str
    setup: Callable[[Context], None]
    requires: tuple[str, ...] = ()
    provides: tuple[str, ...] = ()


@dataclass(frozen=True)
class Registration:
    """One thing a plugin registered, as :meth:`app.host.Host.registered` shows it.

    ``kind`` is ``clock`` / ``task`` / ``route`` / ``inbox`` / ``inboxes_at_start`` / ``durable``
    / ``outbound`` / ``service`` / ``on_stop``; ``detail`` is what the registration declared
    (a clock's seconds, a route's flags and handler, an inbox's options, the inboxes an opener
    declared so far). Read-only: tests and governance checks look, nothing acts on it.
    """

    plugin: str
    kind: str
    name: str
    detail: Mapping[str, Any] = field(default_factory=dict)
