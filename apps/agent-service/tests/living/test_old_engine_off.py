"""实验泳道上旧引擎必须闭嘴 —— 而且只在实验泳道上闭嘴。

不加这道门的后果不是"有点吵"，是三件同时发生：

  1. **双回复**：dev bot 发一条消息，旧 chat 和新引擎各回一份，人分不清哪句是谁说的；
  2. **写脏 chiwei-test**：旧 world / 旧 life / 睡前回顾 / persona 慢漂全在同一个库上
     写自己的状态表；``persona_review`` 更狠 —— 它给 ``bot_persona`` 盖新版本，而新引擎
     每一缝都读这张表，实验读到的人设会被旧引擎中途换掉；
  3. **白烧钱**：world 心跳 10 分钟一拍、眼睛每小时一班，全是模型调用。

coe 的 ConfigBundle 把 ``DATAFLOW_ENABLE_TIME_SOURCES`` 覆盖成 1，所以那些
"非 prod 默认不跑"的 cron 在实验泳道上是**真的会跑**的——这道门不是保险，是必需品。

门只加在 **wiring 声明**上，旧引擎的业务逻辑一个字没动：注册不注册是部署事实，
不是行为开关。
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

LANE = "coe-living"

# 旧引擎在 ``app/wiring`` 里注册的全部 wire，按 (data_type, 消费者) 列出来。
# 关掉的依据一律是"它会在实验泳道上自己跑起来、而且会写库或烧钱"。
_MUST_BE_SILENT = [
    # 旧 chat 入站：唯一一条把飞书消息喂进旧引擎的路，也是双回复的来源。
    ("ChatTrigger", "route_chat_node"),
    ("ChatRequest", "chat_node"),
    # 旧 chat 的附件回填，只可能由 chat_node 的上下文构建触发 —— 没有 chat_node
    # 就是一条死边，留着只是白占一条 durable 队列。
    ("CommonMessageContentSynced", "persist_tos_files_node"),
    # 旧 world 发动机：10 分钟一拍心跳 + 自排卡点，每拍一次模型。
    ("WorldHeartbeatTick", "heartbeat_to_world_tick"),
    ("WorldTick", "world_tick"),
    # 旧 life：信箱敲门唤醒 / 日程到点提醒 / 异步读小说。
    ("EventArrived", "life_wake_node"),
    ("ScheduleReminderTick", "life_schedule_reminder_node"),
    ("ReadingTriggered", "reading_node"),
    # 睡前回顾清晨对账班（cron 05:00-10:00），写旧 life 状态。
    ("LifeDayReviewTick", "review_to_sweep_tick"),
    ("LifeDayReviewSweep", "day_review_sweep_node"),
    # persona 慢漂（cron 11:00）：给 bot_persona 盖新版本 —— 新引擎每一缝都读它。
    ("PersonaReviewTick", "persona_review_to_sweep_tick"),
    ("PersonaReviewSweep", "persona_review_sweep_node"),
    # 眼睛：每小时上网抓底料。spec Non-goal 明写第一版不做上网查。
    ("DailyMaterialsTick", "fetch_to_materials_tick"),
    ("DailyMaterialsFetch", "daily_fetch_node"),
]

_PAIRS = (
    "from app.runtime.wire import WIRING_REGISTRY;"
    "print(sorted((s.data_type.__name__, c.__name__)"
    " for s in WIRING_REGISTRY for c in s.consumers))"
)

_SINK_PAIRS = (
    "from app.runtime.wire import WIRING_REGISTRY;"
    "print(sorted((s.data_type.__name__, k.kind, repr(sorted(k.params.items())))"
    " for s in WIRING_REGISTRY for k in s.sinks))"
)


def _in_a_fresh_process(expr: str, *, lane: str | None) -> str:
    env = dict(os.environ)
    if lane is None:
        env.pop("LANE", None)
    else:
        env["LANE"] = lane
    proc = subprocess.run(
        [sys.executable, "-c", f"import app.wiring;{expr}"],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.mark.parametrize(("data_type", "consumer"), _MUST_BE_SILENT)
def test_the_old_engine_is_not_wired_on_the_experiment_lane(data_type, consumer):
    out = _in_a_fresh_process(_PAIRS, lane=LANE)
    assert f"('{data_type}', '{consumer}')" not in out, (
        f"{data_type} -> {consumer} 在 {LANE} 上还注册着 —— dev bot 发一条消息，"
        f"旧引擎和新引擎会各干各的一份。拿到：{out}"
    )


@pytest.mark.parametrize(("data_type", "consumer"), _MUST_BE_SILENT)
@pytest.mark.parametrize("lane", ["prod", "coe-somethingelse", "ppe-x"])
def test_the_old_engine_still_runs_everywhere_else(lane, data_type, consumer):
    """门只关**这一个**实验泳道，不是整个 coe 等级。

    ``coe`` 是通用隔离等级（独立离线基建），不是实验身份。把整个 ``coe-*`` 当成
    living 实验有两个后果：任何无关的 coe 部署都会被拉起 living，而**涉及 schema /
    协议的旧引擎验证就再也没地方跑了**——那种验证正是不能拿会写 prod 数据的 ppe 代替
    的。所以判据必须是具体泳道名。
    """
    out = _in_a_fresh_process(_PAIRS, lane=lane)
    assert f"('{data_type}', '{consumer}')" in out, (
        f"{data_type} -> {consumer} 在 {lane} 上不见了 —— 实验的隔离门误伤了"
        f"别的泳道。拿到：{out}"
    )


def test_the_outbound_mouth_stays_open_on_the_experiment_lane():
    """出站那条边**不能**一起关掉：新引擎说话就是走它出去的。

    ``ChatResponseSegment -> Sink.mq(chat_response)`` 是嘴的唯一出口，跟入站
    没有任何关系。把它跟入站一起关掉，她就成了哑巴而不是"没有耳朵"。
    """
    out = _in_a_fresh_process(_SINK_PAIRS, lane=LANE)
    assert "ChatResponseSegment" in out and "chat_response" in out, out


def test_the_experiment_engine_is_the_one_that_runs_on_the_experiment_lane():
    """反过来也要成立：实验泳道上新引擎五条钟一条不少。"""
    out = _in_a_fresh_process(
        "from app.runtime.wire import WIRING_REGISTRY;"
        "print(sorted(s.data_type.__name__ for s in WIRING_REGISTRY"
        " for x in s.sources))",
        lane=LANE,
    )
    for name in (
        "CalendarTick",
        "WorldRoundTick",
        "LifeMomentTick",
        "PhoneNudgeTick",
        "LandingTick",
    ):
        assert f"'{name}'" in out, out


@pytest.mark.parametrize("lane", ["coe-somethingelse", "prod", "ppe-x"])
def test_the_new_engine_does_not_start_on_a_lane_that_is_not_the_experiment(lane):
    """反面同一条判据：不是这个实验泳道，新引擎一条钟都不挂。

    跟上面那条合起来才是完整的"两边共用同一个判断、不会分叉"：任何泳道上，
    要么新引擎在跑、要么旧引擎在跑，不会两个都在、也不会两个都不在。
    """
    out = _in_a_fresh_process(
        "from app.runtime.wire import WIRING_REGISTRY;"
        "print(sorted(s.data_type.__name__ for s in WIRING_REGISTRY"
        " for x in s.sources))",
        lane=lane,
    )
    for name in (
        "CalendarTick",
        "WorldRoundTick",
        "LifeMomentTick",
        "PhoneNudgeTick",
        "LandingTick",
    ):
        assert f"'{name}'" not in out, (
            f"{name} 在 {lane} 上挂上了 —— 一个跟 living 无关的泳道被拉起了实验引擎。"
            f"拿到：{out}"
        )
