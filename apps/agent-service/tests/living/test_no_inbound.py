"""收到什么都不会直接让她跑一轮 —— 这是结构，不是纪律。

「被 @ 不能触发 chat」「收到一条消息不能当场让她开口」这两件事，靠的**不是**哪个分支里
写了 if：靠的是**能把东西送进来的地方只有一个，而它只存储**。她只在自己的钟上醒来
（常规的那一拍，或者有新东西时提前的那一拍，见 :mod:`app.living.nudge`），醒来之后自己去
读。纪律会被下一个人绕过去，结构不会。

第二期之前 life 一个收件箱都没有。现在三姐妹各有一个收件箱（通信机制，
:mod:`app.living.received`），这是她唯一的入口：world 告诉她察觉到了什么、姐妹直接对她说
的话都从这里来。所以这里验的是（看的是 agent-service 的插件宿主上登记的东西）：

  * 实验泳道上没有任何一条路由能到达 ``app.living`` 里的处理函数，路由之外能把东西送进进程的
    登记（收件箱、durable 边、将来别的种类）都逐种过一遍；
  * life 开设的收件箱只有三姐妹那一组，处理函数是 :func:`app.living.received.receive`，
    它只存一行、不碰模型：跑一遍看得到一行、看不到任何一次模型调用；它所在的模块连同
    它 import 的一切，一行能跑模型的代码都没有；
  * ``app.living`` 的源码里一次都没有出现旧 chat 入站那几个名字；
  * living 自己登记的只有那五条钟，而且钟上那条 Data 除了 ``ts`` 什么都装不下——
    装不下内容的钟，天然没法当入站口用。

最后一条是最要紧的一条：只要哪天有人给 tick 加一个 ``content`` 字段，"钟"就变成了
"信箱"，而这一步在 code review 里看起来毫无杀伤力。

第一条判的是**路由通向谁**，不是**路由长什么样**。判长相的版本（"Data 叫什么"、"路径是不是
/admin/ 开头"）挡得住"给 living 的 Data 挂一条路由"，挡不住反过来那一半：把一条已经在白名单里的
运维路由，处理函数换成 ``app.living`` 里的节点。后者的 Data、方法、路径一个字都不变，而 HTTP
请求会直接调进 living 的节点——同样是一只耳朵。所以判据落在处理函数身上。
"""
from __future__ import annotations

import ast
import functools
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import pytest

import app.living as living_pkg
from app.living.day_page import DayPageTick
from app.living.landing import LandingTick
from app.living.moment import LifeMomentTick
from app.living.nudge import PhoneNudgeTick
from app.living.persona_review import PersonaReviewTick
from tests.hosting import in_a_fresh_process

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


# 起 agent-service 的宿主之后，它登记的每一样东西。处理函数、Data 都记成完整标识
# ``__module__.__qualname__``：``@node`` 用 ``functools.wraps`` 包装原函数，这两个属性原样保留，
# 拿到的是业务函数自己的坐标，不是 wrapper 的。
_REGISTERED = """
import json
def _id(f):
    return getattr(f, '__module__', '?') + '.' + getattr(f, '__qualname__', repr(f))
rs = host.registered()
print(json.dumps({
    'kinds': sorted({r.kind for r in rs}),
    'routes': sorted(
        [r.detail['method'], r.detail['path'], _id(r.detail['request']), _id(r.detail['handler']),
         r.detail['inner_secret'], r.detail['lane_match'], r.detail['answers_with_lane']]
        for r in rs if r.kind == 'route'),
    'inboxes': sorted([r.name, _id(r.detail['on_message'])] for r in rs if r.kind == 'inbox'),
    'at_start': sorted(_id(r.detail['open']) for r in rs if r.kind == 'inboxes_at_start'),
    'durable': sorted([_id(r.detail['data_type']), _id(r.detail['consumer'])]
                      for r in rs if r.kind == 'durable'),
    'clocks': sorted([r.plugin, r.name] for r in rs if r.kind == 'clock'),
}))
"""

# 宿主能登记的每一种东西，按"能不能把外面的东西送进这个进程"分。能的那几种下面逐种查；钟不能：
# 每一拍只造一个只装得下 ``ts`` 的 Data（最后两条）。宿主多出一种登记，这里先红，要重新判断它算
# 哪一边。
_INBOUND_KINDS = frozenset({"route", "inbox", "inboxes_at_start", "durable"})
_NOT_INBOUND_KINDS = frozenset({"clock", "task", "outbound", "service", "on_stop"})

_LIVING_ROOT = "app.living"


class Route(NamedTuple):
    """宿主上的一条路由：方法、路径、收什么 Data、交给谁处理、三项检查。"""

    method: str
    path: str
    request: str
    handler: str
    inner_secret: bool = False
    lane_match: bool = False
    answers_with_lane: bool = False


def _operator(request: str, method: str, path: str, handler: str) -> Route:
    """通信机制人工入口的一条：三项检查都开着。"""
    return Route(method, path, request, handler, True, True, True)


# 进程里**唯一**允许存在的路由：运维口（``/admin/*``，搜索 + DLQ 巡检 + 通信机制的人工入口）。
#
# 每条按 **方法 + 路径 + 完整类标识 + 处理函数 + 三项检查** 列全，不是按类名、也不是按路径前缀。
# 理由是这份名单要回答的问题不是"它叫什么"而是"它是不是运维口"，而"是运维口"这件事由"这个方法
# 这个路径上的这条 Data 交给这个 admin 节点处理"整体成立——换掉其中任何一项（尤其是把处理函数换成
# ``app.living`` 里的节点），它就不再是当初被放行的那条路由了，必须重新过一遍判断。要不要凭据也
# 算身份的一部分：搜索和 DLQ 那几条今天就是裸的，把它们悄悄关起来会在下一次运维的时候才被发现。
_OPS_ONLY_ROUTES = frozenset({
    Route("POST", "/admin/search", "app.domain.admin.AdminSearchRequest",
          "app.nodes.admin.admin_search_node"),
    Route("POST", "/admin/dlq/inspect", "app.domain.dlq_admin_events.DlqInspectRequest",
          "app.nodes.dlq_admin.dlq_inspect_node"),
    Route("POST", "/admin/dlq/clear-idempotent",
          "app.domain.dlq_admin_events.DlqClearIdempotentRequest",
          "app.nodes.dlq_admin.dlq_clear_idempotent_node"),
    Route("POST", "/admin/dlq/dry-run", "app.domain.dlq_admin_events.DlqDryRunRequest",
          "app.nodes.dlq_admin.dlq_dry_run_node"),
    Route("POST", "/admin/dlq/requeue", "app.domain.dlq_admin_events.DlqRequeueRequest",
          "app.nodes.dlq_admin.dlq_requeue_node"),
    # 通信机制的人工参与者入口（``app.messaging.operator``）。
    #
    # **它们的处理函数不在 app.living 里。** 这六条把消息投进具名收件箱、读通信记录、看或
    # 重放本泳道的死信。第二期起三姐妹有了收件箱，所以 ``send`` / ``send-at`` / 死信重放
    # 送出的消息能到她的收件箱——跟 world 发来的一样，只经过
    # :func:`app.living.received.receive` 存一行，等她自己醒来再读，一次模型都不当场跑
    # （下面 ``test_her_inbox_only_stores`` 那几条守着）。这六条要内网凭据、经 dashboard
    # 落审计，用来以 world 或任何身份给她发一条验证用的消息。``ask`` 到不了她：她的收件箱
    # 不接受提问。
    _operator("app.messaging.operator.OperatorSendRequest", "POST",
              "/admin/messaging/send", "app.messaging.operator.operator_send_node"),
    _operator("app.messaging.operator.OperatorAskRequest", "POST",
              "/admin/messaging/ask", "app.messaging.operator.operator_ask_node"),
    _operator("app.messaging.operator.OperatorSendAtRequest", "POST",
              "/admin/messaging/send-at", "app.messaging.operator.operator_send_at_node"),
    _operator("app.messaging.operator.OperatorRecordRequest", "GET",
              "/admin/messaging/record", "app.messaging.operator.operator_record_node"),
    _operator("app.messaging.operator.OperatorDeadLettersRequest", "GET",
              "/admin/messaging/dead-letters", "app.messaging.operator.operator_dead_letters_node"),
    _operator("app.messaging.operator.OperatorReplayRequest", "POST",
              "/admin/messaging/dead-letters/replay",
              "app.messaging.operator.operator_replay_node"),
})

# 唯一的 durable 边：她自己在一轮里拿起一个文件，读一程（``tests/living/test_reading.py``）。投递方
# 和消费方都是这个进程；多出一条，就是又一条能把东西送进 living 的路，要重新判断。
_THE_READING_EDGE = ["app.living.reading.FilePickedUp", "app.living.reading.read_a_round"]


@functools.cache
def _registered(lane: str) -> dict:
    """在一个干净进程里按线上那样起 agent-service 的宿主，交回它登记的东西（:data:`_REGISTERED`）。"""
    out = in_a_fresh_process("agent-service", _REGISTERED, lane=lane)
    return json.loads(out.strip().splitlines()[-1])


def _lives_in_living(dotted: str) -> bool:
    return dotted == _LIVING_ROOT or dotted.startswith(_LIVING_ROOT + ".")


def _reaches_living(route: Route) -> list[str]:
    """这条路由到达了 living 的什么。空列表 = 没到达。"""
    where = f"HTTP {route.method} {route.path}"
    if _lives_in_living(route.handler):
        return [f"{where} 送进 {route.request}，直接调用 living 的 {route.handler}"]
    if _lives_in_living(route.request):
        return [
            f"{where} 送进的 {route.request} 是 living 自己的 Data，今天的处理函数是 "
            f"{route.handler} —— 只有 living 会读这条 Data，这条路由迟早通到 living 里去"
        ]
    return []


def test_nothing_from_outside_reaches_the_living_engine():
    """实验泳道上没有任何路由能到达 ``app.living`` 里的处理函数，别的入口逐种过一遍。

    路由判的是**通向谁**：把处理函数摸出来（``__module__.__qualname__``），凡是落在
    ``app.living`` 里的就红。这样两个方向都堵上——给 living 的 Data 挂一条路由会红，把一条已经在
    白名单里的运维路由的处理函数换成 living 的节点也会红。

    第二条断言是围栏：路由必须逐字出现在 :data:`_OPS_ONLY_ROUTES` 里，连方法、路径、处理函数和
    检查一起对，所以多出来的任何一条路由都会红。收件箱由下面两条查；durable 边只许是读一程那一条；
    宿主多出一种登记，先红在第一条断言上。
    """
    registered = _registered(LANE)

    unknown = set(registered["kinds"]) - _INBOUND_KINDS - _NOT_INBOUND_KINDS
    assert unknown == set(), (
        f"宿主上多了一种登记：{sorted(unknown)}。先判断它能不能把外面的东西送进进程，"
        "再把它归进 _INBOUND_KINDS 或 _NOT_INBOUND_KINDS（能的话在这里加上它的检查）。"
    )

    routes = [Route(*r) for r in registered["routes"]]
    ears = [line for r in routes for line in _reaches_living(r)]
    assert ears == [], (
        "外面的东西能到达 living 的处理函数了 —— 新引擎长出了耳朵：\n  "
        + "\n  ".join(ears)
        + "\nliving 只能自己按钟醒；手机上的消息她每一轮直接查 common_message，"
        "别人发给她的消息只经她的收件箱存下（app.living.received），"
        "不接任何人推进来、直接调进 living 的东西。"
    )

    unlisted = [r for r in routes if r not in _OPS_ONLY_ROUTES]
    assert unlisted == [], (
        "多了一条白名单外的路由：\n  "
        + "\n  ".join(f"{r.method} {r.path} → {r.request} → {r.handler}" for r in unlisted)
        + f"\n确实是运维口的话，把它的完整身份（方法、路径、类标识、处理函数、检查）"
        f"加进 _OPS_ONLY_ROUTES，并说明为什么它的处理函数不在 {_LIVING_ROOT} 里。"
    )

    assert registered["durable"] == [_THE_READING_EDGE], (
        f"durable 边不只是读一程那一条了：{registered['durable']}"
    )

    for name in ("ChatTrigger", "ChatRequest"):
        assert all(not r.request.endswith("." + name) for r in routes), (
            f"{name} 还挂着路由。拿到：{routes}"
        )


# 她的收件箱：开设它的那一个函数、处理每一条消息的那一个函数。
_OPEN_HER_INBOXES = "app.living.received.open_inboxes"
_HER_INBOX_HANDLER = "app.living.received"

# 能跑模型的代码住在这几处。收件处理所在的模块连同它 import 的一切，一个都不许碰到。
_MODEL_RUNNING = ("app.agent", "app.capabilities", "app.living.moment")


def test_the_only_inboxes_living_opens_are_the_residents():
    """life 开设的收件箱只有三姐妹那一组，由 :func:`app.living.received.open_inboxes` 开。

    名字在人设表里，setup 时只声明"开始接收时再开"；所以宿主起完 setup 之后，直接开设的
    收件箱一个都不许落在 ``app.living`` 里，"启动时再开"的声明只许是那一个。多出任何一个，
    都是又一条能把东西送进 life 的路，要重新判断。
    """
    registered = _registered(LANE)

    assert [n for n, handler in registered["inboxes"] if _lives_in_living(handler)] == [], (
        registered["inboxes"]
    )
    assert registered["at_start"] == [_OPEN_HER_INBOXES]


def test_the_inbox_handler_cannot_reach_a_model():
    """收件处理所在的模块，连同它 import 的一切，一行能跑模型的代码都没有。

    "只存储"在这里是结构：处理函数手边根本没有能叫醒她、能调模型的东西。哪天有人在收件
    处理里 import 一轮 moment 或者一个 agent，这条就红。
    """
    # 不走 ``in_a_fresh_process``：它先起整个 agent-service，那时模型代码早就在了。
    env = dict(os.environ)
    env["LANE"] = LANE
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys; import {_HER_INBOX_HANDLER};"
            "print(repr(sorted(m for m in sys.modules if m.startswith('app.'))))",
        ],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    loaded = ast.literal_eval(proc.stdout.strip().splitlines()[-1])
    assert _HER_INBOX_HANDLER in loaded, "用例前提没成立：处理函数的模块根本没被 import"
    reached = [
        m
        for m in loaded
        if any(m == root or m.startswith(root + ".") for root in _MODEL_RUNNING)
    ]
    assert reached == [], (
        f"收件处理（{_HER_INBOX_HANDLER}）能碰到跑模型的代码了：{reached}。"
        "收件只存储，她自己醒来时再读。"
    )


@pytest.mark.integration
async def test_her_inbox_only_stores(living_db, monkeypatch):
    """跑一遍她的收件处理：落一行，一次模型都不调，也不推进她的任何一轮。"""
    from types import SimpleNamespace

    from sqlalchemy import text

    from app.capabilities.agent import AgentRunner
    from app.data.session import get_session
    from app.living import moment as moment_mod
    from app.living import participants as participants_mod
    from app.living.received import open_inboxes
    from app.messaging.message import Kind, new_message
    from app.messaging.receiving import INBOX_REGISTRY

    monkeypatch.setenv("LANE", LANE)
    names = {"akao": "赤尾", "ayana": "绫奈", "chinagi": "千凪"}

    async def find_persona(persona_id: str):
        return SimpleNamespace(persona_id=persona_id, display_name=names[persona_id])

    monkeypatch.setattr(participants_mod, "find_persona", find_persona)
    monkeypatch.setattr(participants_mod, "_known", None)

    def no_model(*args, **kwargs):
        raise AssertionError("收件处理调到模型了")

    monkeypatch.setattr(AgentRunner, "run", no_model)
    monkeypatch.setattr(moment_mod, "build_moment_runner", no_model)
    monkeypatch.setattr(moment_mod, "run_moment", no_model)

    await open_inboxes()
    for name in names.values():
        await INBOX_REGISTRY[name].on_message(
            new_message(sender="world", recipient=name, body="窗外下起了雨。", kind=Kind.MESSAGE)
        )

    async with get_session() as s:
        stored = (
            await s.execute(
                text("SELECT persona_id FROM data_received_message ORDER BY persona_id")
            )
        ).scalars().all()
    assert stored == sorted(names)


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
    [
        LifeMomentTick,
        PhoneNudgeTick,
        LandingTick,
        DayPageTick,
        PersonaReviewTick,
    ],
)
def test_a_clock_cannot_be_turned_into_a_mailbox(cls):
    """钟上那条 Data 只有 ``ts``：装不下内容的钟没法当入站口。

    顺带也是那条杀 Pod 的约定（钟的循环每一拍固定按 ``data_type(ts=<iso>)`` 造 payload，
    :func:`app.plugins.living._ticker`）。
    """
    assert set(cls.model_fields) == {"ts"}, (
        f"{cls.__name__} 多了字段 {sorted(set(cls.model_fields) - {'ts'})} —— "
        f"钟一旦能携带内容，它就是个信箱了（而且钟的循环每一拍会 ValidationError 杀 Pod）"
    )


def test_the_only_clocks_are_livings_five():
    """宿主上的钟只有 living 那五条，名字就是上面那五个只装得下 ``ts`` 的 tick。"""
    assert _registered(LANE)["clocks"] == [
        ["living", "DayPageTick"],
        ["living", "LandingTick"],
        ["living", "LifeMomentTick"],
        ["living", "PersonaReviewTick"],
        ["living", "PhoneNudgeTick"],
    ]
