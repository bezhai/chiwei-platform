"""知识来源"别人告诉世界的事"：收哪些、怎么按发送方查、同一条再来不重复，以及跟记录分开放。

收件处理和工具都直接调，不起模型；最后两条走一遍收件箱的处理函数，看它在一轮之前先把消息
交给来源。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.agent.round import Call, in_call
from app.messaging.message import Kind, Message, new_message
from app.world import main_agent, records, told
from app.world.agents import when

from .conftest import LANE, sets_nothing, sets_wake


def _said(sender: str, body: str, *, minutes_ago: int = 0, message_id: str | None = None) -> Message:
    """一条发给 world 的普通消息。直接造，不经过参与者名字的校验：名字规则不归这里管。"""
    return Message(
        message_id=message_id or f"m-{sender.encode().hex()}-{minutes_ago}",
        sender=sender,
        recipient="world",
        time=datetime.now(UTC) - timedelta(minutes=minutes_ago),
        kind=Kind.MESSAGE,
        wakes_recipient=True,
        body=body,
    )


async def _call(t, **arguments):
    with in_call(Call(agent="world_answer", session=None, round=None)):
        return await t.invoke(arguments)


def _stored() -> list:
    root = told.stored_dir()
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


# ---------------------------------------------------------------------------
# 插件
# ---------------------------------------------------------------------------


class _Setup:
    """told 的 setup 拿到的东西的替身：``world_intake`` 服务记下登记的收件处理，工具和停下时要做的
    事记下来。"""

    def __init__(self) -> None:
        self.intakes: list[tuple[str, object]] = []
        self.tools: list[tuple[str, str]] = []
        self.on_stops: list[object] = []

    # world_intake
    def register(self, plugin, intake):
        self.intakes.append((plugin, intake))

        async def dispose() -> None:
            self.intakes.remove((plugin, intake))

        return dispose

    # the plugin's context
    def service(self, name):
        assert name == "world_intake"
        return self

    def tool(self, t, *, trim):
        self.tools.append((t.name, trim))

    def on_stop(self, undo):
        self.on_stops.append(undo)


async def test_told_registers_its_intake_with_world_intake_and_its_two_query_tools():
    setup = _Setup()

    told.PLUGIN.setup(setup)

    assert told.PLUGIN.requires == ("world_intake",)
    assert setup.intakes == [("told", told.take_in)]
    assert setup.tools == [("list_senders", "material"), ("read_messages_from", "material")]
    for undo in setup.on_stops:
        await undo()
    assert setup.intakes == []


def test_it_keeps_what_it_is_told_in_its_own_directory_next_to_the_records(bare_volume):
    assert told.stored_dir() == bare_volume / LANE / "sources" / "told"
    assert records.records_root() == bare_volume / LANE / "records"


# ---------------------------------------------------------------------------
# 收哪些
# ---------------------------------------------------------------------------


async def test_a_message_someone_sent_world_is_kept_and_can_be_read_back(volume):
    await told.take_in(_said("ayana", "我在学校，把画拿给老师看了。"))

    shown = await _call(told.read_messages_from, name="ayana")

    assert "我在学校，把画拿给老师看了。" in shown


async def test_worlds_own_wakes_and_not_delivered_notices_are_not_kept(volume):
    wake = new_message(sender="world", recipient="world", body="该看看外面了。", kind=Kind.MESSAGE)
    bounced = new_message(
        sender="world", recipient="world", body="你的消息没有送达。", kind=Kind.NOT_DELIVERED
    )

    await told.take_in(wake)
    await told.take_in(bounced)

    assert _stored() == []
    assert "还没有人" in await _call(told.list_senders)


async def test_the_same_message_delivered_twice_is_kept_once(volume):
    message = _said("ayana", "我出门了。", message_id="same-id")

    await told.take_in(message)
    await told.take_in(message)

    assert len(_stored()) == 1
    assert "1 条" in await _call(told.list_senders)


async def test_the_same_message_id_sent_again_later_is_kept_once_with_its_first_time(volume):
    """发送方按 ``SendFailed`` 带回的 id 重发：同一个 id，消息时间却是重发那一刻的。"""
    first = _said("ayana", "我出门了。", minutes_ago=30, message_id="same-id")
    resent = _said("ayana", "我出门了。", minutes_ago=0, message_id="same-id")

    await told.take_in(first)
    await told.take_in(resent)

    assert len(_stored()) == 1
    assert "1 条" in await _call(told.list_senders)
    shown = await _call(told.read_messages_from, name="ayana")
    assert shown.count("我出门了。") == 1
    assert when(first.time) in shown


async def test_what_it_keeps_stays_apart_from_worlds_records(volume):
    await told.take_in(_said("ayana", "我在厨房。"))

    assert records.listing() == []
    assert all(told.stored_dir() in p.parents for p in _stored())


# ---------------------------------------------------------------------------
# 怎么查
# ---------------------------------------------------------------------------


async def test_each_senders_messages_are_read_back_newest_first_and_only_theirs(volume):
    await told.take_in(_said("ayana", "早上在家。", minutes_ago=90))
    await told.take_in(_said("ayana", "中午到了学校。", minutes_ago=30))
    await told.take_in(_said("akao", "我在厨房。", minutes_ago=10))

    shown = await _call(told.read_messages_from, name="ayana")

    assert shown.index("中午到了学校。") < shown.index("早上在家。")
    assert "我在厨房。" not in shown


async def test_recent_means_by_the_time_the_message_was_sent_not_by_its_id(volume, monkeypatch):
    monkeypatch.setattr(told, "RECENT", 2)
    await told.take_in(_said("ayana", "最早。", minutes_ago=90, message_id="c"))
    await told.take_in(_said("ayana", "最晚。", minutes_ago=10, message_id="a"))
    await told.take_in(_said("ayana", "中间。", minutes_ago=50, message_id="b"))

    shown = await _call(told.read_messages_from, name="ayana")

    assert "最早。" not in shown
    assert shown.index("最晚。") < shown.index("中间。")
    assert "共 3 条" in shown


async def test_list_senders_names_everyone_who_has_sent_something(volume):
    await told.take_in(_said("ayana", "早上在家。", minutes_ago=90))
    await told.take_in(_said("ayana", "中午到了学校。", minutes_ago=30))
    await told.take_in(_said("akao", "我在厨房。", minutes_ago=10))

    shown = await _call(told.list_senders)

    assert "ayana" in shown and "2 条" in shown
    assert "akao" in shown and "1 条" in shown


async def test_asking_about_someone_who_never_sent_anything_says_so_and_lists_who_did(volume):
    await told.take_in(_said("akao", "我在厨房。"))

    shown = await _call(told.read_messages_from, name="ayana")

    assert "没有收到过" in shown and "akao" in shown


@pytest.mark.parametrize("name", ["赤尾", "a/b", ".."])
async def test_any_name_the_messaging_layer_lets_through_is_stored_safely(volume, name):
    await told.take_in(_said(name, "我在这儿。"))

    root = told.stored_dir()
    assert all(root in p.parents for p in _stored())
    assert "我在这儿。" in await _call(told.read_messages_from, name=name)
    assert name in await _call(told.list_senders)


# ---------------------------------------------------------------------------
# 收件箱先交给来源，再跑一轮
# ---------------------------------------------------------------------------


async def test_the_inbox_hands_a_message_to_the_sources_before_the_round(world):
    message = new_message(sender="ayana", recipient="world", body="我在厨房。", kind=Kind.MESSAGE)
    seen_by_round: list[str] = []

    async def plan():
        seen_by_round.append(await told.read_messages_from.invoke({"name": "ayana"}))
        return await sets_wake()()

    world.runner.plan = plan

    await world.deliver(message)

    assert "我在厨房。" in seen_by_round[0]


async def test_a_round_that_fails_and_runs_again_keeps_the_message_once(world):
    message = new_message(sender="ayana", recipient="world", body="我在厨房。", kind=Kind.MESSAGE)
    world.runner.plan = sets_nothing()

    with pytest.raises(main_agent.NoNextWake):
        await world.deliver(message)
    world.runner.plan = sets_wake()
    await world.deliver(message)

    assert len(_stored()) == 1
