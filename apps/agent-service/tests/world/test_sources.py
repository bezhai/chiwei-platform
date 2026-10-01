"""知识来源：怎么登记、启用哪些、四个 agent 拿到的只读工具从哪来，以及记录、现实这两个来源。

工具在 ``agent_context`` 里直接调，不起模型。
"""
from __future__ import annotations

import pytest

from app.agent.context import AgentContext
from app.agent.runtime_context import agent_context
from app.agent.tooling import tool
from app.messaging.message import Kind, new_message
from app.world import records, sources
from app.world.sources import Source, private_dir, reality
from app.world.sources import records as records_source

from .conftest import LANE, load_world_wiring


@tool
async def look_up_tides() -> str:
    """查潮汐。"""
    return "涨潮。"


@pytest.fixture
def registered(monkeypatch):
    """接线登记过的那几个来源；Dynamic Config 按 ``config`` 里给的值，没给就是没配。"""
    from inner_shared.dynamic_config import dynamic_config

    config: dict[str, str] = {}
    monkeypatch.setattr(
        dynamic_config, "get", lambda k, default="": config.get(k, default)
    )
    load_world_wiring()
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
    assert {"list_records", "read_record", "check_weather", "search_web"} <= set(tools)


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

    assert {"list_records", "read_record", "check_weather", "search_web"} <= (
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


def test_the_reality_source_is_weather_and_web_search():
    from app.agent.tools.search import search_web

    assert reality.SOURCE.name == "reality"
    assert list(reality.SOURCE.tools) == [reality.check_weather, search_web]
    assert reality.SOURCE.intake is None


async def test_check_weather_shows_what_qweather_matched_and_its_readings(monkeypatch):
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

    shown = await _call(reality.check_weather, place="甲")

    assert "甲市" in shown and "甲省" in shown
    assert "甲区" in shown  # 同名的候选一并交回去
    assert "小雨" in shown and "中雨" in shown and "s2" in shown


async def test_check_weather_for_a_name_that_matches_nothing(monkeypatch):
    from app.capabilities import weather

    async def find_places(name):
        return []

    monkeypatch.setattr(weather, "find_places", find_places)

    assert "查不到" in await _call(reality.check_weather, place="不存在")
