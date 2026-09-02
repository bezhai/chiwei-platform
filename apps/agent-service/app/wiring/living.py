"""Wiring: living 引擎的 Data 注册与四条时间源。

  interval 60s  -> CalendarTick    -> calendar_tick      （日历，不花模型钱）
  interval 300s -> WorldRoundTick  -> world_round_tick   （world 稀疏轮次，门在节点里）
  interval 60s  -> LifeMomentTick  -> life_moment_tick   （三个 life 的一缝，门在节点里）
  interval 60s  -> PhoneNudgeTick  -> phone_nudge_tick   （有人叫她就提前一缝）

四条钟都直接挂在单字段 ``ts`` 的 tick 上，没有中间翻译节点：泳道由节点自己从进程
环境读（``app.living.clock.living_lane``），tick 本身不需要携带任何内容。挂时间源的
Data 多一个必填字段就会在源循环 ``_build_payload`` 处 ValidationError **直接杀
Pod**，所以这四个 Data 的形状由 ``tests/living/test_clock.py``、
``tests/living/test_moment.py`` 和 ``tests/living/test_no_inbound.py`` 钉住。

后三条钟拍得都比它们真正的间隔密（world 五分钟拍、一小时跑一轮；life 一分钟拍、
十分钟跑一缝），因为 ``Source.interval`` 的秒数在 import 时就固定了——想让间隔成为
可调的业务参数（Dynamic Config），只能让钟拍得比最密的间隔更密、然后在节点里判
"够不够久"。

**这里没有、也不会有任何入站边。** 四条钟全是 interval，一条 ``Source.mq`` /
``Source.http`` 都没有：新引擎的 chat 是嘴，没有耳朵，而这件事靠"根本没有接消息的
地方"来保证，不靠哪个分支里的 if（``tests/living/test_no_inbound.py``）。她收消息走
的是每一缝直接查 ``common_message``（``app.living.phone``），不碰队列。

除四条钟之外还有一条 durable 边（``FilePickedUp -> read_a_round``，她拿起一个文件
读一程）。它**同样不是入站口**：``.durable()`` 只是 ``WireBuilder`` 上的标志位，不
产生任何 ``Source``，边上跑的只有她自己刚在某一缝里 emit 的那个信号。

**四条钟只在实验泳道上注册**（``app.living.experiment``）。这是实验的边界，不是行为
开关（不违反"不用工程思维解决 agent 的不确定性"——它管的是"这批代码允许在哪个环境里
自己跑起来"，不是"她该不该醒"）。同一个判据的**反面**挂在旧引擎那几个 wiring 模块上：
实验泳道上旧引擎一条 wire 都不注册，两套引擎不会在同一个泳道上各干各的。

不加这道门的后果是确定的、而且没有任何提示：runtime 在 prod 上**默认启用**时间源
（``lane_policy.time_sources_enabled_by_default`` 对 prod 返回 True），而
``living.clock.living_lane()`` 拿不到泳道时回落到 ``"prod"``。所以这个分支一旦合进
main、prod 一发版，world 加三个 life 立刻在 prod 库上开跑：建表、写数据、烧模型钱
——正面违反 spec 的 Non-goal「不替换 prod。这是 coe 上的实验，prod 引擎和它的表一律
不动」。

拦得住的是"有没有人往里写"。表还是会被 ``Runtime.migrate_schema()`` 在所有 lane 上
建出来——那个拦不住（建表跟 wiring 无关），也无害：空表不产生行为、不花钱。

``app.living.records`` 的 import 不能删，而且**必须留在门外面**：Data 类要被
``app.wiring`` 的 side-effect import 链拉到才会进 ``DATA_REGISTRY``，否则
``Runtime.migrate_schema()`` 静默不建表、一路跑到真读写才炸。``WorldRound`` 由
``clock`` -> ``world`` 的 import 链带进来，``LooseEnd`` 由 ``moment`` ->
``loose_ends`` 带进来，``PhoneRead`` 由 ``moment`` -> ``phone`` 带进来，
``FileRead`` / ``FilePickedUp`` 由下面那行 ``living.reading`` 直接带进来。注册 Data
和挂钟是两件事，别绑一起。
"""
from __future__ import annotations

import logging

from app.living import records  # noqa: F401
from app.living.clock import (
    CALENDAR_TICK_SECONDS,
    WORLD_ROUND_TICK_SECONDS,
    CalendarTick,
    WorldRoundTick,
    calendar_tick,
    world_round_tick,
)
from app.living.experiment import on_the_living_experiment_lane
from app.living.moment import (
    LIFE_MOMENT_TICK_SECONDS,
    LifeMomentTick,
    life_moment_tick,
)
from app.living.nudge import (
    PHONE_NUDGE_TICK_SECONDS,
    PhoneNudgeTick,
    phone_nudge_tick,
)
from app.living.reading import FilePickedUp, read_a_round
from app.runtime import Source, wire
from app.runtime.lane_policy import current_deployment_lane

logger = logging.getLogger(__name__)

if on_the_living_experiment_lane():
    wire(CalendarTick).from_(Source.interval(CALENDAR_TICK_SECONDS)).to(calendar_tick)
    wire(WorldRoundTick).from_(Source.interval(WORLD_ROUND_TICK_SECONDS)).to(
        world_round_tick
    )
    wire(LifeMomentTick).from_(Source.interval(LIFE_MOMENT_TICK_SECONDS)).to(
        life_moment_tick
    )
    wire(PhoneNudgeTick).from_(Source.interval(PHONE_NUDGE_TICK_SECONDS)).to(
        phone_nudge_tick
    )
    # 读一程：她在某一缝拿起一个文件 → emit 一个 durable ``FilePickedUp`` → 这条边
    # 把它接给 ``read_a_round`` 去读（取字节、解码、几轮模型调用，塞进一缝里会把她
    # 卡在网络上）。durable 让它跨进程可达且不丢，并按 ``(lane, round_id)`` 去重。
    #
    # **这条边不是入站口。** ``.durable()`` 只是 ``WireBuilder`` 上的一个标志位，
    # 不往 ``WireSpec.sources`` 里放任何东西 —— 上面那条"这里没有、也不会有任何入
    # 站边"照旧成立（``tests/living/test_no_inbound.py``）。这条边上跑的只有她自己
    # 刚拿起的那个文件，投递方和消费方都在这个进程里。
    wire(FilePickedUp).durable().to(read_a_round)
else:
    logger.warning(
        "living: 泳道 %r 不是实验泳道，日历 / world 轮次 / life 一缝 / 提前一缝"
        " 四条钟一条都不注册 —— 这批代码是 coe 上的实验，不在 prod / ppe 上自己"
        " 跑起来（表照建，空表无害）",
        current_deployment_lane() or "prod",
    )
