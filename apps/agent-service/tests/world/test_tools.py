"""主 agent 手里的工具：看记录、写记录、查现实、定下次醒来的时刻。

工具在一轮的 ambient context 里跑（:class:`app.world.tools.RoundScope`），这里直接在
``agent_context`` 里调它们，不起模型。
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.agent.context import AgentContext
from app.agent.runtime_context import agent_context
from app.infra.cst_time import CST, now_cst
from app.world import records, tools
from app.world.tools import ROUND_SCOPE, RoundScope


@pytest.fixture
def scope(volume) -> RoundScope:
    s = RoundScope()
    ctx = AgentContext(features={ROUND_SCOPE: s})
    with agent_context(ctx):
        yield s


async def _call(t, **arguments):
    return await t.invoke(arguments)


# ---------------------------------------------------------------------------
# 记录
# ---------------------------------------------------------------------------


async def test_list_records_shows_paths_and_sizes(scope):
    assert "还没有任何记录" in await _call(tools.list_records)

    records.write("地方/甲.md", "一二三。", expected=None)
    shown = await _call(tools.list_records)

    assert "地方/甲.md" in shown and "4 字" in shown


async def test_a_new_record_can_be_written_without_reading_first(scope):
    result = await _call(tools.write_record, path="人/乙.md", text="一个新来的人。")

    assert "写好了" in result
    assert records.read("人/乙.md").text == "一个新来的人。"


async def test_an_existing_record_is_rewritten_only_after_reading_it_this_round(scope):
    records.write("地方/甲.md", "旧的样子。", expected=None)

    refused = await _call(tools.write_record, path="地方/甲.md", text="没读就改。")
    assert "先读" in str(refused)
    assert records.read("地方/甲.md").text == "旧的样子。"

    shown = await _call(tools.read_record, path="地方/甲.md")
    assert "旧的样子。" in shown
    await _call(tools.write_record, path="地方/甲.md", text="新的样子。")
    assert records.read("地方/甲.md").text == "新的样子。"

    # 自己刚写过的，同一轮里接着改不用再读一遍。
    await _call(tools.write_record, path="地方/甲.md", text="又改了一次。")
    assert records.read("地方/甲.md").text == "又改了一次。"


async def test_a_record_changed_by_someone_else_after_reading_is_not_overwritten(scope):
    first = records.write("地方/甲.md", "它读到的。", expected=None)
    await _call(tools.read_record, path="地方/甲.md")
    records.write("地方/甲.md", "人工改过的。", expected=first.fingerprint)

    refused = await _call(tools.write_record, path="地方/甲.md", text="拿旧的改。")

    assert "重新读" in str(refused)
    assert records.read("地方/甲.md").text == "人工改过的。"


async def test_reading_a_missing_record_says_so(scope):
    result = await _call(tools.read_record, path="没有/这份.md")

    assert "没有" in str(result)


async def test_a_bad_path_is_reported_not_raised(scope):
    result = await _call(tools.write_record, path="../越界.md", text="x")

    assert "记录路径" in str(result) or "走出" in str(result)


# ---------------------------------------------------------------------------
# 定下次醒来的时刻
# ---------------------------------------------------------------------------


async def test_wake_me_at_sets_the_rounds_next_wake(scope):
    at = (now_cst() + timedelta(hours=3)).replace(microsecond=0)

    result = await _call(tools.wake_me_at, at=at.isoformat(), reason="等雨停。")

    assert scope.next_wake == tools.WakeChoice(at=at, reason="等雨停。")
    assert "定好了" in result


async def test_the_last_wake_set_in_a_round_wins(scope):
    first = now_cst() + timedelta(hours=1)
    second = now_cst() + timedelta(hours=2)

    await _call(tools.wake_me_at, at=first.isoformat(), reason="先这样。")
    await _call(tools.wake_me_at, at=second.isoformat(), reason="改主意了。")

    assert scope.next_wake.reason == "改主意了。"


async def test_a_time_without_an_offset_is_read_as_utc_plus_eight(scope):
    local = (now_cst() + timedelta(hours=1)).replace(tzinfo=None, microsecond=0)

    await _call(tools.wake_me_at, at=local.isoformat(), reason="一小时后。")

    assert scope.next_wake.at == local.replace(tzinfo=CST)


@pytest.mark.parametrize(
    "offset_minutes,reason",
    [
        (None, "理由"),  # 不是时间
        (-1, "已经过去的时刻"),
        (60, "   "),  # 没写理由
    ],
)
async def test_an_unusable_wake_is_refused_and_nothing_is_set(scope, offset_minutes, reason):
    at = (
        "不是时间"
        if offset_minutes is None
        else (now_cst() + timedelta(minutes=offset_minutes)).isoformat()
    )
    result = await _call(tools.wake_me_at, at=at, reason=reason)

    assert scope.next_wake is None
    assert isinstance(result, dict) and result.get("kind") == "invalid_args"


# ---------------------------------------------------------------------------
# 查现实
# ---------------------------------------------------------------------------


async def test_check_weather_shows_what_qweather_matched_and_its_readings(scope, monkeypatch):
    from app.capabilities import weather

    place = weather.Place(location_id="1", name="甲市", adm2="甲市", adm1="甲省", country="中国")
    other = weather.Place(location_id="2", name="甲区", adm2="乙市", adm1="乙省", country="中国")

    async def find_places(name):
        assert name == "甲"
        return [place, other]

    async def weather_at(p):
        assert p == place
        return weather.Weather(
            place=p,
            now={"obsTime": "t0", "text": "小雨", "temp": "24", "feelsLike": "26"},
            hourly=[{"fxTime": "t1", "text": "中雨", "temp": "23", "pop": "80"}],
            daily=[{"fxDate": "d1", "textDay": "小雨", "textNight": "阴", "sunrise": "s1", "sunset": "s2"}],
        )

    monkeypatch.setattr(weather, "find_places", find_places)
    monkeypatch.setattr(weather, "weather_at", weather_at)

    shown = await _call(tools.check_weather, place="甲")

    assert "甲市" in shown and "甲省" in shown
    assert "甲区" in shown  # 同名的候选一并交回去
    assert "小雨" in shown and "中雨" in shown and "s2" in shown


async def test_check_weather_for_a_name_that_matches_nothing(scope, monkeypatch):
    from app.capabilities import weather

    async def find_places(name):
        return []

    monkeypatch.setattr(weather, "find_places", find_places)

    assert "查不到" in await _call(tools.check_weather, place="不存在")


def test_the_tool_set_is_small_and_reuses_search_web():
    from app.agent.tools.search import search_web

    names = [t.name for t in tools.WORLD_TOOLS]
    assert names == [
        "list_records",
        "read_record",
        "write_record",
        "search_web",
        "check_weather",
        "wake_me_at",
    ]
    assert search_web in tools.WORLD_TOOLS
    assert tools.MATERIAL_TOOLS == {"list_records", "read_record", "search_web", "check_weather"}
