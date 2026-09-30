"""world 测试共用：临时的私有卷、重新执行一遍 world 的接线、替身模型和一轮之外的替身。"""
from __future__ import annotations

import importlib
from datetime import timedelta
from pathlib import Path

import pytest

from app.agent.neutral import Message as Turn
from app.agent.neutral import Role
from app.agent.runtime_context import agent_context
from app.infra.cst_time import now_cst
from app.messaging.message import Kind, new_message
from app.world import main_agent, wake
from app.world.tools import wake_me_at

LANE = "coe-world"


@pytest.fixture
def volume(tmp_path, monkeypatch) -> Path:
    """``WORLD_DATA_DIR`` 指向一个空目录，进程的部署泳道是 :data:`LANE`。"""
    root = tmp_path / "world-volume"
    root.mkdir()
    monkeypatch.setenv("WORLD_DATA_DIR", str(root))
    monkeypatch.setenv("LANE", LANE)
    return root


def load_world_wiring() -> None:
    """清空三张登记表，再执行一遍 ``app.world.wiring``。

    先 import 再清：这个 worker 第一次 import 它时模块体已经跑过一遍（收件箱、节点绑定
    都登记了），不清就 reload 会撞上"已经登记过"。
    """
    import app.world.wiring as wiring
    from app.messaging.receiving import clear_inboxes
    from app.runtime.placement import clear_bindings
    from app.runtime.wire import clear_wiring

    clear_wiring()
    clear_bindings()
    clear_inboxes()
    importlib.reload(wiring)


class FakeRunner:
    """替身模型：每轮按 ``plan`` 在这一轮的 context 里调工具，记下它看到的输入。"""

    def __init__(self, plan):
        self.plan = plan
        self.runs: list[list[Turn]] = []
        self.configs = []

    async def run(self, messages, *, context, max_retries, transcript_sink, **_):
        self.runs.append(list(messages))
        with agent_context(context):
            said = await self.plan()
        reply = Turn(role=Role.ASSISTANT, content=said)
        transcript_sink.append(reply)
        return reply


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
def world(volume, monkeypatch):
    """把一轮之外的东西换成替身，交回一个可以查看的句柄。"""

    class Handle:
        runner: FakeRunner
        scheduled: list[dict] = []
        committed: list[dict] = []
        costs: list[dict] = []
        history: list[Turn] = []
        ver = 3

    h = Handle()
    h.scheduled, h.committed, h.costs = [], [], []
    h.history = [Turn(role=Role.USER, content="上一轮的输入。")]
    h.runner = FakeRunner(sets_wake())

    async def send_at(**kw):
        h.scheduled.append(kw)
        return kw["message_id"]

    async def load_session(key):
        h.loaded_key = key
        return list(h.history), h.ver

    async def commit_transcript(key, messages, *, expected_ver, session):
        h.committed.append({"key": key, "messages": messages, "expected_ver": expected_ver})

    async def record_round_cost(**kw):
        h.costs.append(kw)

    def build_round_runner(config):
        h.runner.configs.append(config)
        return h.runner

    from inner_shared.dynamic_config import dynamic_config

    monkeypatch.setattr(dynamic_config, "get", lambda k, default="": default)
    monkeypatch.setattr(dynamic_config, "get_int", lambda k, default=0: default)
    monkeypatch.setattr(wake, "send_at", send_at)
    monkeypatch.setattr(main_agent, "load_session", load_session)
    monkeypatch.setattr(main_agent, "commit_transcript", commit_transcript)
    monkeypatch.setattr(main_agent, "record_round_cost", record_round_cost)
    monkeypatch.setattr(main_agent, "build_round_runner", build_round_runner)
    return h


def self_message(message_id: str, body: str = "醒来。"):
    return new_message(
        sender="world", recipient="world", body=body, kind=Kind.MESSAGE, message_id=message_id
    )
