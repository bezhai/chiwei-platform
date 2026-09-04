"""一缝的作用域 —— 她此刻是谁、在哪个泳道、几点、哪一缝。

四样 ambient 事实，一缝里**每个**工具都要问，而且四样都不能各取各的：

  * ``lane``       泳道隔离是硬约束。runtime 不给任何 Data 自动加 lane，工具体自己
                   编一个空 lane 会开一条谁也读不到的影子轴——写进去的行查不出来，
                   而她那边一片安静，什么报错都没有。
  * ``now``        本缝的时间锚。**不许工具体自己 ``datetime.now()``**：同一缝里两个
                   工具各取各的"现在"，同一件事会落成两行。
  * ``persona_id`` 谁在做这件事。
  * ``moment_id``  哪一缝。所有派生 id 都从它来（happening_id、whereabouts 的自然键），
                   所以它稳、幂等就稳。

住在这里而不是住在 :mod:`app.living.moment` 里，是因为**用它的不止一处**：手上的事
（moment）、手机（phone）、嘴（mouth）三个模块的工具都要读同一份。放在 moment 里会
让 phone / mouth 反过来 import moment，而 moment 又要 import 它们的工具——一个必然的
循环。这四个 key 只允许在这里定义一次。

``lane`` / ``now`` 两个 key 的名字定义在 :mod:`app.living.world`（world 的轮次也用
同一份），这里不再声明同名常量。
"""

from __future__ import annotations

from datetime import datetime

from app.agent.runtime_context import get_context

# 这两个 ambient key 是整个 living 包共用的一份定义。
from app.living.world import FEATURE_LANE, FEATURE_NOW

# 谁在跑这一缝、这是哪一缝。
FEATURE_PERSONA = "living_persona"
FEATURE_MOMENT = "living_moment"

# 本缝她换过的事（每次 switch_to 追加一条 {"doing","because"}）。数它而不是数
# "有没有调过工具"：换事情和说话是两回事，而"换的理由"要能逐缝查出来。
FEATURE_SWITCHES = "living_switches"

# 本缝真的落库的 happening_id（去重后就是"她这一缝说 / 做了几件事"）。当面说的、
# 手机上发出去的都算——对她自己来说都是"我刚才说了这句话"。
FEATURE_RECORDED = "living_recorded"

# 本缝定下的那份"她看得见哪些会话"：``{"key": <谁+哪一刻>, "channels": [...]}``。
# 会话白名单按时间窗算（:mod:`app.living.whitelist`），一缝里多处各算一次的话，她看到
# 的会话集合会在这一缝中途变化 —— 信封上没有的会话，后半缝突然搜得到、发得出去。所以
# 跟"此刻几点"一样：**一缝之内是同一个值**。缝外面（nudge 那条钟）没有这一项，现算。
FEATURE_IN_SIGHT = "living_in_sight"

# 本缝她看过的手机（每次 look_at_phone 追加一条待落库的游标）。**攒在这儿不当场写**：
# 工具返回不等于她看见了——工具结果还要进模型的上下文，这一缝才算真的把内容送到她眼
# 前。当场推游标的话，崩在中间那几条消息就此永久消失、而且一句报错都没有。所以游标跟
# ``LifeMoment`` 在同一个事务里落库（:func:`app.living.phone.commit_glances`）：缝没
# 跑完就一条都不算已读，她下一缝原样再看到。宁可重看，不可漏看。
FEATURE_GLANCES = "living_glances"


def moment_scope() -> tuple[str, datetime, str, str]:
    """本缝的 ``(lane, 时间锚, persona_id, moment_id)``。

    没绑 context 直接 ``LookupError`` 失败快，暴露漏了 ``agent_context(...)`` 的
    wiring bug —— 静默用一个空 lane 会开一条谁也读不到的影子轴。
    """
    ctx = get_context()
    return (
        ctx.features[FEATURE_LANE],
        datetime.fromisoformat(ctx.features[FEATURE_NOW]),
        ctx.features[FEATURE_PERSONA],
        ctx.features[FEATURE_MOMENT],
    )


def note_recorded(happening_id: str) -> None:
    """记下本缝又落了一件别人感知得到的事（当面说的、手机上发的，一视同仁）。"""
    get_context().features.setdefault(FEATURE_RECORDED, []).append(happening_id)
