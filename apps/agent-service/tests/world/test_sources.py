"""知识来源的登记表：怎么登记、启用哪些、收件处理交给谁。
"""
from __future__ import annotations

import pytest

from app.agent.tooling import tool
from app.messaging.message import Kind, new_message
from app.world import records, sources
from app.world.sources import Source, private_dir, told

from .conftest import LANE


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
    yield config
    # 用例直接登记的来源不属于哪个插件，宿主停下时不会拿掉它们。
    sources.clear_sources()


async def _call(t, **arguments):
    return await t.invoke(arguments)


# ---------------------------------------------------------------------------
# 登记与启用
# ---------------------------------------------------------------------------


async def test_without_dynamic_config_every_registered_source_is_enabled(registered):
    names = [s.name for s in await sources.enabled_sources()]

    assert names == ["told"]
    tools = [spec for s in await sources.enabled_sources() for spec in s.query_tools]
    assert {t.tool.name for t in tools} == {"list_senders", "read_messages_from"}
    assert {t.trim for t in tools} == {"material"}


async def test_the_enabled_list_keeps_only_the_named_sources_in_registration_order(registered):
    sources.register(Source(name="tides", tools=(look_up_tides,)))
    registered[sources.ENABLED_SOURCES_KEY] = " tides , told ,没有这个"

    assert [s.name for s in await sources.enabled_sources()] == ["told", "tides"]


async def test_a_source_left_out_of_the_enabled_list_is_not_enabled(registered):
    sources.register(Source(name="tides", tools=(look_up_tides,)))
    registered[sources.ENABLED_SOURCES_KEY] = "tides"

    assert [s.name for s in await sources.enabled_sources()] == ["tides"]


def test_two_sources_cannot_share_a_name_or_a_tool_name(registered):
    with pytest.raises(ValueError):
        sources.register(Source(name="told", tools=(look_up_tides,)))
    with pytest.raises(ValueError):
        sources.register(Source(name="copy", tools=(told.list_senders,)))


async def test_intake_goes_to_each_enabled_source_that_has_one(registered):
    seen: dict[str, list[str]] = {"a": [], "b": []}

    def keeper(name):
        async def intake(message):
            seen[name].append(message.message_id)

        return intake

    sources.register(Source(name="a", tools=(), intake=keeper("a")))
    sources.register(Source(name="b", tools=(), intake=keeper("b")))
    registered[sources.ENABLED_SOURCES_KEY] = "a"
    message = new_message(sender="ayana", recipient="world", body="x", kind=Kind.MESSAGE)

    await sources.take_in(message)

    assert seen == {"a": [message.message_id], "b": []}


def test_each_source_keeps_its_own_directory_next_to_the_records(bare_volume):
    assert private_dir("tides") == bare_volume / LANE / "sources" / "tides"
    assert records.records_root() == bare_volume / LANE / "records"
