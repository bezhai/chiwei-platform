"""时间锚 —— 一缝 / 一轮的身份，落在格子上而不是钟表的某一瞬。

**为什么需要它。** 一缝和一轮的所有派生 id 都带着"现在"：``moment_id``、
``happening_id``、whereabouts 的自然键、world 的 ``item_id``（里面是
``what|place|due_at``，而 ``due_at = now + in_minutes``）。这些 id 是幂等的全部依据。

副作用先落库、收尾记录后落库，中间是有窗口的：崩在这里，下一拍会重跑同一件事，
而那时 ``now`` 已经不是原来那个了。**只要锚会动，派生 id 就全跟着动，幂等直接被
击穿**——她把同一句话说两遍、world 把同一件事排两次，而且两条记录长得不一样
（差几分钟），事后根本看不出来是重复。

把"现在"落到间隔网格上就解掉这一整类问题：一格之内的所有拍算出同一个锚，重试拿到
的是**同一缝**而不是新的一缝，所有派生 id 原样对上，重放退化成无害的 no-op。

**这不能替代真事务。** 它保证的是"重试同样的动作不会写出重复行"，不保证"重试产出
不同动作时不会两边都留下"——模型是不确定的，重放一次未必做一样的事。真正的全有全无
需要把整缝包进一个数据库事务，而那意味着一条业务连接被一次几十秒的模型调用占着
（``app.living.serial`` 的 docstring 里写了这条为什么不能做）。所以这里选的是"锚稳
住 + 派生 id 幂等"，残余缺口写在这儿，别当成事务用。

格子从**当地午夜**起算，不是从纪元起算：同一个钟点每天落在同一格上，跨天不漂。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.infra.cst_time import CST


def anchor_on_grid(moment: datetime, *, minutes: int) -> datetime:
    """把 ``moment`` 落到 ``minutes`` 分钟的网格上（向下取整，秒一律丢掉）。

    一格之内的每一拍都得到同一个锚——这就是"重试拿到同一缝"的全部机制。

    naive 的时刻直接拒：它落进派生 id 会被按服务器时区解释、静默偏几小时，整条幂等
    链跟着错位，而且一句报错都没有。非正的格子也直接拒：调用方那边是 Dynamic Config
    读出来的业务参数，配脏了要在这里炸，不能悄悄退化成"每一拍都是新的一缝"。
    """
    if moment.tzinfo is None:
        raise ValueError(
            f"时间锚必须带时区：naive 的时刻落进派生 id 会被按服务器时区解释，"
            f"静默偏几小时且不报错。收到 {moment!r}"
        )
    if minutes <= 0:
        raise ValueError(
            f"网格必须是正整数分钟，收到 {minutes!r} —— 非正的格子等于没有锚，"
            f"每一拍都会变成新的一缝，幂等全废。"
        )
    local = moment.astimezone(CST)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = int((local - midnight).total_seconds() // 60)
    return midnight + timedelta(minutes=(elapsed // minutes) * minutes)
