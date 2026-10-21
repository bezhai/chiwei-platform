"""收到什么都不会直接让她跑一轮 —— 这是结构，不是纪律。

「被 @ 不能触发 chat」「收到一条消息不能当场让她开口」这两件事，靠的**不是**哪个分支里
写了 if：靠的是**能把东西送进来的地方只有一个，而它只存储**。她只在自己的钟上醒来
（常规的那一拍，或者有新东西时提前的那一拍，见 :mod:`app.living.life.nudge`），醒来之后自己去
读。纪律会被下一个人绕过去，结构不会。

第二期之前 life 一个收件箱都没有。现在三姐妹各有一个收件箱（通信机制，
:mod:`app.living.household.received`），这是她唯一的入口：world 告诉她察觉到了什么、姐妹直接对她说
的话都从这里来。另有一个读书收件箱（:mod:`app.living.reading`）：她在自己的一轮里拿起一个
文件时发一条消息到这里，读那一程。所以这里验的是（看的是 agent-service 的插件宿主上登记的
东西）：

  * 实验泳道上到得了 ``app.living`` 里的处理函数的路由只有一条：把旧读书队列搬进读书收件箱的那条
    运维路由，它能做到的跟往读书收件箱里投一条一样，碰不到她的一轮。路由之外能把东西送进进程的
    登记（收件箱、将来别的种类）都逐种过一遍；
  * life 开设的收件箱只有三姐妹那一组和读书收件箱。三姐妹的处理函数是
    :func:`app.living.household.received.receive`，它只存一行、不碰模型：跑一遍看得到一行、看不到任何一次
    模型调用；它所在的模块连同它 import 的一切，一行能跑模型的代码都没有；
  * 读书收件箱和别的收件箱一样，运维入口（``/admin/messaging/send``）能以任何身份往里投。投进来
    能做到的只有读一程：处理函数 :func:`app.living.reading.files.read_a_round` 所在的模块连同它
    import 的一切，碰不到她的一轮（:mod:`app.living.life.moment`）；搬运那条路由的处理函数也一样；
  * 她的插件登记的钟只有对账那一条（speaking 插件）；她的轮次、日记和回看由宿主的钟给她的队列发信号。钟给出去
    的只有钟响的时刻：对账那条钟每一拍对一次这个进程泳道的账，她的队列收到的信号里也只有时刻
    （宿主造的 ``app.host.Signal``）——装不下内容的钟，天然没法当入站口用。

最后一条是最要紧的一条：只要哪天有人让钟带上内容（给钟的信号加一个 ``content`` 字段），"钟"
就变成了"信箱"，而这一步在 code review 里看起来毫无杀伤力。

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
from typing import NamedTuple

import pytest

from tests.hosting import in_a_fresh_process

LANE = "coe-living"

# 起 agent-service 的宿主之后，它登记的每一样东西。处理函数、Data 都记成完整标识
# ``__module__.__qualname__``：``@node`` 用 ``functools.wraps`` 包装原函数，这两个属性原样保留，
# 拿到的是业务函数自己的坐标，不是 wrapper 的。
_REGISTERED = """
import json
def _id(f):
    f = getattr(f, 'func', f)  # a functools.partial: the function it calls
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
    'clocks': sorted([r.plugin, r.name] for r in rs if r.kind == 'clock'),
    'wakes': sorted([r.plugin, r.name] for r in rs if r.kind == 'wake'),
}))
"""

# 宿主能登记的每一种东西，按"能不能把外面的东西送进这个进程"分。能的那几种下面逐种查；钟不能：
# 每一拍交出去的只有钟响的时刻（最后一条）。轮次队列（``turns``）和给它发信号的钟（``wake``）
# 也不能：每一拍交给她队列的只有钟响的时刻（宿主造的 ``app.host.Signal``），判不判、判出哪一轮
# 由来源读库决定。宿主多出一种登记，这里先红，要重新判断它算哪一边。
_INBOUND_KINDS = frozenset({"route", "inbox", "inboxes_at_start"})
# 一轮由什么组成（工具、段落、段、各阶段的步骤、上一轮记录）只在她自己的轮次里被调用，不接外面的东西。
_NOT_INBOUND_KINDS = frozenset(
    {
        "clock",
        "task",
        "outbound",
        "service",
        "on_stop",
        "turns",
        "wake",
        "tool",
        "segment",
        "section",
        "round_start",
        "check",
        "commit",
        "after_commit",
        "round_log",
    }
)

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


# 一次性的搬运：旧的读书队列（持久边那一条）里剩下的消息，按读书收件箱的格式发进读书收件箱。它是
# reading 插件登记的（``app.living.reading.carry_over``），所以**处理函数在 app.living 里**，是唯一
# 一条到得了 living 的路由；能做到的跟运维入口往读书收件箱里投一条一样：让读书 agent 读一程，碰不到她的
# 一轮（下面 ``test_the_carry_over_handler_cannot_run_her_round`` 守着）。三项检查跟运维入口一样都开着。
# prod 切换完成、旧队列删掉之后，这一条连同路由一起删除。
_CARRY_OVER = Route(
    "POST",
    "/admin/reading/carry-over",
    "app.living.reading.carry_over.ReadingCarryOverRequest",
    "app.living.reading.carry_over.carry_over_reading",
    True,
    True,
    True,
)
_CARRY_OVER_HANDLER = "app.living.reading.carry_over"

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
    # :func:`app.living.household.received.receive` 存一行，等她自己醒来再读，一次模型都不当场跑
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
    _CARRY_OVER,
})

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
    """实验泳道上除了搬运那一条（:data:`_CARRY_OVER`，它的处理函数碰不到她的一轮，下面单独守着），没有
    任何路由能到达 ``app.living`` 里的处理函数，别的入口逐种过一遍。

    路由判的是**通向谁**：把处理函数摸出来（``__module__.__qualname__``），凡是落在
    ``app.living`` 里的就红。这样两个方向都堵上——给 living 的 Data 挂一条路由会红，把一条已经在
    白名单里的运维路由的处理函数换成 living 的节点也会红。

    第二条断言是围栏：路由必须逐字出现在 :data:`_OPS_ONLY_ROUTES` 里，连方法、路径、处理函数和
    检查一起对，所以多出来的任何一条路由都会红。收件箱由下面几条查；宿主多出一种登记，先红在第一条
    断言上。
    """
    registered = _registered(LANE)

    unknown = set(registered["kinds"]) - _INBOUND_KINDS - _NOT_INBOUND_KINDS
    assert unknown == set(), (
        f"宿主上多了一种登记：{sorted(unknown)}。先判断它能不能把外面的东西送进进程，"
        "再把它归进 _INBOUND_KINDS 或 _NOT_INBOUND_KINDS（能的话在这里加上它的检查）。"
    )

    routes = [Route(*r) for r in registered["routes"]]
    ears = [line for r in routes if r != _CARRY_OVER for line in _reaches_living(r)]
    assert ears == [], (
        "外面的东西能到达 living 的处理函数了 —— 新引擎长出了耳朵：\n  "
        + "\n  ".join(ears)
        + "\nliving 只能自己按钟醒；手机上的消息她每一轮直接查 common_message，"
        "别人发给她的消息只经她的收件箱存下（app.living.household.received），"
        "不接任何人推进来、直接调进 living 的东西。"
    )

    unlisted = [r for r in routes if r not in _OPS_ONLY_ROUTES]
    assert unlisted == [], (
        "多了一条白名单外的路由：\n  "
        + "\n  ".join(f"{r.method} {r.path} → {r.request} → {r.handler}" for r in unlisted)
        + f"\n确实是运维口的话，把它的完整身份（方法、路径、类标识、处理函数、检查）"
        f"加进 _OPS_ONLY_ROUTES，并说明为什么它的处理函数不在 {_LIVING_ROOT} 里。"
    )

    for name in ("ChatTrigger", "ChatRequest"):
        assert all(not r.request.endswith("." + name) for r in routes), (
            f"{name} 还挂着路由。拿到：{routes}"
        )


# 她的收件箱：开设它的那一个函数、处理每一条消息的那一个函数。
_OPEN_HER_INBOXES = "app.living.household.mailbox.Mailbox.open_inboxes"
_HER_INBOX_HANDLER = "app.living.household.received"

# 读书收件箱：名字、处理函数。
_READING_INBOX = ["reading", "app.living.reading.files.read_a_round"]
_READING_HANDLER = "app.living.reading.files"

# 她的一轮。读书收件箱的处理函数所在的模块连同它 import 的一切，不许碰到它。
_HER_ROUND = "app.living.life.moment"

# 能跑模型的代码住在这几处。收件处理所在的模块连同它 import 的一切，一个都不许碰到。
_MODEL_RUNNING = ("app.agent", "app.capabilities", _HER_ROUND)


def test_the_only_inboxes_living_opens_are_the_residents_and_reading():
    """life 开设的收件箱只有两类：三姐妹那一组，由 :meth:`app.living.household.mailbox.Mailbox.open_inboxes` 在开始
    接收时开；读书收件箱，setup 时直接开，处理函数是 :func:`app.living.reading.files.read_a_round`。

    三姐妹的名字在人设表里，setup 时只声明"开始接收时再开"；所以宿主起完 setup 之后，直接开设的
    收件箱落在 ``app.living`` 里的只许是读书那一个，"启动时再开"的声明只许是那一个。多出任何一个，
    都是又一条能把东西送进 life 的路，要重新判断。
    """
    registered = _registered(LANE)

    assert [
        [n, handler] for n, handler in registered["inboxes"] if _lives_in_living(handler)
    ] == [_READING_INBOX], registered["inboxes"]
    assert registered["at_start"] == [_OPEN_HER_INBOXES]


# 在干净进程里只 import 一个模块。它在她的某个插件目录里时，那个目录的 ``__init__`` 不跑：那是插件
# 向宿主登记自己（import 宿主，宿主又 import 整个 agent 层），不是处理函数手边的代码；跑了它，每个插件
# 目录里的模块都"碰得到模型"，这条检查就什么都分不出来了。目录照样是那个目录，模块按原路径 import。
_IMPORT_ALONE = """
import importlib, pathlib, sys, types
import app.living
module = {module!r}
package = module.rpartition(".")[0]
if package.startswith("app.living."):
    plugin = types.ModuleType(package)
    plugin.__path__ = [str(pathlib.Path(app.living.__file__).parent / package.rpartition(".")[2])]
    sys.modules[package] = plugin
importlib.import_module(module)
print(repr(sorted(m for m in sys.modules if m.startswith("app."))))
"""


def _app_modules_loaded_by_importing(module: str) -> list[str]:
    """在一个干净进程里只 import ``module``（连同它 import 的一切，不含它所在插件目录的登记，见
    :data:`_IMPORT_ALONE`），交回那之后 ``sys.modules`` 里的 ``app.*``。

    不走 ``in_a_fresh_process``：它先起整个 agent-service，那时什么模块都早就在了。
    """
    env = dict(os.environ)
    env["LANE"] = LANE
    proc = subprocess.run(
        [sys.executable, "-c", _IMPORT_ALONE.format(module=module)],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    loaded = ast.literal_eval(proc.stdout.strip().splitlines()[-1])
    assert module in loaded, f"用例前提没成立：{module} 根本没被 import"
    return loaded


def test_the_inbox_handler_cannot_reach_a_model():
    """收件处理所在的模块，连同它 import 的一切，一行能跑模型的代码都没有。

    "只存储"在这里是结构：处理函数手边根本没有能叫醒她、能调模型的东西。哪天有人在收件
    处理里 import 一轮 moment 或者一个 agent，这条就红。
    """
    loaded = _app_modules_loaded_by_importing(_HER_INBOX_HANDLER)
    reached = [
        m
        for m in loaded
        if any(m == root or m.startswith(root + ".") for root in _MODEL_RUNNING)
    ]
    assert reached == [], (
        f"收件处理（{_HER_INBOX_HANDLER}）能碰到跑模型的代码了：{reached}。"
        "收件只存储，她自己醒来时再读。"
    )


def test_the_reading_inbox_handler_cannot_run_her_round():
    """读书收件箱的处理函数所在的模块，连同它 import 的一切，碰不到她的一轮。

    运维入口能以任何身份往读书收件箱里投（跟往三姐妹的收件箱里投是同一个信任级别），投进来的
    东西能让读书 agent 读一程，不能让她跑一轮：处理函数手边没有 :mod:`app.living.life.moment`。
    """
    loaded = _app_modules_loaded_by_importing(_READING_HANDLER)
    reached = [m for m in loaded if m == _HER_ROUND or m.startswith(_HER_ROUND + ".")]
    assert reached == [], (
        f"读书收件箱的处理函数（{_READING_HANDLER}）碰得到她的一轮了：{reached}。"
        "往读书收件箱里投一条，只该让读书 agent 读一程。"
    )


def test_the_carry_over_handler_cannot_run_her_round():
    """搬运那条路由的处理函数所在的模块，连同它 import 的一切，碰不到她的一轮。

    它是唯一一条到得了 ``app.living`` 的路由（:data:`_CARRY_OVER`）：要内网凭据、核对泳道，搬进来的
    消息进读书收件箱，能让读书 agent 读一程，不能让她跑一轮。
    """
    loaded = _app_modules_loaded_by_importing(_CARRY_OVER_HANDLER)
    reached = [m for m in loaded if m == _HER_ROUND or m.startswith(_HER_ROUND + ".")]
    assert reached == [], (
        f"搬运那条路由的处理函数（{_CARRY_OVER_HANDLER}）碰得到她的一轮了：{reached}。"
    )


@pytest.mark.integration
async def test_her_inbox_only_stores(living_db, monkeypatch):
    """跑一遍她的收件处理：落一行，一次模型都不调，也不推进她的任何一轮 —— 没开始一轮（没有
    ``NudgeBegun``），没跑一轮（她一轮的固定循环一次都没进），也没落下一轮（没有 ``LifeMoment``）。

    盯的是跑一轮时在调用那一刻才去查的名字：:func:`app.living.life.moment.run_due_moment` 每跑一轮都从它
    的模块里取 ``run_round``。``run_due_moment`` 本身换不得：两个唤醒来源在 setup 时就把它绑进去了，换掉
    它什么都拦不住。
    """
    from types import SimpleNamespace

    from app.data.persist import fetch_all
    from app.data.schema import table_name
    from app.living.life import moment as moment_mod
    from app.living.life.moment import LifeMoment
    from app.living.life.nudge import NudgeBegun
    from app.living.residents import names as names_mod
    from app.living.residents.names import PersonaNames
    from app.messaging.message import Kind, new_message
    from app.messaging.receiving import INBOX_REGISTRY, inbox
    from tests.living.conftest import her_mailbox
    from tests.runtime.conftest import migrate

    for cls in (LifeMoment, NudgeBegun):
        await migrate(cls, living_db)
    monkeypatch.setenv("LANE", LANE)
    names = {"akao": "赤尾", "ayana": "绫奈", "chinagi": "千凪"}

    async def find_persona(persona_id: str):
        return SimpleNamespace(persona_id=persona_id, display_name=names[persona_id])

    monkeypatch.setattr(names_mod, "find_persona", find_persona)

    def no_model(*args, **kwargs):
        raise AssertionError("收件处理调到模型了")

    rounds: list[str] = []

    async def her_round_ran(profile, *, participant, **kwargs):
        rounds.append(participant)
        raise AssertionError("收件处理跑了她一轮")

    monkeypatch.setattr("app.agent.core.build_model_client", no_model)
    monkeypatch.setattr(moment_mod, "run_round", her_round_ran)

    await her_mailbox(PersonaNames(tuple(names))).open_inboxes(inbox)
    for name in names.values():
        await INBOX_REGISTRY[name].on_message(
            new_message(sender="world", recipient=name, body="窗外下起了雨。", kind=Kind.MESSAGE)
        )

    stored = [
        r["persona_id"]
        for r in await fetch_all(
            "SELECT persona_id FROM data_received_message ORDER BY persona_id"
        )
    ]
    assert stored == sorted(names)
    assert rounds == [], f"收件处理跑了她们的一轮：{rounds}"
    for cls in (NudgeBegun, LifeMoment):
        rows = await fetch_all(f"SELECT persona_id FROM {table_name(cls)}")
        assert rows == [], f"收件处理推进了她的一轮（{cls.__name__}）：{rows}"


def test_the_only_clocks_are_the_landing_and_her_wakes():
    """宿主上的钟只有对账那一条（speaking 插件登记）；她的轮次只由常规节奏和提前叫醒这两个来源的钟叫醒，日记、
    回看各由自己的钟叫醒，每人各一条队列。"""
    registered = _registered(LANE)
    assert registered["clocks"] == [["speaking", "landing"]]
    assert registered["wakes"] == sorted(
        [
            *(
                ["life", f"{source} -> {persona}/round"]
                for source in ("nudge", "regular")
                for persona in ("akao", "ayana", "chinagi")
            ),
            *(
                [plugin, f"{line} -> {persona}/{line}"]
                for plugin, line in (("day_page", "day-page"), ("persona_review", "persona-review"))
                for persona in ("akao", "ayana", "chinagi")
            ),
        ]
    )
