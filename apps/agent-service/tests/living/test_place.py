"""位置比对是规则不是模型判断——三档 reach 的纯函数测试。

地点写成层级路径（``家/客厅``、``家/楼上/绫奈房间``、``学校``）。
比对只看路径：完全相同 = 同一地点、同一栋的不同房间 = 只知道有动静、
根不同 = 够不着。
"""
from __future__ import annotations

from app.living.place import (
    EVERYWHERE,
    Reach,
    reach_between,
    reach_between_people,
)


def test_identical_place_is_same_place():
    assert reach_between(observer="家/客厅", happening="家/客厅") is Reach.SAME_PLACE


def test_same_building_different_room_only_hears_a_noise():
    assert (
        reach_between(observer="家/楼上/绫奈房间", happening="家/客厅")
        is Reach.SAME_BUILDING
    )


def test_different_building_is_out_of_reach():
    assert reach_between(observer="学校", happening="家/客厅") is Reach.OUT_OF_REACH


def test_root_only_place_matches_itself():
    assert reach_between(observer="学校", happening="学校") is Reach.SAME_PLACE


def test_parent_and_child_path_are_not_the_same_place():
    """``家`` 和 ``家/客厅`` 不是同一地点，但同属一栋。"""
    assert reach_between(observer="家", happening="家/客厅") is Reach.SAME_BUILDING


def test_an_event_covering_a_whole_area_reaches_everyone_inside_it():
    """事情发生在 ``家`` 这个范围上（天黑、停电、饭菜的味道）—— 屋里的人都在场。

    没有这一档，日历里所有不绑房间的客观时刻（T3）就只能被裁成"那边有动静"，
    她一辈子读不到"天黑了"这四个字。
    """
    assert reach_between(observer="家/客厅", happening="家") is Reach.SAME_PLACE
    assert (
        reach_between(observer="家/楼上/绫奈房间", happening="家") is Reach.SAME_PLACE
    )
    assert reach_between(observer="学校", happening="家") is Reach.OUT_OF_REACH


def test_a_coarser_observer_position_is_not_promoted_to_being_there():
    """反过来不成立：只知道她"在家"，就不知道她在不在客厅 —— 仍然只是同一栋。

    这一档是 fail-closed 的（跟"定位不到她 = 够不着"同一条纪律）：位置数据粗，
    宁可让她少听见一句旁听，也不能凭一个模糊位置就判她在场。
    """
    assert reach_between(observer="家", happening="家/客厅") is Reach.SAME_BUILDING
    assert (
        reach_between(observer="家/楼上", happening="家/楼上/绫奈房间")
        is Reach.SAME_BUILDING
    )


def test_a_shared_prefix_segment_is_not_containment():
    """``家/客厅`` 不在 ``家/客`` 里 —— 比的是路径的段，不是字符串前缀。"""
    assert reach_between(observer="家/客厅", happening="家/客") is Reach.SAME_BUILDING


# ---------------------------------------------------------------------------
# 第三档：全局。
#
# 上面那条覆盖档只覆盖得到**一栋**：事情发生在 ``家`` 上，学校里的人够不着。可是天黑、
# 台风、今天是什么节气这些事没有"一栋"—— 它们笼罩所有地方。在只有点位置的模型里，这种
# 事只能硬塞一个地点，而实测 19.1% 的记录发生在 ``家`` 以外（学校 723、小区 156、老街
# 48），于是她在那些地方时一条都收不到。
# ---------------------------------------------------------------------------


def test_a_global_event_reaches_everyone_wherever_they_are():
    """天黑了、台风来了 —— 在哪都算在场。"""
    assert reach_between(observer="家/客厅", happening=EVERYWHERE) is Reach.SAME_PLACE
    assert reach_between(observer="学校/操场", happening=EVERYWHERE) is Reach.SAME_PLACE
    assert reach_between(observer="老街", happening=EVERYWHERE) is Reach.SAME_PLACE


def test_a_place_that_was_never_recorded_is_still_out_of_reach():
    """**"没记下地点"和"笼罩所有地方"是两件事。**

    这条是区分性的：把空地点顺手当成全局，任何一处忘填 place 的写入都会变成全世界都
    听见，而且一句报错都没有。全局必须是显式写下的那一个值。
    """
    assert reach_between(observer="家/客厅", happening="") is Reach.OUT_OF_REACH
    assert reach_between(observer="家/客厅", happening="   ") is Reach.OUT_OF_REACH


def test_a_global_event_still_does_not_reach_someone_who_cannot_be_located():
    """定位不到她就没有"在场"这个前提 —— 全局档不该把这条 fail-closed 打掉。"""
    assert reach_between(observer=None, happening=EVERYWHERE) is Reach.OUT_OF_REACH
    assert reach_between(observer="", happening=EVERYWHERE) is Reach.OUT_OF_REACH


def test_a_person_standing_everywhere_is_not_standing_anywhere():
    """观察者那一侧写成全局不等于她无处不在 —— 那个值只描述事件的范围。"""
    assert reach_between(observer=EVERYWHERE, happening="家/客厅") is Reach.OUT_OF_REACH


def test_unknown_observer_place_is_out_of_reach():
    """定位不到她（从没写过 whereabouts）时旁听一律够不着——定向送达不走这条路。"""
    assert reach_between(observer=None, happening="家/客厅") is Reach.OUT_OF_REACH


def test_trailing_slash_and_whitespace_do_not_change_the_verdict():
    assert (
        reach_between(observer=" 家/客厅/ ", happening="家/客厅") is Reach.SAME_PLACE
    )


# ---------------------------------------------------------------------------
# 人跟人比位置：**不是**同一条规则。
#
# 上面那条覆盖档（事情发生在 ``家`` 这一整片 → 站在 ``家/客厅`` 的人在场）是给
# **范围事件**用的：天黑、停电、饭菜的味道确实笼罩整栋。人不是范围——"绫奈在家"
# 不代表她跟站在客厅的赤尾同处一室。拿覆盖档去比两个人，会让一个只粗略定位到
# ``家`` 的人被判成"就在你旁边"，她在做什么就此泄露出去。
# ---------------------------------------------------------------------------


def test_two_people_in_the_very_same_spot_are_together():
    assert reach_between_people(observer="家/客厅", other="家/客厅") is Reach.SAME_PLACE


def test_a_coarsely_located_person_is_never_in_the_same_room():
    """她只定位到 ``家``、我在 ``家/客厅`` —— 我不知道她是不是就在这屋里。

    这条要 fail-closed：判成同处一室，她正在做什么会被 ``look_around`` 直接吐出来。
    """
    assert reach_between_people(observer="家/客厅", other="家") is Reach.SAME_BUILDING
    assert reach_between_people(observer="家", other="家/客厅") is Reach.SAME_BUILDING


def test_two_people_in_different_rooms_only_share_the_house():
    assert (
        reach_between_people(observer="家/客厅", other="家/楼上/绫奈房间")
        is Reach.SAME_BUILDING
    )


def test_someone_out_of_the_house_is_out_of_reach():
    assert (
        reach_between_people(observer="家/客厅", other="学校/图书馆")
        is Reach.OUT_OF_REACH
    )


def test_someone_who_cannot_be_located_is_out_of_reach():
    assert reach_between_people(observer="家/客厅", other=None) is Reach.OUT_OF_REACH
    assert reach_between_people(observer=None, other="家/客厅") is Reach.OUT_OF_REACH


def test_nobody_is_everywhere():
    """全局是**事件**的范围，人没有这一档。

    真让它漏进来：一个位置写成全局的人会被判成跟所有人同处一室，``look_around``
    直接把每个人正在做什么吐出来 —— 跟"粗定位不许升格成在场"是同一条纪律。
    """
    assert (
        reach_between_people(observer="家/客厅", other=EVERYWHERE)
        is Reach.OUT_OF_REACH
    )
    assert (
        reach_between_people(observer=EVERYWHERE, other="家/客厅")
        is Reach.OUT_OF_REACH
    )


def test_whitespace_does_not_change_the_verdict_between_people():
    assert (
        reach_between_people(observer=" 家/客厅/ ", other="家/客厅") is Reach.SAME_PLACE
    )
