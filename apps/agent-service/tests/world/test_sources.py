"""知识来源：怎么登记、启用哪些、四个 agent 拿到的只读工具从哪来，以及记录、现实这两个来源。

工具在 ``agent_context`` 里直接调，不起模型。
"""
from __future__ import annotations

import re

import pytest

from app.agent.context import AgentContext
from app.agent.runtime_context import agent_context
from app.agent.tooling import tool
from app.messaging.message import Kind, new_message
from app.world import records, sources
from app.world.sources import Source, private_dir, reality
from app.world.sources import records as records_source
from tests.capabilities import qweather as qw

from .conftest import LANE

WEATHER_TOOLS = {"check_current_weather", "check_hourly_forecast", "check_daily_forecast"}


@tool
async def look_up_tides() -> str:
    """查潮汐。"""
    return "涨潮。"


@pytest.fixture
async def registered(monkeypatch, app_host):
    """world 的插件登记过的那几个来源；Dynamic Config 按 ``config`` 里给的值，没给就是没配。"""
    from inner_shared.dynamic_config import dynamic_config

    config: dict[str, str] = {}
    monkeypatch.setattr(
        dynamic_config, "get", lambda k, default="": config.get(k, default)
    )
    await app_host("world")
    return config


async def _call(t, **arguments):
    return await t.invoke(arguments)


# ---------------------------------------------------------------------------
# 登记与启用
# ---------------------------------------------------------------------------


async def test_without_dynamic_config_every_registered_source_is_enabled(registered):
    names = [s.name for s in await sources.enabled_sources()]

    assert names[:2] == ["records", "reality"]
    tools = [t.name for t in await sources.query_tools()]
    assert {"list_records", "read_record", "search_web", *WEATHER_TOOLS} <= set(tools)


async def test_the_enabled_list_keeps_only_the_named_sources_in_registration_order(registered):
    registered[sources.ENABLED_SOURCES_KEY] = " reality , records ,没有这个"

    assert [s.name for s in await sources.enabled_sources()] == ["records", "reality"]


async def test_a_source_left_out_of_the_enabled_list_loses_its_tools(registered):
    registered[sources.ENABLED_SOURCES_KEY] = "records"

    assert [t.name for t in await sources.query_tools()] == ["list_records", "read_record"]


async def test_a_new_source_is_one_registration_away(registered):
    sources.register(Source(name="tides", tools=(look_up_tides,)))

    assert "look_up_tides" in [t.name for t in await sources.query_tools()]
    assert "look_up_tides" in sources.material_tools()


def test_two_sources_cannot_share_a_name_or_a_tool_name(registered):
    with pytest.raises(ValueError):
        sources.register(Source(name="records", tools=(look_up_tides,)))
    with pytest.raises(ValueError):
        sources.register(Source(name="copy", tools=(records_source.read_record,)))


def test_every_registered_query_tool_counts_as_material_even_when_disabled(registered):
    registered[sources.ENABLED_SOURCES_KEY] = "records"

    assert {"list_records", "read_record", "search_web", *WEATHER_TOOLS} <= (
        sources.material_tools()
    )


async def test_intake_goes_to_each_enabled_source_that_has_one(registered):
    seen: dict[str, list[str]] = {"a": [], "b": []}

    def keeper(name):
        async def intake(message):
            seen[name].append(message.message_id)

        return intake

    sources.register(Source(name="a", tools=(), intake=keeper("a")))
    sources.register(Source(name="b", tools=(), intake=keeper("b")))
    registered[sources.ENABLED_SOURCES_KEY] = "records,a"
    message = new_message(sender="ayana", recipient="world", body="x", kind=Kind.MESSAGE)

    await sources.take_in(message)

    assert seen == {"a": [message.message_id], "b": []}


def test_each_source_keeps_its_own_directory_next_to_the_records(bare_volume):
    assert private_dir("tides") == bare_volume / LANE / "sources" / "tides"
    assert records.records_root() == bare_volume / LANE / "records"


# ---------------------------------------------------------------------------
# 来源：记录（读的那一半）
# ---------------------------------------------------------------------------


@pytest.fixture
def reads(volume):
    """主 agent 一轮里那样：这一次调用带着记读过什么的字典。"""
    seen: dict[str, str] = {}
    with agent_context(AgentContext(features={records_source.RECORDS_READ: seen})):
        yield seen


async def test_list_records_shows_paths_and_sizes(reads):
    assert "还没有任何记录" in await _call(records_source.list_records)

    records.write("地方/甲.md", "一二三。", expected=None)
    shown = await _call(records_source.list_records)

    assert "地方/甲.md" in shown and "4 字" in shown


async def test_reading_a_record_notes_its_fingerprint_for_this_call(reads):
    written = records.write("地方/甲.md", "旧的样子。", expected=None)

    shown = await _call(records_source.read_record, path="地方/甲.md")

    assert "旧的样子。" in shown
    assert reads == {"地方/甲.md": written.fingerprint}


async def test_a_call_that_writes_nothing_reads_without_noting(volume):
    """感知判断、NPC、应答不放那个字典：读照常，什么都不记。"""
    records.write("地方/甲.md", "旧的样子。", expected=None)

    with agent_context(AgentContext()):
        shown = await _call(records_source.read_record, path="地方/甲.md")

    assert "旧的样子。" in shown


async def test_reading_a_missing_record_says_so(reads):
    result = await _call(records_source.read_record, path="没有/这份.md")

    assert "没有" in str(result)
    assert reads == {}


def test_the_records_source_only_reads():
    assert [t.name for t in records_source.SOURCE.tools] == ["list_records", "read_record"]
    assert records_source.SOURCE.intake is None


# ---------------------------------------------------------------------------
# 来源：现实
# ---------------------------------------------------------------------------


def test_the_reality_source_is_three_weather_tools_and_web_search():
    from app.agent.tools.search import search_web

    assert reality.SOURCE.name == "reality"
    assert list(reality.SOURCE.tools) == [
        reality.check_current_weather,
        reality.check_hourly_forecast,
        reality.check_daily_forecast,
        search_web,
    ]
    assert reality.SOURCE.intake is None


# 天气工具一路走到 HTTP 替身：每次调用发几个请求、发到哪儿，交回来的读数长什么样。

CITY = "/geo/v2/city/lookup"
POI = "/geo/v2/poi/lookup"
_CLOCK = re.compile(r"\d{1,2}:\d{2}")


async def test_current_weather_is_two_requests_and_shows_the_readings(monkeypatch):
    fake = qw.install(
        monkeypatch, {CITY: qw.ok(qw.CITIES), "/weather/v1/current": qw.ok(qw.CURRENT)}
    )

    shown = await _call(reality.check_current_weather, place="甲")

    assert fake.paths == [CITY, "/weather/v1/current/39.92/116.42"]
    assert fake.calls[1]["params"] == {"localTime": "true", "lang": "zh"}
    lines = shown.splitlines()
    assert lines[0] == "和风天气认下的地方：甲市（甲省，中国）"
    assert "甲区（乙市，乙省，中国）" in lines[1]  # 同名的候选一并交回去
    for reading in (
        "少云", "气温 31.71°C", "体感 33.64°C", "湿度 69%", "西南风", "风力 3 级",
        "风速 4.74 m/s", "阵风 7.07 m/s", "降水 0 mm", "能见度 29020 m", "云量 5%",
        "紫外线指数 3",
    ):
        assert reading in shown, reading
    # v1 的实况没有观测时间：不写，也不拿查询的时刻冒充
    assert "观测" not in shown and not _CLOCK.search(shown)
    assert lines[-1] == f"数据来源：{qw.ATTRIBUTION}"


@pytest.mark.parametrize(("given", "sent"), [({}, 24), ({"hours": 6}, 6)])
async def test_hourly_forecast_is_two_requests_with_the_hours_passed_through(
    monkeypatch, given, sent
):
    fake = qw.install(
        monkeypatch, {CITY: qw.ok(qw.CITIES), "/weather/v1/hourly": qw.ok(qw.HOURLY)}
    )

    shown = await _call(reality.check_hourly_forecast, place="甲", **given)

    assert fake.paths == [CITY, "/weather/v1/hourly/39.92/116.42"]
    assert fake.calls[1]["params"] == {"hours": sent, "localTime": "true", "lang": "zh"}
    hours = [line for line in shown.splitlines() if line.startswith("- ")]
    assert len(hours) == 2
    for reading in ("2024-05-31T11:00+08:00", "阴", "31.12°C", "降水概率 31%", "降水 0.09 mm",
                    "西南风", "风力 3 级"):
        assert reading in hours[0], reading
    assert "北西北风" in hours[1] and "降水概率 30%" in hours[1]
    assert shown.splitlines()[-1] == f"数据来源：{qw.ATTRIBUTION}"


@pytest.mark.parametrize(("given", "sent"), [({}, 7), ({"days": 3}, 3)])
async def test_daily_forecast_is_two_requests_and_shows_day_and_night(
    monkeypatch, given, sent
):
    fake = qw.install(
        monkeypatch, {CITY: qw.ok(qw.CITIES), "/weather/v1/daily": qw.ok(qw.DAILY)}
    )

    shown = await _call(reality.check_daily_forecast, place="甲", **given)

    assert fake.paths == [CITY, "/weather/v1/daily/39.92/116.42"]
    assert fake.calls[1]["params"] == {"days": sent, "localTime": "true", "lang": "zh"}
    lines = shown.splitlines()
    (day,) = [line for line in lines if line.startswith("- ")]
    for reading in ("2024-08-11", "最低 20.93°C", "最高 29.94°C", "日出 2024-08-11T06:22+08:00",
                    "日落 2024-08-11T19:34+08:00", "亏凸月"):
        assert reading in day, reading
    (daytime,) = [line for line in lines if "白天：" in line]
    (night,) = [line for line in lines if "夜里：" in line]
    for reading in ("小雨", "降水概率 64%", "降水 0.75 mm", "西风", "湿度 52%", "云量 32%"):
        assert reading in daytime, reading
    for reading in ("晴间多云", "降水概率 0%", "北西北风", "湿度 56%", "最低 19.95°C"):
        assert reading in night, reading
    assert lines[-1] == f"数据来源：{qw.ATTRIBUTION}"


async def test_a_name_no_city_matches_is_found_as_a_scenic_spot(monkeypatch):
    fake = qw.install(
        monkeypatch,
        {
            CITY: qw.problem(400, qw.NO_SUCH_LOCATION),
            POI: qw.ok(qw.POIS),
            "/weather/v1/current": qw.ok(qw.CURRENT),
        },
    )

    shown = await _call(reality.check_current_weather, place="丙山")

    assert fake.paths == [CITY, POI, "/weather/v1/current/39.92/116.39"]
    assert shown.splitlines()[0] == "和风天气认下的地方：丙山（丙市，丙省，中国）"


async def test_a_name_that_matches_nothing_sends_no_weather_request(monkeypatch):
    fake = qw.install(
        monkeypatch,
        {
            CITY: qw.problem(400, qw.NO_SUCH_LOCATION),
            POI: qw.problem(400, qw.NO_SUCH_LOCATION),
        },
    )

    shown = await _call(reality.check_current_weather, place="不存在")

    assert "查不到" in shown and "不存在" in shown
    assert fake.paths == [CITY, POI]


@pytest.mark.parametrize(
    ("tool_name", "argument", "value", "bounds"),
    [
        ("check_hourly_forecast", "hours", 0, "1 到 240"),
        ("check_hourly_forecast", "hours", 241, "1 到 240"),
        ("check_daily_forecast", "days", 0, "1 到 10"),
        ("check_daily_forecast", "days", 11, "1 到 10"),
    ],
)
async def test_an_out_of_range_span_tells_the_range_without_a_request(
    monkeypatch, tool_name, argument, value, bounds
):
    fake = qw.install(monkeypatch, {})

    outcome = await _call(getattr(reality, tool_name), place="甲", **{argument: value})

    assert outcome["kind"] == "invalid_args"
    assert bounds in outcome["message"]
    assert fake.calls == []


async def test_an_upstream_error_is_a_tool_error_without_the_key(monkeypatch):
    fake = qw.install(
        monkeypatch, {CITY: qw.problem(403, "Invalid Host"), POI: qw.ok(qw.POIS)}
    )

    outcome = await _call(reality.check_current_weather, place="甲")

    assert outcome["kind"] == "tool_error"
    assert "403 Invalid Host" in outcome["message"]
    assert qw.KEY not in str(outcome)
    assert fake.paths == [CITY]
