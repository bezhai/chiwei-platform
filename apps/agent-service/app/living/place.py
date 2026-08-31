"""位置比对——规则，不是模型判断。

地点写成层级路径，段之间用 ``/``：``家/客厅``、``家/楼上/绫奈房间``、``学校``。
第一段是"哪一栋"，往后是这栋里的哪儿。

三档：

  * :attr:`Reach.SAME_PLACE`     同一地点 —— 听得见原话
  * :attr:`Reach.SAME_BUILDING`  同一栋的不同地方 —— 只知道有动静，没有内容
  * :attr:`Reach.OUT_OF_REACH`   够不着 —— 什么都没有

**"同一地点"包含"事情发生在一整片范围上"这种情况，而且只朝一个方向包含。**
天黑、停电、饭菜的味道发生在 ``家`` 这一整片上，站在 ``家/客厅`` 的人就在这片
里、就在场（:attr:`Reach.SAME_PLACE`）。反过来不成立：只知道她"在家"、事情发生在
``家/客厅`` 时，她在不在客厅是不知道的，仍然只算同一栋——跟"定位不到她一律够不着"
同一条 fail-closed 纪律，位置数据粗的时候宁可让她少听见一句旁听，不能凭一个模糊
位置判她在场。
没有这一档，所有不绑房间的客观时刻（日历里的天亮天黑）就只能被裁成"那边有动静"，
她一辈子读不到"天黑了"这四个字。

为什么是路径而不是"房间表 + 邻接关系"：这三档是**旁听**要回答的全部问题，再细
的空间模型（门开着没有、隔音怎么样）会立刻变成一个需要维护的世界几何，而它换不
来任何她能感知到的差别。定向说话（``addressee``）根本不走这条路——那条一定送到，
所以位置模型算错的代价被封在"旁听听不听得见"这一格里。
"""

from __future__ import annotations

from enum import StrEnum

_SEP = "/"


class Reach(StrEnum):
    """一个观察者相对一件事发生地的三档可及性。"""

    SAME_PLACE = "same_place"
    SAME_BUILDING = "same_building"
    OUT_OF_REACH = "out_of_reach"


def _normalize(path: str) -> str:
    """去掉首尾空白 / 多余分隔符 / 段内空白，得到可比较的规范路径。"""
    segments = [seg.strip() for seg in path.strip().split(_SEP)]
    return _SEP.join(seg for seg in segments if seg)


def reach_between(*, observer: str | None, happening: str) -> Reach:
    """观察者站在 ``observer`` 时，对发生在 ``happening`` 的事够得着几分。

    ``observer`` 为 ``None`` / 空（从没写过 whereabouts、定位不到她）一律
    :attr:`Reach.OUT_OF_REACH`——旁听是"她在场所以感知到了"，定位不到就没有在场
    这个前提。定向送达不经过这里，所以位置缺失不会让一句对她说的话丢掉。
    """
    if not observer:
        return Reach.OUT_OF_REACH
    here = _normalize(observer).split(_SEP)
    there = _normalize(happening).split(_SEP)
    if not here[0] or not there[0]:
        return Reach.OUT_OF_REACH
    if here[: len(there)] == there:
        # 相等，或者事情发生在一整片范围上而她正站在这片里面 —— 都是在场。
        # 按**段**比而不是按字符串前缀比：``家/客`` 不包含 ``家/客厅``。
        return Reach.SAME_PLACE
    if here[0] == there[0]:
        return Reach.SAME_BUILDING
    return Reach.OUT_OF_REACH


def reach_between_people(*, observer: str | None, other: str | None) -> Reach:
    """两个**人**之间够得着几分。跟 :func:`reach_between` 不是同一条规则。

    上面那条有一档"事情发生在一整片范围上，站在这片里的人都在场"——那是给**范围
    事件**用的：天黑、停电、饭菜的味道确实笼罩整栋，不给这一档她就一辈子读不到
    "天黑了"。

    **人不是范围。** "绫奈在家"不等于"绫奈就在客厅"。把覆盖档套到人身上，一个只
    粗略定位到 ``家`` 的人会被判成跟站在 ``家/客厅`` 的她同处一室，于是
    ``look_around`` 把人家正在做什么原样吐出来——位置数据一粗就泄露，而且是静默的。

    所以这里是 fail-closed 的：**同一地点只认路径完全相同**，粗一格就退到"同一栋"
    （知道她在哪个大致位置，不知道她在干嘛），根不同就够不着。定位不到任何一方一律
    够不着——跟"定位不到她 = 不在场"同一条纪律。
    """
    if not observer or not other:
        return Reach.OUT_OF_REACH
    here = _normalize(observer).split(_SEP)
    there = _normalize(other).split(_SEP)
    if not here[0] or not there[0]:
        return Reach.OUT_OF_REACH
    if here == there:
        return Reach.SAME_PLACE
    if here[0] == there[0]:
        return Reach.SAME_BUILDING
    return Reach.OUT_OF_REACH
