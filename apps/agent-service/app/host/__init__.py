"""The plugin host: an app is a manifest of plugins (``app.deployment.APPS``), and the host
starts them in dependency order, stops them in the shutdown order and takes back everything they
registered. See :mod:`app.host.host`.

This package never imports living or world: a plugin module is imported only when a manifest
names it (:meth:`Host.for_app`).
"""
from app.host.clock import Tick
from app.host.errors import (
    DuplicateService,
    HostError,
    MissingService,
    ServiceCycle,
    UndeclaredService,
    UnknownApp,
)
from app.host.host import Context, Host
from app.host.plugin import Disposer, Plugin, Registration

__all__ = [
    "Context",
    "Disposer",
    "DuplicateService",
    "Host",
    "HostError",
    "MissingService",
    "Plugin",
    "Registration",
    "ServiceCycle",
    "Tick",
    "UndeclaredService",
    "UnknownApp",
]
