"""agent-service's manifest: which plugin registers what, and the one dependency between them.

The plugins are the only definition of what the agent-service process runs. The kinds are pinned
here per plugin; the routes and their checks are pinned by ``tests/apps/test_surface.py``, the
clocks by ``test_time_source_payload_contract.py``, her queues of rounds and their wakes by
``tests/living/test_moment.py``, the outbound edges by ``test_outbound.py``, what reaches living
from outside by ``tests/living/test_no_inbound.py``.
"""
from __future__ import annotations

import importlib
from collections import Counter

import pytest

from app.deployment import APPS
from app.host import Host, MissingService


async def test_each_plugin_registers_its_own_part(app_host):
    host = await app_host("agent-service")

    assert [p.name for p in host.plugins] == [
        "ops",
        "operator",
        "skills",
        "persona",
        "residents",
        "her_records",
        "loose_ends",
        "pictures",
        "phone",
        "web",
        "guides",
        "living",
    ]
    assert Counter((r.plugin, r.kind) for r in host.registered()) == {
        ("ops", "route"): 5,
        ("operator", "route"): 6,
        ("operator", "inbox"): 1,
        ("skills", "service"): 1,
        ("skills", "task"): 1,
        ("persona", "service"): 1,
        ("persona", "segment"): 1,  # who she is, the first segment of her round
        ("residents", "service"): 1,
        ("her_records", "service"): 1,
        ("her_records", "section"): 2,
        ("loose_ends", "service"): 1,
        ("loose_ends", "tool"): 1,
        ("loose_ends", "section"): 2,
        ("pictures", "service"): 1,
        ("pictures", "tool"): 4,
        ("phone", "service"): 1,
        ("phone", "tool"): 3,
        ("phone", "section"): 2,
        ("phone", "commit"): 1,
        ("web", "tool"): 2,
        ("guides", "tool"): 2,
        ("guides", "segment"): 1,  # what guides she can read, the last segment of her round
        ("living", "turns"): 9,
        ("living", "wake"): 12,
        ("living", "clock"): 1,
        ("living", "route"): 1,
        ("living", "inbox"): 1,
        ("living", "outbound"): 2,
        ("living", "inboxes_at_start"): 1,
        ("living", "tool"): 10,
        ("living", "segment"): 4,  # three more segments of her round and the output check
        ("living", "round_start"): 1,
        ("living", "round_log"): 1,
        ("living", "section"): 3,
        ("living", "commit"): 2,
        ("living", "after_commit"): 2,
    }


async def test_living_declares_each_tool_her_profile_lists(app_host):
    """Her plugins (living and the ones split out of it) declare every tool the ``resident``
    profile lists and no other; the order she is given them in is the profile's."""
    from app.manifests.agent_service import RESIDENT

    host = await app_host("agent-service")

    by_name = {r.name: r.detail["spec"] for r in host.registered() if r.kind == "tool"}
    assert sorted(by_name) == sorted(RESIDENT.tools)
    declared = [by_name[name] for name in RESIDENT.tools]
    assert [s.tool.name for s in declared] == list(RESIDENT.tools)
    assert [s.tool.name for s in declared if s.trim == "material"] == [
        "look_around",
        "search_online",
        "browse_online",
        "read_a_guide",
        "run_a_script",
    ]
    assert [s.tool.name for s in declared if s.ends_round] == ["stop_for_now"]


def test_her_guides_without_skills_are_refused_naming_both():
    manifest = importlib.import_module(APPS["agent-service"]).MANIFEST
    ops, operator, _skills, *hers = (
        importlib.import_module(m).PLUGIN for m in manifest.plugins
    )

    with pytest.raises(MissingService) as refused:
        Host("agent-service", [ops, operator, *hers])

    message = str(refused.value)
    assert "'guides'" in message
    assert "'skills'" in message
