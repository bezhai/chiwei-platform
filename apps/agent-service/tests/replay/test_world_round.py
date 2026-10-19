"""Replay: the world main agent's round, driven the way messaging drives it (a delivery to the
``world`` inbox, handed to the inbox's consumer).

* ``continuation`` — the process starts (and schedules its start-up wake); a resident's digest
  wakes the first round, which lists and writes a record, reports a change (a perception
  judgement inside the round tells a resident) and sets its next wake. The scheduled queue then
  hands over both wakes: the start-up wake has been replaced and runs no round; the wake the
  first round set runs the second round, which continues the stored history.
* ``model_call_fails`` — the round's second model call fails: the delivery fails, messaging
  schedules a retry; the redelivered message runs the round again and it completes.
* ``reality_and_told`` — the query tools no other baseline calls. 赤尾's digest wakes a round
  that checks the weather right now (``check_current_weather``). 绫奈's message wakes the next
  round, which reads what 赤尾 told the world in the previous delivery (``read_messages_from``,
  from the told source's copy), looks at the hourly and daily forecasts
  (``check_hourly_forecast``, ``check_daily_forecast``) and searches the web (``search_web``:
  search, read each page, rerank). QWeather and the web answer what the scenario gives
  (``replay.weather``, ``replay.web``).
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.capabilities._errors import CapabilityTimeout
from app.infra.cst_time import CST
from tests.replay.harness import Fail, Reply, ToolUse
from tests.replay.harness.web import Hit

pytestmark = pytest.mark.integration

ROUND = "world_round"
PERCEPTION = "world_perception"

AKAOS_DIGEST = (
    "我做了这些（按先后）：\n"
    "- 14:00 CST 改做 整理胶片，在 家/客厅\n"
    "- 14:00 CST 把胶片一卷卷摆到茶几上\n"
    "做完这些，我在 家/客厅，正在 整理胶片。"
)


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, 25, hour, minute, tzinfo=CST)


async def _world_is_up(replay) -> None:
    # The sisters' inboxes are opened by agent-service, another process.
    for name in ("赤尾", "绫奈", "千凪"):
        replay.broker.declare_inbox(name)
    await replay.start("world")


def _akaos_digest_arrives(replay, message_id: str = "akao-digest-1") -> str:
    return replay.message_arrives(
        sender="赤尾",
        recipient="world",
        body=AKAOS_DIGEST,
        message_id=message_id,
        time=_at(14, 0),
    )


async def test_continuation(replay):
    await _world_is_up(replay)
    inbox = _akaos_digest_arrives(replay)

    replay.model.script(
        ROUND,
        Reply(tools=(ToolUse("list_records", {}), ToolUse("list_senders", {}))),
        Reply(
            tools=(
                ToolUse(
                    "write_record",
                    {"path": "地方/家.md", "text": "客厅的茶几上摊着赤尾的胶片。"},
                ),
            ),
        ),
        Reply(
            tools=(ToolUse("report_change", {"change": "14:01，家附近下起了小雨。"}),)
        ),
        Reply(
            tools=(
                ToolUse(
                    "wake_me_at", {"at": "2026-07-25T16:00", "reason": "看看雨停了没有"}
                ),
            ),
        ),
        Reply(text="这一轮看完了。"),
    )
    replay.model.script(
        PERCEPTION,
        Reply(
            tools=(
                ToolUse(
                    "someone_notices",
                    {
                        "who": "赤尾",
                        "what": "你听见客厅窗外下起了小雨。",
                        "right_away": False,
                    },
                ),
            ),
        ),
        Reply(text="判断完了。"),
    )
    await replay.step(
        "赤尾's digest wakes the first round",
        lambda: replay.broker.deliver(inbox),
        at=_at(14, 1),
    )

    async def hand_over_due_wakes() -> list[str]:
        return [
            await replay.broker.deliver(replay.scheduled()),
            await replay.broker.deliver(replay.scheduled()),
        ]

    await replay.step(
        "the scheduled queue hands over the due wakes",
        hand_over_due_wakes,
        at=_at(16, 0),
    )
    await replay.step(
        "the replaced start-up wake arrives", lambda: replay.broker.deliver(inbox)
    )

    replay.model.script(
        ROUND,
        Reply(tools=(ToolUse("read_record", {"path": "地方/家.md"}),)),
        Reply(
            tools=(
                ToolUse(
                    "wake_me_at", {"at": "2026-07-25T20:00", "reason": "晚上再看看家里"}
                ),
            ),
        ),
        Reply(text="雨停了，记录不用改。"),
    )
    await replay.step(
        "its own 16:00 wake runs the second round", lambda: replay.broker.deliver(inbox)
    )

    replay.check("world_round/continuation")


async def test_model_call_fails(replay):
    await _world_is_up(replay)
    inbox = _akaos_digest_arrives(replay)

    replay.model.script(
        ROUND,
        Reply(tools=(ToolUse("list_records", {}),)),
        Fail(lambda: CapabilityTimeout("offline-model gave no answer within 180s")),
    )
    await replay.step(
        "the round's second model call fails",
        lambda: replay.broker.deliver(inbox),
        at=_at(14, 1),
    )

    replay.model.script(
        ROUND,
        Reply(tools=(ToolUse("list_records", {}),)),
        Reply(
            tools=(
                ToolUse(
                    "wake_me_at",
                    {"at": "2026-07-25T16:00", "reason": "过两个小时再看看"},
                ),
            ),
        ),
        Reply(text="这一轮看完了。"),
    )
    await replay.step(
        "the redelivered message runs the round again",
        lambda: replay.broker.deliver(inbox),
        at=_at(14, 2),
    )

    replay.check("world_round/model_call_fails")


# Tokyo as QWeather's GeoAPI names it; the capability rounds the coordinates to two decimals.
TOKYO = {
    "name": "东京",
    "id": "B76E6",
    "lat": "35.68950",
    "lon": "139.69171",
    "adm2": "东京",
    "adm1": "东京都",
    "country": "日本",
    "tz": "Asia/Tokyo",
    "type": "city",
}
LAT, LON = "35.69", "139.69"
SOURCE = {"attributions": ["QWeather"]}


def _quantity(value: float, unit: str) -> dict:
    return {"value": value, "unit": unit}


def _wind(compass: str, scale: int) -> dict:
    return {"direction": {"compass": compass}, "scale": scale}


def _current() -> dict:
    return {
        "condition": {"text": "小雨", "code": "305"},
        "temperature": _quantity(26, "°C"),
        "feelsLike": _quantity(29, "°C"),
        "humidity": 0.88,
        "wind": {**_wind("se", 2), "speed": _quantity(9, "km/h")},
        "precipitation": {"amount": _quantity(0.4, "mm")},
        "visibility": _quantity(8, "km"),
        "cloudCover": 0.95,
        "uvIndex": 1,
        "metadata": SOURCE,
    }


def _hour(at: str, condition: str, probability: float, amount: float) -> dict:
    return {
        "forecastTime": at,
        "condition": {"text": condition},
        "temperature": _quantity(25, "°C"),
        "precipitation": {
            "probability": probability,
            "amount": _quantity(amount, "mm"),
        },
        "wind": _wind("s", 2),
    }


def _hourly() -> dict:
    return {
        "hours": [
            _hour("2026-07-25T16:00+09:00", "小雨", 0.7, 0.6),
            _hour("2026-07-25T17:00+09:00", "阴", 0.3, 0.0),
            _hour("2026-07-25T18:00+09:00", "多云", 0.1, 0.0),
        ],
        "metadata": SOURCE,
    }


def _half(condition: str, high: int, low: int, probability: float) -> dict:
    return {
        "condition": {"text": condition},
        "temperatureMax": _quantity(high, "°C"),
        "temperatureMin": _quantity(low, "°C"),
        "precipitation": {"probability": probability, "amount": _quantity(0.0, "mm")},
        "wind": _wind("ssw", 3),
        "humidity": 0.75,
        "cloudCover": 0.4,
    }


def _daily() -> dict:
    return {
        "days": [
            {
                "forecastStartTime": "2026-07-25T00:00+09:00",
                "temperatureMax": _quantity(29, "°C"),
                "temperatureMin": _quantity(24, "°C"),
                "uvIndexMax": 6,
                "astro": {
                    "sunrise": "04:45",
                    "sunset": "18:50",
                    "moonrise": "09:12",
                    "moonset": "21:40",
                    "moonPhase": "waxing-crescent",
                },
                "daytime": _half("小雨", 29, 25, 0.7),
                "nighttime": _half("多云", 26, 24, 0.2),
            },
            {
                "forecastStartTime": "2026-07-26T00:00+09:00",
                "temperatureMax": _quantity(32, "°C"),
                "temperatureMin": _quantity(25, "°C"),
                "uvIndexMax": 9,
                "astro": {
                    "sunrise": "04:46",
                    "sunset": "18:49",
                    "moonPhase": "first-quarter",
                },
                "daytime": _half("晴", 32, 27, 0.0),
            },
        ],
        "metadata": SOURCE,
    }


FIREWORKS = "东京 隅田川花火大会 2026 日期"
FIREWORKS_PAGES = {
    "https://example.com/events/sumida-2026": (
        "# 第 49 届隅田川花火大会\n\n"
        "日期：2026 年 7 月 25 日（周六）19:00 开始，雨天顺延至次日。\n"
        "会场：隅田川沿岸两处，预计约两万发。"
    ),
    "https://example.com/travel/tokyo-summer": "# 东京夏日指南\n\n浅草一带的屋台从傍晚开始营业。",
}


async def test_reality_and_told(replay):
    await _world_is_up(replay)
    replay.weather.places("东京", [TOKYO])
    replay.weather.current(LAT, LON, _current())
    replay.weather.hourly(LAT, LON, _hourly())
    replay.weather.daily(LAT, LON, _daily())
    replay.web.results(
        FIREWORKS,
        [
            Hit(
                "第 49 届隅田川花火大会",
                "https://example.com/events/sumida-2026",
                "7 月 25 日 19:00 开始",
            ),
            Hit(
                "东京夏日指南", "https://example.com/travel/tokyo-summer", "浅草的屋台"
            ),
        ],
    )
    for url, text in FIREWORKS_PAGES.items():
        replay.web.page(url, text)
    replay.web.rerank(FIREWORKS, [0.87, 0.21])

    inbox = _akaos_digest_arrives(replay)
    replay.model.script(
        ROUND,
        Reply(tools=(ToolUse("check_current_weather", {"place": "东京"}),)),
        Reply(
            tools=(
                ToolUse(
                    "wake_me_at",
                    {"at": "2026-07-25T18:00", "reason": "傍晚看看雨停没停"},
                ),
            ),
        ),
        Reply(text="东京在下小雨，跟记录里一样，不用改。"),
    )
    await replay.step(
        "赤尾's digest wakes a round that checks the weather",
        lambda: replay.broker.deliver(inbox),
        at=_at(14, 1),
    )

    replay.message_arrives(
        sender="绫奈",
        recipient="world",
        body="我在琴房练琴，窗户开着。",
        message_id="ayana-1",
        time=_at(14, 20),
    )
    replay.model.script(
        ROUND,
        Reply(tools=(ToolUse("read_messages_from", {"name": "赤尾"}),)),
        Reply(
            tools=(
                ToolUse("check_hourly_forecast", {"place": "东京", "hours": 3}),
                ToolUse("check_daily_forecast", {"place": "东京", "days": 2}),
            ),
        ),
        Reply(tools=(ToolUse("search_web", {"query": FIREWORKS}),)),
        Reply(
            tools=(
                ToolUse(
                    "wake_me_at",
                    {"at": "2026-07-25T18:30", "reason": "花火大会开始前看看天"},
                ),
            ),
        ),
        Reply(text="雨傍晚会停，花火大会照常。"),
    )
    await replay.step(
        "绫奈's message wakes a round that reads the forecasts and the web",
        lambda: replay.broker.deliver(inbox),
        at=_at(14, 21),
    )

    replay.check("world_round/reality_and_told")
