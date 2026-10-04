"""三姐妹在通信机制里的名字：取自人设表的显示名，启动时先检查，有问题就不开收件箱。

名字就是地址：一个名字错了，发给她的消息就进不了她的收件箱，或者进了别人的。所以每一种
拒绝情况各有一条用例，而且报错要说得出是哪个人、哪里不对。人设表在这里换成替身——检查
本身不碰库，库里的那一行长什么样才是输入。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.living import participants as participants_mod
from app.living.participants import WORLD, load_residents, residents
from app.living.persona import LIVING_PERSONAS
from app.messaging.operator import OPERATOR

_GOOD = {"akao": "赤尾", "ayana": "绫奈", "chinagi": "千凪"}


@pytest.fixture
def persona_table(monkeypatch):
    """人设表的替身：``rows[persona_id]`` 是这一行的显示名，``None`` 表示没有这一行。"""
    rows: dict[str, str | None] = dict(_GOOD)

    async def find_persona(persona_id: str):
        name = rows.get(persona_id)
        if name is None:
            return None
        return SimpleNamespace(persona_id=persona_id, display_name=name)

    monkeypatch.setattr(participants_mod, "find_persona", find_persona)
    monkeypatch.setattr(participants_mod, "_known", None)
    return rows


async def test_each_resident_is_named_after_her_display_name(persona_table):
    known = await load_residents()

    assert known.by_persona == _GOOD
    assert known.persona_of("绫奈") == "ayana"
    assert known.persona_of(WORLD) is None
    assert residents() is known, "开收件箱和以后查地址要用同一份对照"


async def test_the_names_are_read_from_the_persona_table_not_written_in_code(persona_table):
    persona_table["akao"] = "赤尾酱"

    known = await load_residents()

    assert known.by_persona["akao"] == "赤尾酱"


def test_the_names_are_unknown_until_they_are_read(monkeypatch):
    monkeypatch.setattr(participants_mod, "_known", None)

    with pytest.raises(RuntimeError, match="还没读"):
        residents()


async def test_a_missing_persona_row_refuses_to_start(persona_table):
    persona_table["ayana"] = None

    with pytest.raises(RuntimeError, match="ayana") as refused:
        await load_residents()

    assert "没有这一行" in str(refused.value)


@pytest.mark.parametrize("empty", ["", "   "])
async def test_an_empty_display_name_refuses_to_start(persona_table, empty):
    persona_table["chinagi"] = empty

    with pytest.raises(RuntimeError, match="chinagi") as refused:
        await load_residents()

    assert "显示名是空的" in str(refused.value)


@pytest.mark.parametrize("bad", ["赤 尾", "赤.尾", "-赤尾", "赤尾/姐姐", "赤:尾"])
async def test_a_name_messaging_cannot_carry_refuses_to_start(persona_table, bad):
    persona_table["akao"] = bad

    with pytest.raises(RuntimeError, match="akao") as refused:
        await load_residents()

    assert "名字规则" in str(refused.value)


async def test_two_residents_with_the_same_name_refuse_to_start(persona_table):
    persona_table["chinagi"] = "绫奈"

    with pytest.raises(RuntimeError, match="绫奈") as refused:
        await load_residents()

    assert "ayana" in str(refused.value) and "chinagi" in str(refused.value)


@pytest.mark.parametrize("taken", [WORLD, OPERATOR])
async def test_a_name_another_participant_already_has_refuses_to_start(persona_table, taken):
    persona_table["ayana"] = taken

    with pytest.raises(RuntimeError, match="ayana") as refused:
        await load_residents()

    assert "已有的参与者" in str(refused.value)


async def test_every_problem_is_reported_at_once_and_nothing_is_kept(persona_table):
    """几处都错时一次报全，改一处、重启、再撞下一处太慢；报错之后也不留半份对照。"""
    persona_table["akao"] = None
    persona_table["chinagi"] = ""

    with pytest.raises(RuntimeError) as refused:
        await load_residents()

    assert "akao" in str(refused.value) and "chinagi" in str(refused.value)
    with pytest.raises(RuntimeError, match="还没读"):
        residents()


def test_life_knows_world_by_the_name_world_gives_itself():
    """life 和 world 互不 import，world 的名字两边各写一份；这里把两份钉在一起。"""
    from app.world.wake import WORLD as world_calls_itself

    assert WORLD == world_calls_itself


def test_every_living_persona_is_checked():
    """检查的范围就是住在这个家里的那几个人，不多不少。"""
    assert set(_GOOD) == set(LIVING_PERSONAS)
