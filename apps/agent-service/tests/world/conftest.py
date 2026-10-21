"""world 测试共用：临时的私有卷、起 world 的插件宿主（``app_host``）、替身模型和一轮之外的替身。

主 agent 的一轮由固定循环跑（:func:`app.agent.round.run_round`，它造 ``app.agent.core.Agent``），
另外三类（感知判断、NPC、应答）经 :func:`app.world.agents.run_agent` 造 ``Agent``，替身从这两处
换：按这次调用的名字（它的 prompt id）分给各自的替身，同时记下每一次调用的是哪一类、它的 spec、
拿到了哪些工具。上下文存储和成本也在这两处换。
"""
from __future__ import annotations

import importlib
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.agent.neutral import Message as Turn
from app.agent.neutral import Role
from app.agent.round import Call, Round, in_call, in_round
from app.infra.cst_time import now_cst
from app.messaging import receiving
from app.messaging.message import Delivery, Kind, SendFailed, new_message
from app.world import agents, perception
from app.world.core import main_agent, wake
from app.world.core.actions import wake_me_at
from app.world.core.rounds import InboxRound

LANE = "coe-world"

# 这次调用交给模型的工具（名字 → 工具）：替身模型只够得着这些，跟真的模型一样。
_offered: ContextVar[dict[str, Any]] = ContextVar("world_test_offered_tools")


async def use_tool(name: str, arguments: dict[str, Any]) -> Any:
    """替身模型在这次调用里调工具 ``name``：只有交给这次调用的工具才调得到。"""
    return await _offered.get()[name].invoke(arguments)


@contextmanager
def in_world_round(
    messages: Sequence = (), *, agent: str = "world_round"
) -> Iterator[tuple[Round, Call]]:
    """把工具放进 world 的一轮、主 agent 的一次调用里直接调，不起模型：绑上这一轮（带着
    ``messages``）和这次调用，交回两者。"""
    world_round = Round(
        app="world",
        lane=LANE,
        participant="world",
        profile="world",
        round_id="round-under-test",
        now=now_cst(),
        reason="test",
        due=InboxRound(reason="test", messages=tuple(messages)),
    )
    call = Call(agent=agent, session=None, round=world_round)
    with in_round(world_round), in_call(call):
        yield world_round, call


@pytest.fixture
def bare_volume(tmp_path, monkeypatch) -> Path:
    """``WORLD_DATA_DIR`` 指向一个空目录，进程的部署泳道是 :data:`LANE`；写锁不在手上。"""
    root = tmp_path / "world-volume"
    root.mkdir()
    monkeypatch.setenv("WORLD_DATA_DIR", str(root))
    monkeypatch.setenv("LANE", LANE)
    return root


@pytest.fixture
def volume(bare_volume) -> Path:
    """同 :func:`bare_volume`，并且这个进程拿着这条泳道的写锁——跑着的 world 就是这样。"""
    from app.world import volume as world_volume

    assert world_volume.try_acquire_writer_lock()
    yield bare_volume
    world_volume.release_writer_lock()


class FakeRunner:
    """替身模型：每轮按 ``plan`` 在这一轮里、这次调用上调工具，记下它看到的输入。"""

    def __init__(self, plan):
        self.plan = plan
        self.runs: list[list[Turn]] = []

    async def run(self, messages, *, call, transcript_sink, **_):
        self.runs.append(list(messages))
        with in_call(call):
            said = await self.plan()
        reply = Turn(role=Role.ASSISTANT, content=said)
        transcript_sink.append(reply)
        return reply


class ScriptedAgent:
    """主 agent 之外那几类的替身：每次调用按 ``plan(输入那段话)`` 在这次调用上调工具，``plan``
    交回的话就是它最后说的。记下每次看到的输入和调用。"""

    def __init__(self, plan):
        self.plan = plan
        self.inputs: list[str] = []
        self.calls: list = []

    async def run(self, messages, *, call, transcript_sink=None, **_):
        text = messages[-1].content
        self.inputs.append(text)
        self.calls.append(call)
        with in_call(call):
            said = await self.plan(text)
        return Turn(role=Role.ASSISTANT, content=said)


def sets_wake(hours: float = 2, reason: str = "过一阵再看看。"):
    async def plan():
        at = (now_cst() + timedelta(hours=hours)).replace(microsecond=0)
        await wake_me_at.invoke({"at": at.isoformat(), "reason": reason})
        return "这一轮看完了。"

    return plan


def sets_nothing():
    async def plan():
        return "看完了，但忘了定时刻。"

    return plan


@pytest.fixture
def world_manifest():
    """``world`` 按哪份清单起宿主：默认就是 world 的清单（:mod:`app.manifests.world`）。用例模块
    覆盖这个 fixture 就换一份清单，比如多一个来源插件。"""
    from app.manifests.world import MANIFEST

    return MANIFEST


@pytest.fixture
async def world(volume, monkeypatch, app_host, world_manifest):
    """按 ``world_manifest`` 起 world 的宿主（只跑 setup），把一轮之外的东西换成替身，交回一个
    可以查看的句柄；``profile`` 是宿主按清单组好的 ``world``，跟 world 插件交给收件箱的是同一份。"""

    class Handle:
        runner: FakeRunner
        # 宿主组好的 ``world``（:class:`app.agent.round.AssembledProfile`）。
        profile = None
        # 主 agent 之外那三类的替身：prompt id → runner。
        agents: dict[str, object] = {}
        # 每一次调用：(调用的名字, AgentSpec, 拿到的工具名)。
        built: list[tuple] = []
        scheduled: list[dict] = []
        # 感知判断之后代码发出去的告知（和它们的消息 id、要不要叫醒收件人，按发的先后）；开设了
        # 收件箱的名字；发送会抛 SendFailed 的名字；每次发之前调一下的钩子（拿到收件人，可以在
        # 这里抛异常，模拟发到一半进程死了或者被取消）。
        sent: list[dict] = []
        sent_ids: list[str] = []
        sent_wakes: list[bool] = []
        open_inboxes: set[str] = set()
        send_fails: set[str] = set()
        before_send = None
        committed: list[dict] = []
        costs: list[dict] = []
        history: list[Turn] = []
        ver = 3
        # 起着的 world 宿主，和它的收件箱交给通信机制的处理函数（:func:`restart` 换）。
        host = None
        handler = None
        # 通信机制记下了"处理成功"的消息 id：同一条再来，交不到处理函数手里。
        succeeded: set[str] = set()

    h = Handle()
    h.agents, h.built = {}, []
    h.scheduled, h.committed, h.costs = [], [], []
    h.sent, h.sent_ids, h.sent_wakes = [], [], []
    h.open_inboxes, h.send_fails = set(), set()
    h.before_send = None
    h.history = [Turn(role=Role.USER, content="上一轮的输入。")]
    h.runner = FakeRunner(sets_wake())
    h.succeeded = set()

    async def deliver(message, *, success_recorded: bool = True):
        """通信机制投一次：成功记下来过的不再交给处理函数；处理函数正常返回之后才记成功，
        ``success_recorded=False`` 是记成功的那一笔没写成（或者进程死在返回和记下之间）。"""
        if message.message_id in h.succeeded:
            return
        await h.handler(message)
        if success_recorded:
            h.succeeded.add(message.message_id)

    async def succeeded_message_ids(inbox_name, message_ids):
        assert inbox_name == "world"
        return {m for m in message_ids if m in h.succeeded}

    h.deliver = deliver

    async def send_at(**kw):
        h.scheduled.append(kw)
        return kw["message_id"]

    async def send(*, sender, recipient, body, message_id=None, wakes_recipient=True):
        if h.before_send is not None:
            h.before_send(recipient)
        if recipient in h.send_fails:
            raise SendFailed("broker did not confirm", message_id=message_id)
        h.sent.append({"sender": sender, "recipient": recipient, "body": body})
        h.sent_ids.append(message_id)
        h.sent_wakes.append(wakes_recipient)
        if recipient in h.open_inboxes:
            return Delivery(f"n{len(h.sent)}", delivered=True)
        return Delivery(f"n{len(h.sent)}", delivered=False, reason="对方没有开设收件箱")

    async def load_session(key):
        h.loaded_key = key
        return list(h.history), h.ver

    async def commit_transcript(key, messages, *, expected_ver):
        """跟真的一样按版本做 CAS，写下的就是下一轮 ``load_session`` 读回来的那一版。"""
        from app.agent.continuity import TranscriptConflict

        if expected_ver != h.ver:
            raise TranscriptConflict(f"读到的是 ver={expected_ver}，现在是 {h.ver}")
        h.committed.append({"key": key, "messages": messages, "expected_ver": expected_ver})
        h.history, h.ver = list(messages), expected_ver + 1

    async def record_round_cost(**kw):
        h.costs.append(kw)

    class Dispatch:
        """按这次调用的名字交给主 agent 的替身或那一类的替身，记下调用的是谁、拿到哪些工具。"""

        def __init__(self, spec, *, tools=(), never_ran=None):
            self.spec, self.tools = spec, {t.tool.name: t.tool for t in tools}

        async def run(self, messages, *, call, **kw):
            h.built.append((call.agent, self.spec, list(self.tools)))
            target = h.runner if call.agent == main_agent.ROUND else h.agents[call.agent]
            offered = _offered.set(self.tools)
            try:
                return await target.run(messages, call=call, **kw)
            finally:
                _offered.reset(offered)

    from inner_shared.dynamic_config import dynamic_config

    import app.agent.core as agent_core
    import app.agent.round as agent_round
    import app.manifests.world as manifest_module
    from app.host import Plugin

    monkeypatch.setattr(dynamic_config, "get", lambda k, default="": default)
    monkeypatch.setattr(dynamic_config, "get_int", lambda k, default=0: default)
    monkeypatch.setattr(wake, "send_at", send_at)
    monkeypatch.setattr(perception, "send", send)
    monkeypatch.setattr(agent_round, "load_session", load_session)
    monkeypatch.setattr(agent_round, "commit_transcript", commit_transcript)
    monkeypatch.setattr(agent_round, "record_round_cost", record_round_cost)
    monkeypatch.setattr(agent_core, "Agent", Dispatch)
    monkeypatch.setattr(agents, "record_round_cost", record_round_cost)
    monkeypatch.setattr(agents, "Agent", Dispatch)
    monkeypatch.setattr(receiving, "succeeded_message_ids", succeeded_message_ids)
    monkeypatch.setattr(manifest_module, "MANIFEST", world_manifest)
    assembled = []
    takes_the_profile = Plugin(
        name="world-round-under-test",
        setup=lambda ctx: assembled.append(ctx.profile("world")),
    )
    h.host = await app_host(
        "world",
        [
            *(importlib.import_module(m).PLUGIN for m in world_manifest.plugins),
            takes_the_profile,
        ],
    )
    h.profile = assembled[0]
    h.handler = _world_inbox_handler(h.host)
    return h


async def run_round_taking(
    world_handle,
    messages: Sequence,
    *,
    round_id: str = "round-under-test",
    now: datetime | None = None,
):
    """不经过收件箱直接跑一轮：带着 ``messages``（按到达的先后），按 ``world`` 组好的那一份，
    这一轮的 id 和时刻是给的（没给时刻就是现在）。"""
    return await main_agent.run_inbox_round(
        world_handle.profile,
        InboxRound(reason="test", messages=tuple(messages)),
        round_id=round_id,
        now=now_cst() if now is None else now,
    )


async def restart(world_handle) -> None:
    """一个新的 world 进程：宿主停了再起，之后的投递交给新的收件箱处理函数。进程里的东西
    都是新的，私有卷上的还在。"""
    from tests.hosting import start_without_io

    await world_handle.host.stop()
    await start_without_io(world_handle.host)
    world_handle.handler = _world_inbox_handler(world_handle.host)


def _world_inbox_handler(host):
    """宿主登记的 ``world`` 收件箱交给通信机制的那个处理函数。"""
    (inbox,) = [r for r in host.registered() if r.kind == "inbox" and r.name == "world"]
    return inbox.detail["on_message"]


async def ask(world_handle, question):
    """问 world 一个问题：交给 world 收件箱登记的 ``on_question``，跟通信机制交的一样。"""
    (inbox,) = [
        r for r in world_handle.host.registered() if r.kind == "inbox" and r.name == "world"
    ]
    return await inbox.detail["on_question"](question)


def tools_built_for(world_handle, agent: str) -> list[str]:
    """最近一次调用 ``agent`` 那一类时，它拿到的工具名。"""
    return [names for name, _, names in world_handle.built if name == agent][-1]


def spec_called(world_handle, agent: str):
    """调用 ``agent`` 那一类时用的 AgentSpec（每次都一样，取第一次的）。"""
    return [spec for name, spec, _ in world_handle.built if name == agent][0]


def self_message(message_id: str, body: str = "醒来。"):
    return new_message(
        sender="world", recipient="world", body=body, kind=Kind.MESSAGE, message_id=message_id
    )


@pytest.fixture(autouse=True)
def _prompts(prompts_from_fixtures):
    """一轮取齐的 prompt 来自回放的 prompt 快照（根 conftest 的 ``prompts_from_fixtures``）。"""
    return prompts_from_fixtures
