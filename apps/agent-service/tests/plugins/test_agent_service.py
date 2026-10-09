"""agent-service's manifest: which plugin registers what, and the one dependency between them.

The plugins are the only definition of what the agent-service process runs. The kinds are pinned
here per plugin; the routes and their checks are pinned by ``tests/apps/test_surface.py``, the
clocks by ``test_time_source_payload_contract.py``, the outbound edges by ``test_outbound.py``,
what reaches living from outside by ``tests/living/test_no_inbound.py``.
"""
from __future__ import annotations

import importlib
from collections import Counter

import pytest

from app.deployment import APPS
from app.host import Host, MissingService


async def test_each_plugin_registers_its_own_part(app_host):
    host = await app_host("agent-service")

    assert [p.name for p in host.plugins] == ["ops", "operator", "skills", "living"]
    assert Counter((r.plugin, r.kind) for r in host.registered()) == {
        ("ops", "route"): 5,
        ("operator", "route"): 6,
        ("operator", "inbox"): 1,
        ("skills", "service"): 1,
        ("skills", "task"): 1,
        ("living", "clock"): 5,
        ("living", "durable"): 1,
        ("living", "outbound"): 2,
        ("living", "inboxes_at_start"): 1,
    }


def test_living_without_skills_is_refused_naming_both():
    ops, operator, _skills, living = (
        importlib.import_module(m).PLUGIN for m in APPS["agent-service"]
    )

    with pytest.raises(MissingService) as refused:
        Host("agent-service", [ops, operator, living])

    message = str(refused.value)
    assert "'living'" in message
    assert "'skills'" in message
