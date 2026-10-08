"""Replay: her rounds that something wakes early, driven the way the nudge clock drives one
person (``nudge_once`` with the clock read when the round gets its turn).

The clock itself (``phone_nudge_tick``) runs the three sisters concurrently, so the order of
their effects would differ from run to run; one person is what a baseline can pin.

Inbox (``inbox_wake``): a message passed to her that the sender says should wake her.

* ``wakes_her_once`` — a notice from world that says not to wake her arrives and does not wake
  her; then 绫奈's message does. The woken round shows both (its identity is 绫奈's message),
  she answers 绫奈 (the direct message to her sister and the round-end digest to world) and
  stops. The next tick finds nothing new: one message wakes her once.
* ``model_call_fails`` — the woken round's second model call fails after the round has been
  recorded as begun. 千凪's message, from earlier but delivered later, arrives in between. The
  next tick runs the begun round again under the same identity (it shows both messages); the
  tick after that finds both seen.

Phone (``phone_wake``): a message in a private chat, or one in a group that names her.

* ``private_chat`` — bezhai writes; the round he woke ends without her looking at the phone. The
  same unread message does not wake her again. His next message does: that round continues the
  history, she reads the chat and answers (the output check and the ``chat_response`` queue).
* ``group_names_her`` — a group message that does not name her does not wake her; one that
  names her does.
* ``model_call_fails`` — the round bezhai's first message woke fails at its first model call. His
  second message arrives; the next tick runs the begun round again under the first message's
  identity (it shows both messages on the envelope), and the tick after that is woken by the
  second message, still unread, although that round has nothing new on the phone to show.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from sqlalchemy import text

from app.capabilities._errors import CapabilityTimeout
from app.data.session import get_session
from app.infra.cst_time import CST, now_cst
from app.living.nudge import nudge_once
from tests.replay import seeds
from tests.replay.harness import Fail, Reply, ToolUse

pytestmark = pytest.mark.integration

MOMENT = "living_life_moment"
GUARD = "guard_output_safety"

GROUP = seeds.fixed_id("conversation:group-film-club")
KOBAYASHI = seeds.fixed_id("user:kobayashi")


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, 25, hour, minute, tzinfo=CST)


def _nudge(replay):
    return lambda: nudge_once(lane=replay.lane, persona_id="akao", clock=now_cst)


async def _her_household_is_up(replay) -> None:
    await seeds.seed_household()
    await seeds.seed_akaos_phone()
    replay.broker.declare_inbox(
        "world", answers=lambda question: "客厅里只有你一个人，窗外开始下小雨。"
    )
    await replay.start("agent-service")


async def _film_club_group() -> None:
    """A group her bot is in, with 小林 in it. Not in ``seeds``: only the group scenario reads it."""
    async with get_session() as s:
        await s.execute(
            text(
                "INSERT INTO common_user (common_user_id, channel, display_name) VALUES "
                "(CAST(:u AS uuid), 'lark', '小林')"
            ),
            {"u": str(KOBAYASHI)},
        )
        await s.execute(
            text(
                "INSERT INTO common_conversation (common_conversation_id, channel, scope, "
                "display_name, is_active) VALUES (CAST(:c AS uuid), 'lark', 'group', "
                "'胶片同好会', true)"
            ),
            {"c": str(GROUP)},
        )
        await s.execute(
            text(
                "INSERT INTO common_bot_presence (common_conversation_id, bot_name, is_active) "
                "VALUES (CAST(:c AS uuid), :b, true)"
            ),
            {"c": str(GROUP), "b": seeds.AKAO_BOT},
        )


async def _kobayashi_says_in_group(
    words: str, *, at: datetime, name: str, names_her: bool
) -> None:
    """小林 writes in the group, naming her bot or not (what lark-service would have stored)."""
    async with get_session() as s:
        await s.execute(
            text(
                "INSERT INTO common_message (common_message_id, channel, "
                "common_conversation_id, common_user_id, sender_display_name, role, content, "
                "content_text, scope, event_time, mentioned_common_user_ids) VALUES "
                "(CAST(:m AS uuid), 'lark', CAST(:c AS uuid), CAST(:u AS uuid), '小林', "
                "'user', CAST(:body AS jsonb), :words, 'group', :at, "
                "CAST(:named AS text[])::uuid[])"
            ),
            {
                "m": str(seeds.fixed_id(f"message:{name}")),
                "c": str(GROUP),
                "u": str(KOBAYASHI),
                "body": json.dumps(
                    [{"kind": "text", "text": words}], ensure_ascii=False
                ),
                "words": words,
                "at": int(at.timestamp() * 1000),
                "named": [str(seeds.AKAO_BOT_USER)] if names_her else [],
            },
        )


def _stops() -> Reply:
    return Reply(tools=(ToolUse("stop_for_now", {}),))


# ---------------------------------------------------------------------------- inbox


async def test_inbox_wakes_her_once(replay):
    await _her_household_is_up(replay)

    notice = replay.message_arrives(
        sender="world",
        recipient="赤尾",
        body="窗外开始下小雨了。",
        message_id="world-notice-1",
        time=_at(13, 55),
        wakes_recipient=False,
    )
    await replay.step(
        "a notice that does not wake her reaches her inbox",
        lambda: replay.broker.deliver(notice),
        at=_at(13, 55),
    )
    await replay.step(
        "the nudge clock finds nothing waking her", _nudge(replay), at=_at(13, 56)
    )

    inbox = replay.message_arrives(
        sender="绫奈",
        recipient="赤尾",
        body="姐，你看到我的发圈了吗？",
        message_id="ayana-1",
        time=_at(13, 58),
    )
    await replay.step(
        "绫奈's message reaches her inbox",
        lambda: replay.broker.deliver(inbox),
        at=_at(13, 58),
    )

    replay.model.script(
        MOMENT,
        Reply(
            tools=(
                ToolUse(
                    "switch_to",
                    {"doing": "看雨", "place": "家/客厅", "because": "窗外下雨了"},
                ),
                ToolUse("say", {"what": "在洗手台旁边，我刚看到。", "to": ["绫奈"]}),
            ),
        ),
        _stops(),
    )
    await replay.step("绫奈's message wakes her", _nudge(replay), at=_at(13, 59))

    await replay.step(
        "the next tick: the same message does not wake her again",
        _nudge(replay),
        at=_at(14, 0),
    )

    replay.check("inbox_wake/wakes_her_once")


async def test_inbox_model_call_fails(replay):
    await _her_household_is_up(replay)

    inbox = replay.message_arrives(
        sender="绫奈",
        recipient="赤尾",
        body="姐，你看到我的发圈了吗？",
        message_id="ayana-1",
        time=_at(13, 58),
    )
    await replay.step(
        "绫奈's message reaches her inbox",
        lambda: replay.broker.deliver(inbox),
        at=_at(13, 58),
    )

    replay.model.script(
        MOMENT,
        Reply(
            tools=(
                ToolUse(
                    "switch_to",
                    {"doing": "找发圈", "place": "家/洗手间", "because": "绫奈在找"},
                ),
            ),
        ),
        Fail(lambda: CapabilityTimeout("life-model gave no answer within 180s")),
    )
    await replay.step(
        "the woken round's second model call fails",
        _nudge(replay),
        at=_at(13, 59),
        raises=CapabilityTimeout,
    )

    # 千凪's message is from earlier than 绫奈's, but arrives after the round began.
    later = replay.message_arrives(
        sender="千凪",
        recipient="赤尾",
        body="晚饭我来做吧。",
        message_id="chinagi-1",
        time=_at(13, 57),
    )
    await replay.step(
        "千凪's earlier message reaches her inbox",
        lambda: replay.broker.deliver(later),
        at=_at(14, 0),
    )

    replay.model.script(
        MOMENT,
        Reply(
            tools=(
                ToolUse(
                    "switch_to",
                    {"doing": "找发圈", "place": "家/洗手间", "because": "绫奈在找"},
                ),
            ),
        ),
        _stops(),
    )
    await replay.step(
        "the next tick runs the begun round again, under the same identity",
        _nudge(replay),
        at=_at(14, 1),
    )

    await replay.step(
        "the tick after that finds both messages seen", _nudge(replay), at=_at(14, 2)
    )

    replay.check("inbox_wake/model_call_fails")


# ---------------------------------------------------------------------------- phone


async def test_phone_private_chat(replay):
    await _her_household_is_up(replay)
    await seeds.bezhai_says("在吗？", at=_at(13, 58), name="dm-1")

    replay.model.script(
        MOMENT,
        Reply(
            tools=(
                ToolUse(
                    "switch_to",
                    {
                        "doing": "冲胶卷",
                        "place": "家/暗房",
                        "because": "药水已经调好了",
                    },
                ),
            ),
        ),
        _stops(),
    )
    await replay.step(
        "bezhai's message wakes her; she does not look at the phone",
        _nudge(replay),
        at=_at(13, 59),
    )

    await replay.step(
        "the same unread message does not wake her again", _nudge(replay), at=_at(14, 0)
    )

    await seeds.bezhai_says("看到回我一下", at=_at(14, 2), name="dm-2")
    replay.model.script(
        MOMENT,
        Reply(
            tools=(ToolUse("look_at_phone", {"channel_id": str(seeds.DM_WITH_BEZHAI)}),)
        ),
        Reply(
            tools=(
                ToolUse(
                    "send_message",
                    {
                        "what": "在暗房，等会儿出来回你。",
                        "channel_id": str(seeds.DM_WITH_BEZHAI),
                    },
                ),
            ),
        ),
        _stops(),
    )
    replay.model.script(GUARD, Reply(data={"is_unsafe": False, "confidence": 0.02}))
    await replay.step(
        "his next message wakes her again (continues the history)",
        _nudge(replay),
        at=_at(14, 3),
    )

    replay.check("phone_wake/private_chat")


async def test_phone_group_names_her(replay):
    await _her_household_is_up(replay)
    await _film_club_group()

    await _kobayashi_says_in_group(
        "今天有人去冲片吗", at=_at(13, 58), name="group-1", names_her=False
    )
    await replay.step(
        "a group message that does not name her does not wake her",
        _nudge(replay),
        at=_at(13, 59),
    )

    await _kobayashi_says_in_group(
        "@赤尾 你上次说的那卷片子冲出来了吗",
        at=_at(14, 1),
        name="group-2",
        names_her=True,
    )
    replay.model.script(MOMENT, _stops())
    await replay.step(
        "a group message that names her wakes her", _nudge(replay), at=_at(14, 2)
    )

    replay.check("phone_wake/group_names_her")


async def test_phone_model_call_fails(replay):
    await _her_household_is_up(replay)
    await seeds.bezhai_says("在吗？", at=_at(13, 58), name="dm-1")

    replay.model.script(
        MOMENT, Fail(lambda: CapabilityTimeout("life-model gave no answer within 180s"))
    )
    await replay.step(
        "the round bezhai woke fails at its first model call",
        _nudge(replay),
        at=_at(13, 59),
        raises=CapabilityTimeout,
    )

    await seeds.bezhai_says("看到回我一下", at=_at(14, 0), name="dm-2")
    replay.model.script(MOMENT, _stops())
    await replay.step(
        "the next tick runs the begun round again, under the first message's identity",
        _nudge(replay),
        at=_at(14, 1),
    )

    replay.model.script(MOMENT, _stops())
    await replay.step(
        "the tick after that is woken by his second message, still unread",
        _nudge(replay),
        at=_at(14, 2),
    )

    replay.check("phone_wake/model_call_fails")
