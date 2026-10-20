"""记录的两个插件：``records`` 只有读记录的两个查询工具，是开关（``world_sources``）能关掉的那个；
``record_keeping`` 是写记录、主 agent 的记录目录、写记录那一段 prompt 和人工读写接口，不随开关关掉。

起 world 的宿主（不碰 IO），按插件看它登记了什么；工具在绑好的一次调用里直接调，不起模型。
"""
from __future__ import annotations

import pytest

from app.agent.round import Call, in_call
from app.world import records
from app.world.records import keeping, reading


async def _call(t, **arguments):
    return await t.invoke(arguments)


async def _registered_by(app_host, plugin: str) -> list[tuple[str, str, object]]:
    """world 的宿主起来时（不碰 IO），插件 ``plugin`` 登记的东西：(类型, 名字, 细节)，按登记的先后。"""
    host = await app_host("world")
    return [(r.kind, r.name, r.detail) for r in host.registered() if r.plugin == plugin]


# ---------------------------------------------------------------------------
# 各自登记了什么
# ---------------------------------------------------------------------------


async def test_records_registers_only_the_two_query_tools_as_material(app_host):
    registered = await _registered_by(app_host, "records")

    assert reading.PLUGIN.name == "records"
    assert [(kind, name, d["spec"].trim) for kind, name, d in registered] == [
        ("tool", "list_records", "material"),
        ("tool", "read_record", "material"),
    ]


async def test_record_keeping_registers_writing_the_listing_its_segment_and_the_routes(
    app_host,
):
    registered = await _registered_by(app_host, "record_keeping")

    assert keeping.PLUGIN.name == "record_keeping"
    assert [(kind, name) for kind, name, _ in registered] == [
        ("segment", "world_round/write-record"),
        ("tool", "write_record"),
        ("section", "records.listing"),
        ("route", "GET /admin/world/records"),
        ("route", "GET /admin/world/records/document"),
        ("route", "PUT /admin/world/records/document"),
        ("route", "DELETE /admin/world/records/document"),
    ]
    (write,) = [d["spec"] for kind, _, d in registered if kind == "tool"]
    assert write.trim == "kept"


# ---------------------------------------------------------------------------
# records：读的那一半
# ---------------------------------------------------------------------------


@pytest.fixture
def reads(volume):
    """主 agent 一轮里那样：这一次调用的状态记着读过什么。"""
    call = Call(agent="world_round", session=None, round=None)
    with in_call(call):
        yield call.state(reading.RecordsRead).fingerprints


async def test_list_records_shows_paths_and_sizes(reads):
    assert "还没有任何记录" in await _call(reading.list_records)

    records.write("地方/甲.md", "一二三。", expected=None)
    shown = await _call(reading.list_records)

    assert "地方/甲.md" in shown and "4 字" in shown


async def test_reading_a_record_notes_its_fingerprint_for_this_call(reads):
    written = records.write("地方/甲.md", "旧的样子。", expected=None)

    shown = await _call(reading.read_record, path="地方/甲.md")

    assert "旧的样子。" in shown
    assert reads == {"地方/甲.md": written.fingerprint}


async def test_what_another_call_reads_is_noted_on_that_call_only(volume):
    """感知判断、NPC、应答读到的记在它们自己那次调用上，不落到主 agent 那次调用里。"""
    records.write("地方/甲.md", "旧的样子。", expected=None)
    main = Call(agent="world_round", session=None, round=None)
    perception = Call(agent="world_perception", session=None, round=None)

    with in_call(main):
        with in_call(perception):
            shown = await _call(reading.read_record, path="地方/甲.md")

    assert "旧的样子。" in shown
    assert main.state(reading.RecordsRead).fingerprints == {}
    assert list(perception.state(reading.RecordsRead).fingerprints) == ["地方/甲.md"]


async def test_reading_a_missing_record_says_so(reads):
    result = await _call(reading.read_record, path="没有/这份.md")

    assert "没有" in str(result)
    assert reads == {}


