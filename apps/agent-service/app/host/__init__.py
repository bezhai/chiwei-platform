"""The plugin host: an app is a manifest of plugins (``app.deployment.APPS`` names its module,
:mod:`app.manifests`), and the host starts them in dependency order, stops them in the shutdown order and takes back everything they
registered. See :mod:`app.host.host`.

This package never imports living or world: a plugin module is imported only when a manifest
names it (:meth:`Host.for_app`).
"""
from app.host.clock import Tick
from app.host.errors import (
    DuplicateContribution,
    DuplicateService,
    HostError,
    MissingService,
    ServiceCycle,
    UndeclaredService,
    UnknownApp,
    UnknownContribution,
    UnplacedContribution,
)
from app.host.host import Context, Host
from app.host.manifest import App, Participant
from app.host.plugin import Disposer, Plugin, Registration
from app.host.turns import Batch, Coalesce, JoinedTurnFailed, Signal, Turns, WakeSource
from app.messaging.receiving import DeclareInbox

__all__ = [
    "App",
    "Batch",
    "Coalesce",
    "Context",
    "DeclareInbox",
    "Disposer",
    "DuplicateContribution",
    "DuplicateService",
    "Host",
    "HostError",
    "JoinedTurnFailed",
    "MissingService",
    "Participant",
    "Plugin",
    "Registration",
    "ServiceCycle",
    "Signal",
    "Tick",
    "Turns",
    "UndeclaredService",
    "UnknownApp",
    "UnknownContribution",
    "UnplacedContribution",
    "WakeSource",
]
