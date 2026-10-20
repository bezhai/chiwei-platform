"""Replay: her day page, driven the way the host's clock drives it (one tick of the ``day-page``
wake, signalling the three sisters' day-page queues at once).

The tick reaches the three sisters concurrently. Only 赤尾 has anything on the day being written,
so the other two return before any write or model call and the order of effects is fixed.

Her material is seeded with the product's own writes (``record_happening`` for what she did and
said, an inbox delivery for what was passed to her), between steps, so it is not part of the
record: the round under test reads it.

* ``two_mornings`` — a tick before the 04:00–06:00 window does nothing; the first tick in the
  window writes the page for the living day that just ended (things after midnight belong to it,
  things before 04:00 that morning do not; a received message and something she did at the same
  minute: the received one first), with no page before it; a later tick finds the page written;
  the next morning's page is written with the first page in front of her.
* ``model_call_fails`` — the model call fails; the tick does not raise it and nothing is written,
  no cost either. The next tick in the window writes the page.
* ``blank_reply`` — she writes nothing (whitespace only). The agent asks again inside the same
  turn, the blank turn kept in the conversation, up to three times; all three blank: the cost is
  recorded, no page. The next tick's first answer is blank again and its second one is the page;
  its cost row has the same round id as the first one's, so it is not added.
* ``commit_fails`` — the page's own transaction fails at commit after the model answered and the
  cost was recorded. The next tick asks the model again and writes the page; again its cost row is
  not added.
* ``prompt_config_error`` — a release of the diary prompt declares a variable no plugin gives:
  the call fails when it fixes its prompt, before the model; the tick does not raise it and
  nothing is written, no cost either. Once the prompt is fixed, the next tick writes the page.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.capabilities._errors import CapabilityTimeout
from app.infra.cst_time import CST
from app.living.happening import record_happening
from app.living.records import (
    KIND_ACT,
    KIND_SPEECH,
    MEDIUM_IN_PERSON,
    MEDIUM_PHONE,
    OUTBOUND_HAPPENING_PREFIX,
)
from tests.replay import seeds
from tests.replay.harness import Fail, Reply
from tests.replay.harness.prompts import config_errors

pytestmark = pytest.mark.integration

DAY_PAGE = "living_day_page"


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, day, hour, minute, tzinfo=CST)


def _tick(replay):
    return lambda: replay.tick("day-page")


async def _her_household_is_up(replay) -> None:
    await seeds.seed_household()
    await seeds.seed_akaos_phone()
    await replay.start("agent-service")


async def _she_did(
    replay,
    what: str,
    *,
    at: datetime,
    name: str,
    kind: str = KIND_ACT,
    to: tuple[str, ...] = (),
    on_phone: bool = False,
) -> None:
    """Something she did or said that day, as her round records it."""
    prefix = OUTBOUND_HAPPENING_PREFIX if on_phone else "moment:"
    await record_happening(
        lane=replay.lane,
        happening_id=f"{prefix}{seeds.fixed_id(f'happening:{name}').hex}",
        actor="akao",
        kind=kind,
        content=what,
        occurred_at=at,
        audience=to,
        medium=MEDIUM_PHONE if on_phone else MEDIUM_IN_PERSON,
        channel_id=str(seeds.DM_WITH_BEZHAI) if on_phone else None,
    )


async def _passed_to_her(
    replay, body: str, *, sender: str, at: datetime, message_id: str
) -> None:
    """A message passed to her that day, delivered to her inbox."""
    await replay.broker.deliver(
        replay.message_arrives(
            sender=sender, recipient="赤尾", body=body, message_id=message_id, time=at
        )
    )


async def _her_saturday(replay) -> None:
    """Her living day 07-25 (04:00 that day to 04:00 the next), plus one thing just before it."""
    await _she_did(replay, "还在修前一天的片子", at=_at(25, 3, 30), name="sat-0330")
    await _passed_to_her(
        replay,
        "姐，早饭在桌上。",
        sender="绫奈",
        at=_at(25, 10),
        message_id="ayana-sat-1",
    )
    await _she_did(
        replay,
        "谢啦",
        at=_at(25, 10, 5),
        name="sat-1005",
        kind=KIND_SPEECH,
        to=("绫奈",),
    )
    await _she_did(replay, "去暗房冲了两卷胶片", at=_at(25, 15), name="sat-1500")
    await _she_did(
        replay,
        "胶片冲好了，明天给你看",
        at=_at(25, 21, 30),
        name="sat-2130",
        kind=KIND_SPEECH,
        on_phone=True,
    )
    await _passed_to_her(
        replay,
        "楼下的猫在叫。",
        sender="world",
        at=_at(25, 21, 30),
        message_id="world-sat-1",
    )
    await _she_did(replay, "睡不着，起来翻相册", at=_at(26, 2, 10), name="sun-0210")


async def _her_sunday(replay) -> None:
    await _she_did(replay, "把昨天冲的胶片扫描进电脑", at=_at(26, 9), name="sun-0900")
    await _passed_to_her(
        replay,
        "下午借我一下你的相机？",
        sender="千凪",
        at=_at(26, 11),
        message_id="chinagi-sun-1",
    )


SATURDAY_PAGE = (
    "早上绫奈把早饭摆在桌上了。下午去暗房冲了两卷胶片，晚上说了句冲好了、明天给看。"
    "楼下的猫叫了一晚上，后半夜睡不着，起来翻了会儿相册。"
)


def _blank() -> Reply:
    return Reply(text=" \n ")


async def test_two_mornings(replay):
    await _her_household_is_up(replay)
    await _her_saturday(replay)

    await replay.step(
        "a tick before the window does nothing", _tick(replay), at=_at(26, 3, 55)
    )

    replay.model.script(DAY_PAGE, Reply(text=SATURDAY_PAGE))
    await replay.step(
        "the first tick in the window: she writes 07-25", _tick(replay), at=_at(26, 4)
    )

    await replay.step("a later tick finds the page written", _tick(replay), at=_at(26, 4, 5))

    await _her_sunday(replay)
    replay.model.script(
        DAY_PAGE,
        Reply(
            text="把昨天的胶片扫进了电脑，比想象中好看。千凪想借相机，下午借给她了。"
        ),
    )
    await replay.step(
        "the next morning: she writes 07-26 with the 07-25 page in front of her",
        _tick(replay),
        at=_at(27, 4),
    )

    replay.check("day_page/two_mornings")


async def test_model_call_fails(replay):
    await _her_household_is_up(replay)
    await _her_saturday(replay)

    replay.model.script(
        DAY_PAGE,
        Fail(lambda: CapabilityTimeout("offline-model gave no answer within 180s")),
    )
    await replay.step(
        "the model call fails; the tick swallows it", _tick(replay), at=_at(26, 4)
    )

    replay.model.script(DAY_PAGE, Reply(text=SATURDAY_PAGE))
    await replay.step("the next tick writes the page", _tick(replay), at=_at(26, 4, 5))

    replay.check("day_page/model_call_fails")


async def test_blank_reply(replay):
    await _her_household_is_up(replay)
    await _her_saturday(replay)

    replay.model.script(DAY_PAGE, _blank(), _blank(), _blank())
    await replay.step(
        "she writes nothing, three times in one turn", _tick(replay), at=_at(26, 4)
    )

    replay.model.script(DAY_PAGE, _blank(), Reply(text=SATURDAY_PAGE))
    await replay.step("the next tick: blank, then the page", _tick(replay), at=_at(26, 4, 5))

    replay.check("day_page/blank_reply")


async def test_commit_fails(replay):
    await _her_household_is_up(replay)
    await _her_saturday(replay)
    replay.fail_commits(lambda writes: "INSERT data_living_day_page" in writes)

    replay.model.script(DAY_PAGE, Reply(text=SATURDAY_PAGE))
    await replay.step(
        "the page's transaction fails at commit; the tick swallows it",
        _tick(replay),
        at=_at(26, 4),
    )

    replay.model.script(DAY_PAGE, Reply(text=SATURDAY_PAGE))
    await replay.step("the next tick writes the page", _tick(replay), at=_at(26, 4, 5))

    replay.check("day_page/commit_fails")


async def test_prompt_config_error(replay):
    await _her_household_is_up(replay)
    await _her_saturday(replay)
    errors_before = config_errors(DAY_PAGE, "production", "akao")

    replay.misconfigure_prompt(DAY_PAGE, "mood")
    await replay.step(
        "a release of the diary prompt declares a variable no plugin gives; the tick swallows it",
        _tick(replay),
        at=_at(26, 4),
    )
    failed = replay.steps[-1]
    assert failed["model_calls"] == []
    assert failed["rows"] == {}, "no cost, no page"
    assert config_errors(DAY_PAGE, "production", "akao") == errors_before + 1

    replay.fix_prompt(DAY_PAGE)
    replay.model.script(DAY_PAGE, Reply(text=SATURDAY_PAGE))
    await replay.step(
        "the prompt is fixed; the next tick writes the page", _tick(replay), at=_at(26, 4, 5)
    )
    assert "data_living_day_page" in replay.steps[-1]["rows"]

    replay.check("day_page/prompt_config_error")
