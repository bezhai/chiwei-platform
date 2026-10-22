"""What the host refuses to run, and says so before anything touches the database or the broker.

``MissingService`` / ``ServiceCycle`` / ``DuplicateService`` come from :class:`app.host.Host`'s
constructor, ``UnknownApp`` from :meth:`app.host.Host.for_app`, ``UndeclaredService`` from setup
(right after the plugin's own setup, before the schema step). ``DuplicateContribution`` /
``UnknownContribution`` / ``UnplacedContribution`` from the check of the manifest against what the
plugins registered (:mod:`app.host.assembly`), after every setup and before any IO. Each lists every instance of its
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


class DuplicateContribution(HostError):
    """One name is registered twice (a tool, a segment, a section, a step, a round log), or listed
    twice in one list of a profile."""


class UnknownContribution(HostError):
    """The manifest names something no plugin registered: a tool, segment, section, step or round
    log a profile or an agent lists, an agent a profile names, a profile a participant names."""


class UnplacedContribution(HostError):
    """A plugin registered something no profile or agent of the manifest lists, so nothing would
    ever use it."""
