"""The skills plugin: what ``app.main`` does with the guides today, as a plugin.

It loads ``SKILLS_DIR`` (default ``app/skills/definitions``) when it is set up, provides the
registry as the service ``skills``, and runs the 30-second reload loop on the same directory as a
task the host cancels at stop.
"""
from __future__ import annotations

import asyncio
import importlib
from pathlib import Path

import pytest

import app
from app.host import Host, Plugin
from app.skills.registry import SkillRegistry

SKILL = """---
name: drawing
description: what she looks like
---
body
"""


@pytest.fixture
def skills_plugin(monkeypatch):
    monkeypatch.setattr(SkillRegistry, "_skills", {})
    return importlib.import_module("app.plugins.skills")


async def _start(host: Host, *, tasks: bool = False) -> None:
    await host.start(http=None, schema=False, mq=False, clocks=False, tasks=tasks)


async def test_setup_loads_the_guides_in_skills_dir_and_provides_the_registry(
    skills_plugin, monkeypatch, tmp_path
):
    (tmp_path / "drawing").mkdir()
    (tmp_path / "drawing" / "SKILL.md").write_text(SKILL, encoding="utf-8")
    monkeypatch.setenv("SKILLS_DIR", str(tmp_path))
    handed: list[object] = []
    reader = Plugin(
        name="reader", setup=lambda ctx: handed.append(ctx.service("skills")), requires=("skills",)
    )
    host = Host("agent-service", [reader, skills_plugin.PLUGIN])

    await _start(host)
    try:
        assert [s.name for s in SkillRegistry.list_all()] == ["drawing"]
        assert handed == [SkillRegistry]
        assert [(r.kind, r.name) for r in host.registered()] == [
            ("service", "skills"),
            ("task", "skill-reload"),
        ]
    finally:
        await host.stop()


async def test_without_skills_dir_it_loads_the_definitions_next_to_the_app(
    skills_plugin, monkeypatch
):
    monkeypatch.delenv("SKILLS_DIR", raising=False)
    loaded: list[Path] = []
    monkeypatch.setattr(SkillRegistry, "load_all", classmethod(lambda cls, d: loaded.append(d)))
    host = Host("agent-service", [skills_plugin.PLUGIN])

    await _start(host)
    await host.stop()

    assert loaded == [Path(app.__file__).parent / "skills" / "definitions"]


async def test_the_reload_task_watches_the_same_directory_and_stops_with_the_host(
    skills_plugin, monkeypatch, tmp_path
):
    monkeypatch.setenv("SKILLS_DIR", str(tmp_path))
    watching: list[Path] = []
    running = asyncio.Event()
    cancelled = asyncio.Event()

    async def reload_loop(skills_dir: Path) -> None:
        watching.append(skills_dir)
        running.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(skills_plugin, "skill_reload_loop", reload_loop)
    host = Host("agent-service", [skills_plugin.PLUGIN])

    await _start(host, tasks=True)
    await asyncio.wait_for(running.wait(), timeout=5)
    await host.stop()

    assert watching == [tmp_path]
    assert cancelled.is_set()
