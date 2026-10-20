"""What a model is offered is built from the tools' declarations (``ToolSpec``): each tool's schema
reflected from the tool, in the order the declarations are given. Declaring a tool must not change
a byte of what the model sees — the schemas are part of every request, and the order is part of the
prompt cache's prefix — so every agent's tools, declared the way the code declares them, are run
through ``Agent`` and checked against what the baselines recorded: the schemas in
``tool_schemas`` and, for each model call, the names in ``tools``.

Runs no scenario: the baselines are read as files, the model is a stand-in that answers at once.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from app.agent.contracts import AgentSpec, ToolSpec
from app.agent.core import Agent
from app.agent.neutral import Message, Role
from app.agent.round import Call
from tests.replay.harness.baseline import BASELINES, _Normaliser


class _NoPrompts:
    """A prompt set with nothing to render: only the tools are of interest here."""

    def messages(self, agent, values=None):
        return []

    def versions(self, agent):
        return []

    def prompts(self, agent):
        return ()


async def _offered(specs: list[ToolSpec]) -> list[dict]:
    """What ``Agent`` hands the model for these declarations, as the replay records it (name,
    description, parameters) and normalises it (a multi-line string becomes its lines)."""
    model = AsyncMock()
    model.complete = AsyncMock(return_value=Message(role=Role.ASSISTANT, content="好。"))

    @contextmanager
    def no_span(**_kwargs):
        yield MagicMock()

    spec = AgentSpec(segments=("offered",), model="m", trace_name="offered")
    with (
        patch("app.agent.core.build_model_client", new=AsyncMock(return_value=model)),
        patch("app.agent.core._root_span", no_span),
    ):
        await Agent(spec, tools=specs).run(
            [Message(role=Role.USER, content="hi")],
            call=Call(agent="offered", session=None, round=None),
            prompts=_NoPrompts(),
        )
    defs = model.complete.await_args.kwargs["tools"]
    return _Normaliser([]).apply(
        [{"name": d.name, "description": d.description, "parameters": d.parameters} for d in defs]
    )


async def _declared(app_host, monkeypatch) -> dict[str, list[ToolSpec]]:
    """Each agent that is offered tools, and the declarations it is given them from."""
    from inner_shared.dynamic_config import dynamic_config

    from app.living.reading.agent import build_reading_tools
    from app.manifests.agent_service import RESIDENT
    from app.manifests.world import MANIFEST, WORLD

    # Nothing configured: every knowledge source the world manifest lists is enabled.
    monkeypatch.setattr(dynamic_config, "get", lambda key, default="": default)
    living = await app_host("agent-service")
    world = await app_host("world")

    def declared(host) -> dict[str, ToolSpec]:
        return {r.name: r.detail["spec"] for r in host.registered() if r.kind == "tool"}

    hers, its = declared(living), declared(world)
    return {
        "living_life_moment": [hers[name] for name in RESIDENT.tools],
        "book_reading_impression": build_reading_tools(pages=[], total_pages=0, progress={}),
        "world_round": [its[name] for name in WORLD.tools],
        **{
            agent: [its[name] for name in MANIFEST.agents[agent].tools]
            for agent in ("world_answer", "world_npc", "world_perception")
        },
    }


def _baselines() -> list[tuple[str, dict]]:
    return [
        (str(path.relative_to(BASELINES)), json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(BASELINES.glob("*/*.json"))
    ]


async def test_every_agent_is_offered_its_tools_as_the_baselines_recorded_them(
    app_host, monkeypatch
):
    declared = await _declared(app_host, monkeypatch)
    offered = {agent: await _offered(specs) for agent, specs in declared.items()}

    checked: set[str] = set()
    for name, doc in _baselines():
        for step in doc["steps"]:
            for call in step["model_calls"]:
                recorded = call.get("tools") or []
                if not recorded:
                    continue
                agent = call["agent"]
                assert agent in offered, f"{name}: {agent} is offered tools nothing here declares"
                assert recorded == [d["name"] for d in offered[agent]], f"{name}: {agent}"
                for d in offered[agent]:
                    assert doc["tool_schemas"][d["name"]] == {
                        "description": d["description"],
                        "parameters": d["parameters"],
                    }, f"{name}: {d['name']}"
                checked.add(agent)

    assert checked == set(declared)
