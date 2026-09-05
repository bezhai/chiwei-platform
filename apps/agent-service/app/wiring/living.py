"""Wiring: living 引擎的 Data 注册、五条时间源与那条出站边。

  interval 60s  -> CalendarTick    -> calendar_tick      （日历，不花模型钱）
  interval 300s -> WorldRoundTick  -> world_round_tick   （world 稀疏轮次，门在节点里）
  interval 60s  -> LifeMomentTick  -> life_moment_tick   （三个 life 的一缝，门在节点里）
  interval 60s  -> PhoneNudgeTick  -> phone_nudge_tick   （有人叫她就提前一缝）
  interval 300s -> LandingTick     -> landing_tick       （她那次开口落地成哪一行）

  ChatResponseSegment -> Sink.mq("chat_response")        （她开口，出 graph）

五条钟都直接挂在单字段 ``ts`` 的 tick 上，没有中间翻译节点：泳道由节点自己从进程
环境读（``app.living.clock.living_lane``），tick 本身不需要携带任何内容。挂时间源的
Data 多一个必填字段就会在源循环 ``_build_payload`` 处 ValidationError **直接杀
Pod**，所以这五个 Data 的形状由 ``tests/living/test_clock.py``、
``tests/living/test_moment.py`` 和 ``tests/living/test_no_inbound.py`` 钉住。

后三条钟拍得都比它们真正的间隔密（world 五分钟拍、一小时跑一轮；life 一分钟拍、
十分钟跑一缝），因为 ``Source.interval`` 的秒数在 import 时就固定了——想让间隔成为
可调的业务参数（Dynamic Config），只能让钟拍得比最密的间隔更密、然后在节点里判
"够不够久"。

**这里没有、也不会有任何入站边。** 五条钟全是 interval，一条 ``Source.mq`` /
``Source.http`` 都没有：chat 是嘴，没有耳朵，而这件事靠"根本没有接消息的地方"来
保证，不靠哪个分支里的 if。她收消息走的是每一缝直接查 ``common_message``
（``app.living.phone``），不碰队列。

守这条的是 ``tests/living/test_no_inbound.py``，它判的是**这条来源通向谁**：时间源
以外的每一种来源都算外部，消费者落在 ``app.living`` 里就红。所以"把一条已经放行的
运维 HTTP 接到 living 的节点上"同样拦得住——那和新挂一个源一样是长耳朵。运维那几条
要连消费者一起逐字命中白名单才放行。

出站方向的回执也走这条口径：``LandingTick`` 那条钟是**自己去查**公共层对账，不是
让渠道回调进来（那就是第一只耳朵）。

除五条钟之外还有一条 durable 边（``FilePickedUp -> read_a_round``，她拿起一个文件
读一程）。它**同样不是入站口**：``.durable()`` 只是 ``WireBuilder`` 上的标志位，不
产生任何 ``Source``，边上跑的只有她自己刚在某一缝里 emit 的那个信号。

``app.living.records`` 的 import 不能删：Data 类要被 ``app.wiring`` 的 side-effect
import 链拉到才会进 ``DATA_REGISTRY``，否则 ``Runtime.migrate_schema()`` 静默不建表、
一路跑到真读写才炸。``WorldRound`` 由 ``clock`` -> ``world`` 的 import 链带进来，
``LooseEnd`` 由 ``moment`` -> ``loose_ends`` 带进来，``PhoneRead`` 由 ``moment`` ->
``phone`` 带进来，``FileRead`` / ``FilePickedUp`` 由下面那行 ``living.reading`` 直接
带进来。``app.living.pictures``（她做过的图）**没有任何 import 链会顺路带到它**——它
不挂钟、不挂边，只有工具在用，所以跟 ``records`` 一样在这里显式 import 一次。注册
Data 和挂钟是两件事，别绑一起。
"""
from __future__ import annotations

from app.domain.chat_dataflow import ChatResponseSegment
from app.living import pictures, records  # noqa: F401
from app.living.clock import (
    CALENDAR_TICK_SECONDS,
    WORLD_ROUND_TICK_SECONDS,
    CalendarTick,
    WorldRoundTick,
    calendar_tick,
    world_round_tick,
)
from app.living.landing import (
    LANDING_TICK_SECONDS,
    LandingTick,
    landing_tick,
)
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
from app.runtime import Sink, Source, wire

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
# 对账：把她说出去的那些跟公共层落下的那一行接回来。单独一条钟，不塞进上面任何
# 一条 —— 那几条钟各有各的节奏理由（日历的到期误差、world 的轮次分辨率、一缝的
# 提前延迟），把一件跟它们无关的事挂上去就是职责混淆，而且从此改不动其中任何
# 一个的频率。
wire(LandingTick).from_(Source.interval(LANDING_TICK_SECONDS)).to(landing_tick)

# 读一程：她在某一缝拿起一个文件 → emit 一个 durable ``FilePickedUp`` → 这条边
# 把它接给 ``read_a_round`` 去读（取字节、解码、几轮模型调用，塞进一缝里会把她
# 卡在网络上）。durable 让它跨进程可达且不丢，并按 ``(lane, round_id)`` 去重。
#
# **这条边不是入站口。** ``.durable()`` 只是 ``WireBuilder`` 上的一个标志位，
# 不往 ``WireSpec.sources`` 里放任何东西 —— 上面那条"这里没有、也不会有任何入
# 站边"照旧成立（``tests/living/test_no_inbound.py``）。这条边上跑的只有她自己
# 刚拿起的那个文件，投递方和消费方都在这个进程里。
wire(FilePickedUp).durable().to(read_a_round)

# 她开口：``app.living.mouth`` emit 的每一段出 graph，投 ``chat_response_{channel}``
# 给拥有该 channel 的渠道服务（飞书 → lark-service，QQ → channel-server）。
#
# 这条边**是嘴的唯一出口**。它原先住在旧 chat 主 pipeline 的 wiring 里（那条链上
# 还挂着一条从消息队列进来的入站边），旧实现删掉时跟着搬到这里——出口归说话的人管，
# 不再寄居在别人的 pipeline 上。搬没搬到位由
# ``tests/wiring/test_outbound_wiring.py`` 按模块钉住。
#
# ``ChatResponseSegment`` 是 transient 且出 graph，不需要 ``.durable()``。
wire(ChatResponseSegment).to(Sink.mq("chat_response"))
