"""chat 是嘴，没有耳朵 —— 这是结构，不是纪律。

「被 @ 不能触发 chat」这件事，靠的**不是**哪个分支里写了 if：靠的是新引擎这一侧
**根本没有接消息的地方**。纪律会被下一个人绕过去，结构不会。

所以这里验的全是"存在性"而不是"行为"：

  * 实验泳道上没有任何外部来源（消息队列 / HTTP / 将来别的 kind）能到达
    ``app.living`` 里的消费者；
  * ``app.living`` 的源码里一次都没有出现旧 chat 入站那几个名字；
  * living 自己挂的钟全是 interval，而且钟上那条 Data 除了 ``ts`` 什么都装不下——
    装不下内容的钟，天然没法当入站口用。

第三条是最要紧的一条：只要哪天有人给 tick 加一个 ``content`` 字段，"钟"就变成了
"信箱"，而这一步在 code review 里看起来毫无杀伤力。

第一条判的是**来源通向谁**，不是**来源长什么样**。判来源长相的版本（"data_type 叫
什么"、"路径是不是 /admin/ 开头"）挡得住"给 living 的 Data 挂一个 HTTP 源"，挡不住
反过来那一半：把一条已经在白名单里的运维 HTTP 源，消费者换成 ``app.living`` 里的
节点。后者采集到的 data_type、kind、路径一个字都不变，而 HTTP 请求会直接调进 living
的节点——同样是一只耳朵。所以判据落在消费者身上。
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import pytest

import app.living as living_pkg
from app.living.clock import CalendarTick, WorldRoundTick
from app.living.landing import LandingTick
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


# 采集每条源的四要素。**消费者是其中一要素**：只有它能回答"这条源通向谁"。
#
# ``__module__ + '.' + __qualname__`` 就是消费者的完整标识。``@node`` 用
# ``functools.wraps`` 包装原函数，这两个属性原样保留，拿到的是业务函数自己的坐标，
# 不是 wrapper 的。四个字段全部输出成字符串，是为了让 ``sorted`` 有全序（params 里
# 混着 str / bool / float，直接排会在同键不同类型时炸）。
_ALL_SOURCES = (
    "_id=(lambda c: getattr(c,'__module__','?')+'.'+getattr(c,'__qualname__',repr(c)));"
    "from app.runtime.wire import WIRING_REGISTRY;"
    "print(sorted(("
    "x.kind,"
    "s.data_type.__module__+'.'+s.data_type.__qualname__,"
    "repr(tuple(sorted(x.params.items()))),"
    "repr(tuple(sorted(_id(c) for c in s.consumers)))"
    ") for s in WIRING_REGISTRY for x in s.sources))"
)

# 钟不是入站边：``Engine._build_payload`` 对 cron / interval 固定造
# ``data_type(ts=<iso>)``，外面塞不进任何内容。除这两种之外的每一种 kind 都算外部
# 来源——包括今天还不存在的 kind，新增一种就会落进下面的判据里。
_CLOCK_KINDS = frozenset({"interval", "cron"})

_LIVING_ROOT = "app.living"


class RegisteredSource(NamedTuple):
    """这个进程里注册的一条源：从哪种来源来、装的是哪条 Data、通向哪些消费者。"""

    kind: str
    data_type: str
    params: dict
    consumers: tuple[str, ...]


def _ops_http(data_type: str, method: str, path: str, consumer: str) -> tuple:
    """一条运维 HTTP 口的完整身份。"""
    return (
        "http",
        data_type,
        (("method", method), ("path", path), ("response", True)),
        (consumer,),
    )


# 进程里**唯一**允许存在的外部来源：运维口（``/admin/*``，搜索 + DLQ 巡检）。
#
# 每条按 **完整类标识 + 方法 + 路径 + 消费者** 列全，不是按类名、也不是按路径前缀。
# 理由是这份名单要回答的问题不是"它叫什么"而是"它是不是运维口"，而"是运维口"这件
# 事由"这个方法这个路径上的这条 Data 交给这个 admin 节点处理"整体成立——换掉其中
# 任何一项（尤其是把消费者换成 ``app.living`` 里的节点），它就不再是当初被放行的
# 那条边了，必须重新过一遍判断。
#
# 名单里没有 mq，所以任何 ``Source.mq`` 都会红。
_OPS_ONLY_EXTERNAL_SOURCES = frozenset({
    _ops_http(
        "app.domain.admin.AdminSearchRequest",
        "POST", "/admin/search",
        "app.nodes.admin.admin_search_node",
    ),
    _ops_http(
        "app.domain.dlq_admin_events.DlqInspectRequest",
        "POST", "/admin/dlq/inspect",
        "app.nodes.dlq_admin.dlq_inspect_node",
    ),
    _ops_http(
        "app.domain.dlq_admin_events.DlqClearIdempotentRequest",
        "POST", "/admin/dlq/clear-idempotent",
        "app.nodes.dlq_admin.dlq_clear_idempotent_node",
    ),
    _ops_http(
        "app.domain.dlq_admin_events.DlqDryRunRequest",
        "POST", "/admin/dlq/dry-run",
        "app.nodes.dlq_admin.dlq_dry_run_node",
    ),
    _ops_http(
        "app.domain.dlq_admin_events.DlqRequeueRequest",
        "POST", "/admin/dlq/requeue",
        "app.nodes.dlq_admin.dlq_requeue_node",
    ),
})


def _sources(lane: str) -> list[RegisteredSource]:
    """这个进程里注册的每一条源，带着它的消费者。"""
    out = _in_a_fresh_process(_ALL_SOURCES, lane=lane)
    return [
        RegisteredSource(
            kind=kind,
            data_type=data_type,
            params=dict(ast.literal_eval(params)),
            consumers=ast.literal_eval(consumers),
        )
        for kind, data_type, params, consumers in ast.literal_eval(out)
    ]


def _lives_in_living(dotted: str) -> bool:
    return dotted == _LIVING_ROOT or dotted.startswith(_LIVING_ROOT + ".")


def _fingerprint(src: RegisteredSource) -> tuple:
    """和 :data:`_OPS_ONLY_EXTERNAL_SOURCES` 对齐的完整身份（含消费者）。"""
    return (src.kind, src.data_type, tuple(sorted(src.params.items())), src.consumers)


def _where(src: RegisteredSource) -> str:
    """这条源从哪儿进来，说人话。"""
    if src.kind == "http":
        return (
            f"HTTP {src.params.get('method', '?')} {src.params.get('path', '?')}"
        )
    if src.kind == "mq":
        return f"消息队列 {src.params.get('queue', '?')}"
    return f"{src.kind} {sorted(src.params.items())}"


def _reaches_living(src: RegisteredSource) -> list[str]:
    """这条外部源到达了 living 的哪些消费者。空列表 = 没到达。"""
    landed_on = [c for c in src.consumers if _lives_in_living(c)]
    if landed_on:
        return [
            f"{_where(src)} 送进 {src.data_type}，直接调用 living 的消费者 {c}"
            for c in landed_on
        ]
    if _lives_in_living(src.data_type):
        seen = ", ".join(src.consumers) or "（这条 wire 上一个消费者都没有）"
        return [
            f"{_where(src)} 送进的 {src.data_type} 是 living 自己的 Data，"
            f"今天挂的消费者是 {seen} —— 只有 living 会读这条 Data，"
            f"这条边迟早通到 living 里去"
        ]
    return []


def test_no_external_source_reaches_the_living_engine():
    """实验泳道上没有任何外部来源能到达 ``app.living`` 里的消费者。

    判的是**通向谁**：把源的消费者摸出来（``__module__.__qualname__``），凡是落在
    ``app.living`` 里的就红。这样两个方向都堵上——给 living 的 Data 挂一条
    ``Source.http`` 会红，把一条已经在白名单里的运维 HTTP 源的消费者换成 living 的
    节点也会红。后者在只看 data_type / kind / 路径的判据下采集结果一个字都不变，
    但 HTTP 请求会直接调进 living 的节点。

    第二条断言是围栏：外部源必须逐字出现在 :data:`_OPS_ONLY_EXTERNAL_SOURCES` 里，
    连方法、路径和消费者一起对。名单里只有 ``/admin/*`` 那几条运维口，没有 mq，
    所以多出来的任何一条外部源——不管什么 kind——都会红。
    """
    registered = _sources(LANE)
    external = [s for s in registered if s.kind not in _CLOCK_KINDS]

    ears = [line for s in external for line in _reaches_living(s)]
    assert ears == [], (
        "外面的东西能到达 living 的消费者了 —— 新引擎长出了耳朵：\n  "
        + "\n  ".join(ears)
        + "\nliving 只能自己按钟醒；她收消息走的是每一缝直接查 common_message，"
        "不接任何人推进来的东西。"
    )

    unlisted = [s for s in external if _fingerprint(s) not in _OPS_ONLY_EXTERNAL_SOURCES]
    assert unlisted == [], (
        "多了一条白名单外的外部来源：\n  "
        + "\n  ".join(
            f"{_where(s)} → {s.data_type} → 消费者 {list(s.consumers)}"
            for s in unlisted
        )
        + f"\n确实是运维口的话，把它的完整身份（kind、类标识、方法、路径、消费者）"
        f"加进 _OPS_ONLY_EXTERNAL_SOURCES，并说明为什么它的消费者不在 {_LIVING_ROOT} 里。"
    )

    for name in ("ChatTrigger", "ChatRequest"):
        assert all(not s.data_type.endswith("." + name) for s in registered), (
            f"{name} 还挂着源。拿到：{registered}"
        )


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
    "cls",
    [CalendarTick, WorldRoundTick, LifeMomentTick, PhoneNudgeTick, LandingTick],
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
    """living 自己挂的五条钟全是 interval：没有 cron、没有 mq、没有 http。"""
    out = _in_a_fresh_process(
        "from app.runtime.wire import WIRING_REGISTRY;"
        "print(sorted((s.data_type.__name__, x.kind)"
        " for s in WIRING_REGISTRY for x in s.sources"
        " if s.data_type.__name__ in"
        " ('CalendarTick','WorldRoundTick','LifeMomentTick','PhoneNudgeTick',"
        "'LandingTick')))",
        lane=LANE,
    )
    assert out.strip() == (
        "[('CalendarTick', 'interval'), ('LandingTick', 'interval'), "
        "('LifeMomentTick', 'interval'), ('PhoneNudgeTick', 'interval'), "
        "('WorldRoundTick', 'interval')]"
    ), out
