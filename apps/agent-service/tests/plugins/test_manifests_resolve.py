"""Both apps' manifests resolve with nothing outside the process.

Starting an app's host without IO runs every plugin's setup and then checks the manifest against
what the plugins registered (:mod:`app.host.assembly`): a tool, segment, section or step the
manifest names that no plugin registers, one a plugin registers that the manifest does not place,
or a name registered twice, fails here, before the process would touch the database or the
broker. CI runs this file alone (``.github/workflows/grep-gate.yml``, job ``manifests-resolve``),
on a runner where no database, broker, Langfuse or lane registry is reachable, so a broken manifest
is caught on the pull request rather than at the next deploy. The tests here refuse every connection
the process opens, the way that runner does, so a plugin whose setup comes to need something outside
the process fails here as well, not only in CI.

Something does try to connect: importing :mod:`app.infra.lane` builds the lane router, which polls
the lane registry once at import and then from a background thread, and logs a warning when the
registry cannot be reached. Setup and the manifest check do not depend on its answer.
"""
from __future__ import annotations

import errno
import importlib
import socket
from dataclasses import replace

import pytest

from app.deployment import APPS
from app.host import UnknownContribution


@pytest.fixture
def nothing_reachable(monkeypatch):
    """Every host name fails to resolve and every connection is refused, as on the CI runner."""

    def unresolvable(*args, **kwargs):
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    def refused(self, address):
        raise ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")

    monkeypatch.setattr(socket, "getaddrinfo", unresolvable)
    monkeypatch.setattr(socket.socket, "connect", refused)
    monkeypatch.setattr(socket.socket, "connect_ex", lambda self, address: errno.ECONNREFUSED)


@pytest.mark.parametrize("app_name", sorted(APPS))
async def test_the_apps_manifest_resolves_with_nothing_reachable(app_host, nothing_reachable, app_name):
    host = await app_host(app_name)

    manifest = importlib.import_module(APPS[app_name]).MANIFEST
    assert {p.name for p in host.plugins} == {
        importlib.import_module(module).PLUGIN.name for module in manifest.plugins
    }


# Each app, and the profile whose tools the broken manifest adds a name to.
_PROFILES = [("agent-service", "resident"), ("world", "world")]


@pytest.mark.parametrize(("app_name", "profile_name"), _PROFILES)
async def test_a_manifest_naming_a_tool_no_plugin_registers_fails_naming_it(
    app_host, nothing_reachable, app_name, profile_name
):
    manifest = importlib.import_module(APPS[app_name]).MANIFEST
    profile = manifest.profiles[profile_name]
    broken = replace(
        manifest,
        profiles={
            **manifest.profiles,
            profile_name: replace(profile, tools=(*profile.tools, "no_such_tool")),
        },
    )
    plugins = [importlib.import_module(module).PLUGIN for module in manifest.plugins]

    with pytest.raises(UnknownContribution, match="no_such_tool"):
        await app_host(app_name, plugins, manifest=broken)
