"""Her round's prompt and world's round prompt are split into segments, one per top-level section,
and the agents list them in order. Split only where one blank line separates two sections, the
segments joined by a blank line (how ``PromptSet.messages`` renders them) give back the whole
prompt byte for byte, so the model is sent exactly what it was sent before the split.

A section that serves several plugins (her ``<rules>``; world's sections on what it can look up and
on the order of a round) stays whole in the core segment.

Runs no scenario: the fixtures are read as files.
"""

from __future__ import annotations

import pytest

from app.manifests.agent_service import MANIFEST as AGENT_SERVICE
from app.manifests.world import MANIFEST as WORLD
from tests.replay.harness.prompts import PROMPTS_DIR

SPLITS = {
    "living_life_moment": ("identity", "this-moment", "rules", "voice", "guides"),
    "world_round": (
        "core",
        "reality",
        "report-change",
        "npc",
        "write-record",
        "wake",
        "unfinished",
        "order",
    ),
}


@pytest.mark.parametrize("whole", sorted(SPLITS))
def test_the_segments_join_back_into_the_whole_prompt(whole):
    parts = [(PROMPTS_DIR / whole / f"{name}.txt").read_bytes() for name in SPLITS[whole]]

    assert b"\n\n".join(parts) == (PROMPTS_DIR / f"{whole}.txt").read_bytes()


@pytest.mark.parametrize(
    ("manifest", "whole"),
    [(AGENT_SERVICE, "living_life_moment"), (WORLD, "world_round")],
)
def test_the_agent_lists_its_segments_in_order(manifest, whole):
    assert manifest.agents[whole].segments == tuple(
        f"{whole}/{name}" for name in SPLITS[whole]
    )
