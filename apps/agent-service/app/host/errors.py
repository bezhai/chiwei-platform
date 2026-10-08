"""What the host refuses to run, and says so before anything touches the database or the broker.

``MissingService`` / ``ServiceCycle`` / ``DuplicateService`` come from :class:`app.host.Host`'s
constructor, ``UnknownApp`` from :meth:`app.host.Host.for_app`, ``UndeclaredService`` from setup
(right after the plugin's own setup, before the schema step). Each lists every instance of its
problem in one message, prefixed with the app.
"""
from __future__ import annotations


class HostError(Exception):
    """A manifest, plugin or registration the host cannot run."""


class UnknownApp(HostError):
    """``APP_NAME`` names an app ``app.deployment.APPS`` does not declare."""


class MissingService(HostError):
    """A plugin requires a service no plugin in the manifest provides."""


class ServiceCycle(HostError):
    """Plugins require each other's services, so none of them can be set up first."""


class DuplicateService(HostError):
    """Two plugins provide the same service, or one plugin provides it twice."""


class UndeclaredService(HostError):
    """A plugin used a service it did not list in ``requires``, or what its setup provided does
    not match its ``provides``."""
