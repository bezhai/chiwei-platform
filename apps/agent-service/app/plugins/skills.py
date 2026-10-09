"""Skills: the guides on disk, loaded when the plugin is set up and reloaded while the app runs.

The directory is ``SKILLS_DIR`` (in prod a read-only volume), ``app/skills/definitions`` when
unset. A directory that does not exist leaves the registry empty with a warning
(:meth:`app.skills.registry.SkillRegistry.load_all`). The reload task checks the files every
30 s and loads them again when they changed.

Provides ``skills``, the registry: living's guides read it (:mod:`app.living.guides`), so living
is set up after this plugin and is refused without it.
"""
from __future__ import annotations

import os
from pathlib import Path

from app.host import Context, Plugin
from app.skills.registry import SkillRegistry, skill_reload_loop

DEFAULT_SKILLS_DIR = Path(__file__).parent.parent / "skills" / "definitions"


def setup(ctx: Context) -> None:
    skills_dir = Path(os.environ.get("SKILLS_DIR", str(DEFAULT_SKILLS_DIR)))
    SkillRegistry.load_all(skills_dir)
    ctx.provide("skills", SkillRegistry)
    ctx.task("skill-reload", lambda: skill_reload_loop(skills_dir))


PLUGIN = Plugin(name="skills", setup=setup, provides=("skills",))
