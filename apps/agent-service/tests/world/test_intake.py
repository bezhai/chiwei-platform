"""world 收件箱的 ``world_intake`` 服务（world 核心提供，:class:`app.world.intake.Intakes`）：

* 收件处理：每收到一条普通消息，交给登记过的、这一刻没被开关关掉的插件，按登记的先后。开关
  （清单 ``world`` 的 ``switches``）在每条消息来时读一次；开关不管的插件的收件处理总在。
* 提问：交给登记的那一个应答，最多一个；没有登记时就是"什么都没说"。world 核心不依赖应答插件。

Dynamic Config 是替身。
"""
from __future__ import annotations

import pytest

from app.agent.contracts import Switches
from app.messaging.message import Kind, new_message
from app.world.intake import Intakes

SWITCHES = Switches("world_sources", ("told", "tides"))


@pytest.fixture
def enabled(monkeypatch):
    """Dynamic Config 里 ``world_sources`` 的值，用例改它；每读一次记一笔。"""
    from inner_shared.dynamic_config import dynamic_config

    config = {"value": "", "reads": 0}

    def get(key, default=""):
        if key != SWITCHES.key:
            return default
        config["reads"] += 1
        return config["value"]

    monkeypatch.setattr(dynamic_config, "get", get)
    return config


def _message(kind: Kind = Kind.MESSAGE):
    return new_message(sender="ayana", recipient="world", body="我在厨房。", kind=kind)


def _keeper(seen: list[str], name: str):
    async def intake(message) -> None:
        seen.append(f"{name}:{message.message_id}")

    return intake


async def test_a_message_goes_to_each_intake_that_is_on_in_the_order_they_registered(enabled):
    seen: list[str] = []
    intakes = Intakes(SWITCHES)
    intakes.register("tides", _keeper(seen, "tides"))
    intakes.register("ledger", _keeper(seen, "ledger"))
    intakes.register("told", _keeper(seen, "told"))
    enabled["value"] = "told"
    message = _message()

    await intakes.take_in(message)

    # tides 被关掉了；ledger 不归开关管，总在。
    assert seen == [f"ledger:{message.message_id}", f"told:{message.message_id}"]


async def test_the_switches_are_read_for_each_message(enabled):
    seen: list[str] = []
    intakes = Intakes(SWITCHES)
    intakes.register("tides", _keeper(seen, "tides"))
    first, second = _message(), _message()

    await intakes.take_in(first)
    enabled["value"] = "told"
    await intakes.take_in(second)

    assert seen == [f"tides:{first.message_id}"]
    assert enabled["reads"] == 2


async def test_a_disposed_intake_takes_in_nothing(enabled):
    seen: list[str] = []
    intakes = Intakes(SWITCHES)
    dispose = intakes.register("told", _keeper(seen, "told"))

    await dispose()
    await intakes.take_in(_message())

    assert seen == []


async def test_an_intake_that_fails_fails_the_take_in(enabled):
    async def broken(_message) -> None:
        raise OSError("volume is gone")

    intakes = Intakes(SWITCHES)
    intakes.register("told", broken)

    with pytest.raises(OSError):
        await intakes.take_in(_message())


async def test_a_question_goes_to_the_answer_registered_when_it_is_asked():
    intakes = Intakes(SWITCHES)
    question = _message(Kind.QUESTION)

    assert await intakes.answer(question) is None

    async def answer(asked) -> str:
        return f"回答 {asked.body}"

    dispose = intakes.answer_with("answer", answer)
    assert await intakes.answer(question) == "回答 我在厨房。"

    await dispose()
    assert await intakes.answer(question) is None


async def test_only_one_plugin_answers_questions():
    async def answer(_asked) -> str:
        return "嗯。"

    intakes = Intakes(SWITCHES)
    intakes.answer_with("answer", answer)

    with pytest.raises(ValueError, match="'answer'"):
        intakes.answer_with("oracle", answer)
