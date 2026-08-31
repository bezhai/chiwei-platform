"""位置比对是规则不是模型判断——三档 reach 的纯函数测试。

地点写成层级路径（``家/客厅``、``家/楼上/绫奈房间``、``学校``）。
比对只看路径：完全相同 = 同一地点、同一栋的不同房间 = 只知道有动静、
根不同 = 够不着。
"""
from __future__ import annotations

from app.living.place import Reach, reach_between, reach_between_people


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


def test_whitespace_does_not_change_the_verdict_between_people():
    assert (
        reach_between_people(observer=" 家/客厅/ ", other="家/客厅") is Reach.SAME_PLACE
    )
