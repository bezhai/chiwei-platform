"""她做过的一张图：存得下、按句柄找得回、别人的看不见。

四条硬边界：

  * **存的是永久句柄，不是地址。** 预签名地址 1.5 小时就死，而她下一缝、下一天、
    下个月都可能想再看一眼那张图。所以库里那一列是 ``file_name``（对象存储的键），
    要看的时候现签。
  * **句柄是从 ``file_name`` 派生的，不是随机的。** 同一张图存两次撞同一条记录 ——
    工具重试、整轮重放都不该让她的记录里多出一张一模一样的图。
  * **按 ``(lane, persona_id)`` 隔离。** 这张表三个人共用，少了任何一半，一个从别处
    拿到的句柄就能取到姐姐画的图。
  * **取多少次都是同一张。** 她这一缝看一眼、下一缝再看一眼、发出去之前再取一次，
    每一次拿到的必须是同一个 ``file_name``；取过就没了的话她根本发不出去。
"""
from __future__ import annotations

import datetime as dt

import pytest

from app.living.pictures import (
    Picture,
    handle_for,
    her_picture,
    pictures_she_made,
    remember_a_picture,
)

LANE = "coe-living"
_CST = dt.timezone(dt.timedelta(hours=8))


def _at(hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 7, 25, hour, minute, tzinfo=_CST)


@pytest.fixture
async def pictures_db(real_pg_required, test_db):
    """只建她这张表 —— 她画图那一刻不碰会话、不碰手机、不碰任何别人的表。"""
    from tests.runtime.conftest import migrate

    await migrate(Picture, test_db)
    yield test_db


# ---------------------------------------------------------------------------
# 句柄本身（纯函数，不用库）
# ---------------------------------------------------------------------------


def test_the_same_file_always_gets_the_same_handle():
    """句柄由 ``file_name`` 派生 —— 同一张图算几遍都是同一串。

    随机句柄的后果是重放会多出一条记录：同一张图在她的记录里出现两次，而她分不出
    这是两张还是一张。
    """
    assert handle_for("temp/tos_abc_1234.jpg") == handle_for("temp/tos_abc_1234.jpg")


def test_two_different_files_never_share_a_handle():
    assert handle_for("temp/tos_abc_1234.jpg") != handle_for("temp/tos_abc_5678.jpg")


def test_the_handle_does_not_leak_the_storage_path():
    """句柄是 opaque 的：她照抄它就行，不该在里面读到对象存储的路径。"""
    handle = handle_for("temp/tos_abc_1234.jpg")
    assert "temp/" not in handle
    assert "tos_abc_1234" not in handle


# ---------------------------------------------------------------------------
# 存下来、按句柄取回来
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_picture_she_made_comes_back_by_its_handle(pictures_db):
    """按句柄取回来的那张，带着的是同一个永久句柄 ``file_name``。

    这一条是整件事的地基：``file_name`` 取不回来的话，"以后任何会话都能引用"根本
    不成立 —— 她手里只剩一个 1.5 小时后就死掉的地址。
    """
    made = await remember_a_picture(
        lane=LANE,
        persona_id="akao",
        file_name="temp/tos_cat_0001.jpg",
        what="一只在窗台上晒太阳的猫",
        made_at=_at(21, 30),
    )
    assert made.picture_id == handle_for("temp/tos_cat_0001.jpg")

    got = await her_picture(lane=LANE, persona_id="akao", picture_id=made.picture_id)
    assert got is not None
    assert got.file_name == "temp/tos_cat_0001.jpg"
    assert got.what == "一只在窗台上晒太阳的猫"
    assert got.made_at == _at(21, 30)


@pytest.mark.integration
async def test_the_same_picture_comes_back_as_many_times_as_she_asks(pictures_db):
    """取一次不是消费掉它 —— 看一眼、再看一眼、发出去，都得是同一张。"""
    made = await remember_a_picture(
        lane=LANE,
        persona_id="akao",
        file_name="temp/tos_cat_0001.jpg",
        what="一只猫",
        made_at=_at(21, 30),
    )
    for _ in range(3):
        got = await her_picture(
            lane=LANE, persona_id="akao", picture_id=made.picture_id
        )
        assert got is not None
        assert got.file_name == "temp/tos_cat_0001.jpg"


@pytest.mark.integration
async def test_a_handle_nobody_ever_made_is_just_not_there(pictures_db):
    """编出来的句柄如实说没有，不挑一张最近的顶上。"""
    got = await her_picture(
        lane=LANE, persona_id="akao", picture_id=handle_for("temp/never_made.jpg")
    )
    assert got is None


@pytest.mark.integration
async def test_the_same_file_remembered_twice_is_still_one_picture(pictures_db):
    """同一张图存两次只落一行 —— 工具重试、整轮重放不该让她多出一张。"""
    for _ in range(2):
        await remember_a_picture(
            lane=LANE,
            persona_id="akao",
            file_name="temp/tos_cat_0001.jpg",
            what="一只猫",
            made_at=_at(21, 30),
        )
    assert len(await pictures_she_made(lane=LANE, persona_id="akao")) == 1


# ---------------------------------------------------------------------------
# 隔离：这张表三个人共用
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_her_sister_cannot_reach_a_picture_she_made(pictures_db):
    """同一个泳道里的另一个人，拿着同一串句柄也取不到 —— 那不是她画的。"""
    made = await remember_a_picture(
        lane=LANE,
        persona_id="akao",
        file_name="temp/tos_cat_0001.jpg",
        what="一只猫",
        made_at=_at(21, 30),
    )
    assert await her_picture(
        lane=LANE, persona_id="ayana", picture_id=made.picture_id
    ) is None
    assert await pictures_she_made(lane=LANE, persona_id="ayana") == []


@pytest.mark.integration
async def test_another_lane_never_sees_a_picture_she_made(pictures_db):
    """泳道隔离是硬约束：prod 那条轴上的图不该在别的泳道被取到，反过来也一样。"""
    made = await remember_a_picture(
        lane=LANE,
        persona_id="akao",
        file_name="temp/tos_cat_0001.jpg",
        what="一只猫",
        made_at=_at(21, 30),
    )
    assert await her_picture(
        lane="prod", persona_id="akao", picture_id=made.picture_id
    ) is None
    assert await pictures_she_made(lane="prod", persona_id="akao") == []


@pytest.mark.integration
async def test_two_people_can_make_pictures_without_stepping_on_each_other(
    pictures_db,
):
    """同一张图两个人各存一次是**两条**记录，各自取各自那条。

    句柄从 ``file_name`` 派生，所以这里两条记录的句柄字面相同 —— 隔离必须由
    ``(lane, persona_id)`` 提供，不能指望句柄本身撞不上。
    """
    await remember_a_picture(
        lane=LANE,
        persona_id="akao",
        file_name="temp/tos_cat_0001.jpg",
        what="赤尾画的猫",
        made_at=_at(21, 30),
    )
    await remember_a_picture(
        lane=LANE,
        persona_id="ayana",
        file_name="temp/tos_cat_0001.jpg",
        what="绫奈画的猫",
        made_at=_at(21, 40),
    )
    handle = handle_for("temp/tos_cat_0001.jpg")
    hers = await her_picture(lane=LANE, persona_id="akao", picture_id=handle)
    sisters = await her_picture(lane=LANE, persona_id="ayana", picture_id=handle)
    assert hers is not None and sisters is not None
    assert hers.what == "赤尾画的猫"
    assert sisters.what == "绫奈画的猫"


# ---------------------------------------------------------------------------
# 列出她做过的（T2 那两只手要用）
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_what_she_made_comes_back_newest_first(pictures_db):
    """最近做的排在最前 —— 她说"刚才那张"时指的是最新那张。"""
    for hour, name in ((20, "a"), (21, "b"), (22, "c")):
        await remember_a_picture(
            lane=LANE,
            persona_id="akao",
            file_name=f"temp/tos_{name}.jpg",
            what=f"第 {name} 张",
            made_at=_at(hour),
        )
    got = await pictures_she_made(lane=LANE, persona_id="akao")
    assert [p.file_name for p in got] == [
        "temp/tos_c.jpg",
        "temp/tos_b.jpg",
        "temp/tos_a.jpg",
    ]


@pytest.mark.integration
async def test_the_list_stops_at_the_limit_but_the_handle_still_reaches_the_rest(
    pictures_db,
):
    """清单有上限，按句柄找那条路没有 —— 被挤下去的那些照样取得到。"""
    for i in range(5):
        await remember_a_picture(
            lane=LANE,
            persona_id="akao",
            file_name=f"temp/tos_{i}.jpg",
            what=f"第 {i} 张",
            made_at=_at(20, i),
        )
    got = await pictures_she_made(lane=LANE, persona_id="akao", limit=2)
    assert [p.file_name for p in got] == ["temp/tos_4.jpg", "temp/tos_3.jpg"]

    oldest = await her_picture(
        lane=LANE, persona_id="akao", picture_id=handle_for("temp/tos_0.jpg")
    )
    assert oldest is not None
    assert oldest.file_name == "temp/tos_0.jpg"


# ---------------------------------------------------------------------------
# 写进去的时刻必须带时区
# ---------------------------------------------------------------------------


def test_a_naive_moment_is_refused_before_it_reaches_the_column():
    """不带 tzinfo 的时刻落进 TIMESTAMPTZ 会被按服务器时区解释，静默偏几小时。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="时区"):
        Picture(
            lane=LANE,
            persona_id="akao",
            picture_id="p1",
            file_name="temp/tos_a.jpg",
            what="一只猫",
            made_at=dt.datetime(2026, 7, 25, 21, 30),
        )
