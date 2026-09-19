"""位置比对——规则，不是模型判断。

地点写成层级路径，段之间用 ``/``：``家/客厅``、``家/楼上/绫奈房间``、``学校``。
第一段是"哪一栋"，往后是这栋里的哪儿。

三档：

  * :attr:`Reach.SAME_PLACE`     同一地点 —— 听得见原话
  * :attr:`Reach.SAME_BUILDING`  同一栋的不同地方 —— 只知道有动静，没有内容
  * :attr:`Reach.OUT_OF_REACH`   够不着 —— 什么都没有

**事件的范围有三种写法**，全部落在同一条路径比对上，不需要额外的字段：

  * 一个具体位置 ``家/厨房`` —— 站在那儿的人在场
  * 一整片 ``学校`` —— 这片里的人都在场（下面那条覆盖档）
  * :data:`EVERYWHERE` —— 天黑、台风、今天是什么节气，在哪都在场

**:data:`EVERYWHERE` 和"没记下地点"必须是两个值。** 空地点仍然一律
:attr:`Reach.OUT_OF_REACH`：把它顺手当成全局，任何一处忘填 place 的写入都会变成全世界
都听见，而且一句报错都没有。全局只能是显式写下的那一个值。

在有这一档之前，不绑地点的事被写在 ``家`` 这一整片上（旧的 ``AMBIENT_PLACE``），而实测
19.1% 的记录发生在 ``家`` 以外的根（学校 723、小区 156、老街 48）—— 她在那些地方时一条
日历事件都收不到。根因不是常量填错，是事件模型里只有"点位置"没有"全局"这个选项，
非局部的事塞哪个地点都是错的。

**只有事件有全局这一档，人没有**（见 :func:`reach_between_people`）。

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

# 事件范围的第三档：不属于任何一栋，笼罩所有地方。
#
# 用一个不可能是地名的值，而不是"空地点"或者一个新字段：空地点已经有含义（没记下来，
# fail-closed），而加一列 scope 要让每一处写入都决定填什么，等于把一个只有两个取值的
# 判断摊到所有调用方身上。层级路径本来就表达得了"多大一片"，全局只是这条轴的顶端。
EVERYWHERE = "*"

# ---------------------------------------------------------------------------
# 这套地名交给模型的字
#
# 说这几句的是**她那两只落位置的手**（:func:`app.living.moment.switch_to` /
# :func:`app.living.moment.move_to`）和 **world 让事情发生的那一只**
# （:func:`app.living.world.expect`）。两边写的是同一套地名空间：同一个地方谁写都得
# 写成同一条路径，才算同一处。所以说形状的这段字只能有一份，而它的家就是本模块 ——
# 路径怎么规范化、怎么比对写在下面几个函数里，说它长什么样的措辞跟规则住在一起，改
# 分隔符的时候两样东西在同一屏里。各写各的必然漂开，漂开的表现就是同一个地方裂成两个。
#
# **举例就是词表。** 她不读文档，唯一见过的地名样本就是这段描述，写在这儿的地名会被
# 逐字抄走。两次线上事故都是这么来的：
#
#   * 举例写 ``家/楼上/我房间`` —— "我房间"是说话人相对的说法，而 place 是三个人共用
#     的同一套字符串。prod 实测 45 次 ``家/楼上/我房间`` 加 9 次 ``家/我房间`` 全部出
#     自绫奈和千凪两个人，2026-09-13 21:00 两人同一分钟落在这个地名上，各自在自己屋
#     里，判定却是同处一室，私下说的话被对方原话听见。
#   * 举例写 ``学校/二年三班教室`` —— 被一字不差抄走，而设定集里那间教室叫
#     ``学校/初二三班教室``。同一个地方从此分成两半。
#
# 上一轮修复换上的是一批新的写死字符串（家/浴室、学校/操场这些），事故没再发生，但
# 病根没除：那些名字是从世界的初始设定里抄来的，而世界随时可以改名、删掉、重写它们，
# 这段描述不会跟着变 —— 那只是把同一次事故往后推。
#
# 所以这里**一个样本都不给**，只说规则。占位符（``A/B``、``某地/某处``、
# ``<建筑>/<房间>``）同样不给：那仍然是一个可以照着填的模板，而且比直说更难懂。
# 也不写"去查一下设定集有哪些地名"——她不读文档是既定边界；她写错的时候
# :func:`app.living.moment._place_names_in_the_same_building` 会把树上确实有的那些如
# 实报给她。
# ---------------------------------------------------------------------------

PLACE_SHAPE = (
    "写成一条层级路径：最外面一层是这个世界里一处独立的地方，"
    "往里一层层是它内部的分隔，层与层之间用 / 隔开；"
    "就在那一处本身、底下没有更细的分隔时，只写一层也行。"
)

# 第一次事故的病根单列一句：形状写对了，"我房间"这种写法照样会把两个人钉在同一条
# 路径上，而那是 place 里最贵的一种错。
PLACE_IS_EVERYONES = (
    "这套地名三个人共用，同一个地方谁写都得写成同一条路径，"
    "所以别用只有站在你自己的位置上才指得明白的说法。"
)


class Reach(StrEnum):
    """一个观察者相对一件事发生地的三档可及性。"""

    SAME_PLACE = "same_place"
    SAME_BUILDING = "same_building"
    OUT_OF_REACH = "out_of_reach"


def _normalize(path: str) -> str:
    """去掉首尾空白 / 多余分隔符 / 段内空白，得到可比较的规范路径。"""
    segments = [seg.strip() for seg in path.strip().split(_SEP)]
    return _SEP.join(seg for seg in segments if seg)


def building_of(path: str | None) -> str:
    """这条路径属于哪一栋 —— 就是 :func:`reach_between` 判 ``SAME_BUILDING`` 时比的那一段。

    单独暴露出来，是因为**别处也要问这个问题**（:func:`app.living.moment.arriving_at`
    报"同一栋里有哪些地名"时要先知道是哪一栋），而那边自己拿 ``split`` 切一次就会切出
    另一套规范化：``/ /家/不存在`` 这边算"家"、那边算空，``./不存在`` 这边算 ``.``、
    那边交给文件路径解析器会被消成根目录。同一条路径在两处判出两个答案，而且一句报错
    都没有。

    定位不到（``None`` / 空 / 全是空白）返回空串，跟 :attr:`Reach.OUT_OF_REACH` 同源：
    没有"在某一栋里"这个前提。
    """
    if not path:
        return ""
    segments = _normalize(path).split(_SEP)
    return segments[0] if segments else ""


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
    if not here[0]:
        return Reach.OUT_OF_REACH
    if there == [EVERYWHERE]:
        # 全局：天黑、台风、今天什么节气。定位得到她就在场，跟她在哪无关。
        # **判在这儿而不是判在上面**：定位不到她这条 fail-closed 仍然先生效 ——
        # "在场"这件事的前提是知道她在某个地方。
        return Reach.SAME_PLACE
    if not there[0]:
        # 没记下地点。跟全局是两件事，一律够不着（判据写在模块头上）。
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

    **:data:`EVERYWHERE` 在这条规则里没有意义，两侧出现它都是够不着。** 那个值描述的是
    事件的范围，人没有这一档：真放行了，一个位置写成全局的人会被判成跟所有人同处一室，
    ``look_around`` 把每个人正在做什么直接吐出来。
    """
    if not observer or not other:
        return Reach.OUT_OF_REACH
    if EVERYWHERE in (observer.strip(), other.strip()):
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
