"""chat 是嘴，没有耳朵 —— 这是结构，不是纪律。

「被 @ 不能触发 chat」这件事，靠的**不是**哪个分支里写了 if：靠的是新引擎这一侧
**根本没有接消息的地方**。纪律会被下一个人绕过去，结构不会。

所以这里验的全是"存在性"而不是"行为"：

  * 实验泳道上没有任何 wire 从消息队列 / HTTP 把外面的东西喂进来；
  * ``app.living`` 的源码里一次都没有出现旧 chat 入站那几个名字；
  * living 自己挂的钟全是 interval，而且钟上那条 Data 除了 ``ts`` 什么都装不下——
    装不下内容的钟，天然没法当入站口用。

第三条是最要紧的一条：只要哪天有人给 tick 加一个 ``content`` 字段，"钟"就变成了
"信箱"，而这一步在 code review 里看起来毫无杀伤力。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import app.living as living_pkg
from app.living.clock import CalendarTick, WorldRoundTick
from app.living.moment import LifeMomentTick
from app.living.nudge import PhoneNudgeTick

LANE = "coe-living"

# 旧 chat 入站那条链上的名字。它们出现在 ``app.living`` 里就是一个入站口子的开始。
_INBOUND_NAMES = (
    "chat_request",
    "ChatTrigger",
    "ChatRequest",
    "route_chat_node",
    "chat_node",
    "life_wake_node",
    "EventArrived",
    "deliver_event",
)


def _in_a_fresh_process(expr: str, *, lane: str) -> str:
    env = dict(os.environ)
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


_ALL_SOURCES = (
    "from app.runtime.wire import WIRING_REGISTRY;"
    "print(sorted((s.data_type.__name__, x.kind, repr(sorted(x.params.items())))"
    " for s in WIRING_REGISTRY for x in s.sources))"
)


def test_nothing_carries_a_message_into_the_experiment_lane():
    """实验泳道上一条入站边都没有：没有 mq 消费、没有 HTTP 收消息的口。

    admin 那几条 HTTP 是运维口（``/admin/*``），不是消息入口——它们不喂 living 的
    任何 Data，所以按 data_type 白名单放行；除此之外任何 mq / http 源都该红。
    """
    out = _in_a_fresh_process(_ALL_SOURCES, lane=LANE)

    assert "'mq'" not in out, (
        f"实验泳道上还挂着消息队列消费者 —— 外面的消息能进来，chat 就不只是嘴了。"
        f"拿到：{out}"
    )
    for name in ("ChatTrigger", "ChatRequest"):
        assert name not in out, f"{name} 还挂着源。拿到：{out}"


def test_no_living_module_ever_mentions_the_old_inbound_chain():
    """``app.living`` 的源码里一次都不出现旧 chat 入站的名字。

    这条是"结构"的字面检查：新引擎连引用都没有，就没有谁能"顺手接一下"。
    """
    root = Path(living_pkg.__file__).parent
    offenders: list[str] = []
    for path in sorted(root.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for name in _INBOUND_NAMES:
            if name in text:
                offenders.append(f"{path.name}: {name}")
    assert offenders == [], (
        f"living 里出现了旧 chat 入站链的名字：{offenders} —— "
        f"一旦引用上了，「@ 触发 chat」离回来只差一行。"
    )


@pytest.mark.parametrize(
    "cls", [CalendarTick, WorldRoundTick, LifeMomentTick, PhoneNudgeTick]
)
def test_a_clock_cannot_be_turned_into_a_mailbox(cls):
    """钟上那条 Data 只有 ``ts``：装不下内容的钟没法当入站口。

    顺带也是那条杀 Pod 的约定（源循环固定按 ``data_type(ts=<iso>)`` 造 payload）。
    """
    assert set(cls.model_fields) == {"ts"}, (
        f"{cls.__name__} 多了字段 {sorted(set(cls.model_fields) - {'ts'})} —— "
        f"钟一旦能携带内容，它就是个信箱了（而且源循环每一拍会 ValidationError 杀 Pod）"
    )


def test_every_living_clock_is_an_interval_and_nothing_else():
    """living 自己挂的四条钟全是 interval：没有 cron、没有 mq、没有 http。"""
    out = _in_a_fresh_process(
        "from app.runtime.wire import WIRING_REGISTRY;"
        "print(sorted((s.data_type.__name__, x.kind)"
        " for s in WIRING_REGISTRY for x in s.sources"
        " if s.data_type.__name__ in"
        " ('CalendarTick','WorldRoundTick','LifeMomentTick','PhoneNudgeTick')))",
        lane=LANE,
    )
    assert out.strip() == (
        "[('CalendarTick', 'interval'), ('LifeMomentTick', 'interval'), "
        "('PhoneNudgeTick', 'interval'), ('WorldRoundTick', 'interval')]"
    ), out
