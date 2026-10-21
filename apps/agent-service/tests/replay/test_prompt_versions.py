"""A round fixes the version of every prompt it may render when it first needs one: after its input
is put together and before its first model call, for its main agent and for the sub-agents it may
call (her output check; world's perception and NPC). A prompt released while the round runs is
rendered from the next round on.

Langfuse is the replay's fixture prompts, except that every fetch of a prompt reports the next
version (1, 2, ...): a round that fetched a sub-agent's prompt at each call would render its second
call from a newer version than its first.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

import pytest

from app.infra.cst_time import CST
from app.living.life.moment import LIFE_MOMENT_AGENT
from app.world.core.main_agent import ROUND_SEGMENTS
from tests.replay import seeds
from tests.replay.harness import Reply, ToolUse
from tests.replay.harness.prompts import load_fixture

pytestmark = pytest.mark.integration


class _Releasing:
    """The replay's fixture prompts; every fetch of one is a newer version than the last."""

    def __init__(self) -> None:
        self.fetches: Counter[str] = Counter()
        self.order: list[str] = []

    def get_prompt(self, prompt_id: str, *, label: str | None = None, **_: Any):
        self.fetches[prompt_id] += 1
        self.order.append(prompt_id)
        prompt = load_fixture(prompt_id)
        prompt.version = self.fetches[prompt_id]
        return prompt


@pytest.fixture
def releasing(replay, monkeypatch) -> _Releasing:
    langfuse = _Releasing()
    monkeypatch.setattr("app.agent.prompts._get_client", lambda: langfuse)
    return langfuse


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, 25, hour, minute, tzinfo=CST)


def _versions(replay, agent: str) -> list[int | tuple[int, ...]]:
    """The version each model call of ``agent`` was rendered from; for an agent whose prompt is
    several segments, the version of each segment."""
    return [
        prompt["version"]
        if isinstance(prompt, dict)
        else tuple(segment["version"] for segment in prompt)
        for prompt in (c["prompt"] for c in replay.model.calls if c["agent"] == agent)
    ]


def _sends(what: str) -> Reply:
    return Reply(
        tools=(
            ToolUse(
                "send_message", {"what": what, "channel_id": str(seeds.DM_WITH_BEZHAI)}
            ),
        )
    )


async def test_her_output_check_renders_the_version_her_round_fixed(replay, releasing):
    await seeds.seed_household()
    await seeds.seed_akaos_phone()
    replay.broker.declare_inbox("world")
    await replay.start("agent-service")
    await seeds.bezhai_says("在吗？", at=_at(13, 55), name="dm-1")
    replay.model.script(
        "living_life_moment",
        _sends("在呢。"),
        _sends("刚冲完胶片。"),
        Reply(tools=(ToolUse("stop_for_now", {}),)),
    )
    safe = Reply(data={"is_unsafe": False, "confidence": 0.1})
    replay.model.script("guard_output_safety", safe, safe)

    await replay.step(
        "her round sends twice", lambda: replay.wake("akao", "regular"), at=_at(14)
    )

    assert _versions(replay, "guard_output_safety") == [1, 1]
    assert _versions(replay, "living_life_moment") == [(1,) * 5] * 3
    assert releasing.fetches == {
        **dict.fromkeys(LIFE_MOMENT_AGENT.segments, 1),
        "guard_output_safety": 1,
    }


async def test_worlds_perception_and_npc_render_the_versions_its_round_fixed(
    replay, releasing
):
    for name in ("赤尾", "绫奈", "千凪"):
        replay.broker.declare_inbox(name)
    await replay.start("world")
    inbox = replay.message_arrives(
        sender="赤尾",
        recipient="world",
        body="我在客厅。",
        message_id="d-1",
        time=_at(14),
    )
    replay.model.script(
        "world_round",
        Reply(tools=(ToolUse("report_change", {"change": "14:01，起风了。"}),)),
        Reply(
            tools=(
                ToolUse(
                    "let_npc_appear", {"npc": "门卫", "situation": "14:01，校门口。"}
                ),
            )
        ),
        Reply(tools=(ToolUse("report_change", {"change": "14:02，下雨了。"}),)),
        Reply(
            tools=(ToolUse("wake_me_at", {"at": "2026-07-25T16:00", "reason": "看看"}),)
        ),
        Reply(text="这一轮看完了。"),
    )
    nobody = Reply(text="没有人会察觉。")
    replay.model.script("world_perception", nobody, nobody, nobody)
    replay.model.script("world_npc", Reply(text="门卫点了点头。"))

    await replay.step(
        "a round with two changes and an NPC",
        lambda: replay.broker.deliver(inbox),
        at=_at(14, 1),
    )

    assert _versions(replay, "world_perception") == [1, 1, 1]
    assert _versions(replay, "world_npc") == [1]
    assert _versions(replay, "world_round") == [(1,) * 8] * 5
    assert releasing.fetches == {
        **dict.fromkeys(ROUND_SEGMENTS, 1),
        "world_perception": 1,
        "world_npc": 1,
    }


async def test_her_prompts_are_fixed_after_her_input_and_before_her_first_model_call(
    replay, releasing, monkeypatch
):
    """The fetch sits where it always was: everything the round reads to put her input together
    comes first (her phone's notices last), the model after it."""
    import app.living.phone.handset as handset_mod

    order: list[str] = []
    real_envelopes = handset_mod.envelopes_for

    async def envelopes_for(**kw):
        order.append("her phone's notices")
        return await real_envelopes(**kw)

    real_get_prompt = releasing.get_prompt

    def get_prompt(prompt_id, **kw):
        order.append(f"fetch {prompt_id}")
        return real_get_prompt(prompt_id, **kw)

    monkeypatch.setattr(handset_mod, "envelopes_for", envelopes_for)
    monkeypatch.setattr(releasing, "get_prompt", get_prompt)
    await seeds.seed_household()
    await seeds.seed_akaos_phone()
    replay.broker.declare_inbox("world")
    await replay.start("agent-service")
    replay.model.script(
        "living_life_moment",
        lambda request: (
            order.append("model call") or Reply(tools=(ToolUse("stop_for_now", {}),))
        ),
    )

    await replay.step(
        "her round stops right away", lambda: replay.wake("akao", "regular"), at=_at(14)
    )

    fetched = [i for i, step in enumerate(order) if step.startswith("fetch living_life_moment/")]
    assert len(fetched) == 5
    assert order.index("her phone's notices") < fetched[0]
    assert fetched[-1] < order.index("model call")
