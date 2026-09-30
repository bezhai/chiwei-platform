"""两个 world 进程同时起来：只有拿到写锁的那一个在消费；它退出之后，另一个接上。

发布是滚动更新，单副本也会有新旧两个 world 进程同时在跑的一段。这里起两个真的操作系统
进程（``world_process.py``），连同一个 broker、同一个 Postgres、同一块卷，看每一轮落在哪个
进程上。
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from app.messaging.lifecycle import start_messaging
from app.messaging.sending import send
from tests.messaging.conftest import (  # noqa: F401
    LANE,
    broker,
    delayed_broker,
    messaging_db,
)
from tests.messaging.helpers import eventually
from tests.runtime.conftest import test_db, test_db_dsn  # noqa: F401

pytestmark = pytest.mark.usefixtures("messaging_db")

AGENT_SERVICE = Path(__file__).resolve().parents[2]


def _lines(path: Path) -> list[tuple[int, str]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        pid, _, rest = line.partition(" ")
        out.append((int(pid), rest))
    return out


@pytest.fixture
def two_world_processes(delayed_broker, test_db_dsn, tmp_path):  # noqa: F811
    amqp_url, _ = delayed_broker
    db = make_url(test_db_dsn)
    rounds, events = tmp_path / "rounds.log", tmp_path / "events.log"
    env = dict(os.environ)
    env.pop("RABBITMQ_DISABLE_DELAYED", None)
    env.update(
        APP_NAME="world",
        LANE=LANE,
        WORLD_DATA_DIR=str(tmp_path / "world-volume"),
        RABBITMQ_URL=amqp_url,
        POSTGRES_HOST=db.host,
        POSTGRES_PORT=str(db.port),
        POSTGRES_USER=db.username,
        POSTGRES_PASSWORD=db.password,
        POSTGRES_DB=db.database,
        WORLD_TEST_ROUNDS=str(rounds),
        WORLD_TEST_EVENTS=str(events),
        PYTHONPATH=str(AGENT_SERVICE),
    )
    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "tests.world.world_process"],
            cwd=AGENT_SERVICE,
            env=env,
            stdout=open(tmp_path / f"process-{i}.log", "w"),
            stderr=subprocess.STDOUT,
        )
        for i in range(2)
    ]
    try:
        yield procs, rounds, events
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.wait(timeout=10)


async def test_only_the_lock_holder_consumes_and_the_other_takes_over_when_it_exits(
    broker, two_world_processes  # noqa: F811
):
    procs, rounds, events = two_world_processes
    pids = {p.pid for p in procs}
    await start_messaging()  # 这个测试进程只用来发消息，它自己没有开设 world 收件箱

    def holders():
        return [pid for pid, line in _lines(events) if "holding the writer lock" in line]

    def waiters():
        return {pid for pid, line in _lines(events) if "waiting" in line}

    await eventually(lambda: len(holders()) == 1 and len(waiters()) == 1, timeout=30)
    first = holders()[0]
    second = (pids - {first}).pop()
    assert waiters() == {second}

    # 拿着锁的那个做启动补醒，然后处理发来的消息；另一个一轮都不跑。
    await eventually(lambda: len(_lines(rounds)) >= 1, timeout=20)
    for i in range(3):
        await send(sender="operator", recipient="world", body=f"第 {i} 条。")
    await eventually(lambda: len(_lines(rounds)) >= 4, timeout=20)
    time.sleep(1.0)
    assert {pid for pid, _ in _lines(rounds)} == {first}

    # 第一个正常退出：停消费、等正在处理的、放锁。第二个接上，接着处理。
    holder = next(p for p in procs if p.pid == first)
    holder.send_signal(signal.SIGTERM)
    holder.wait(timeout=30)
    assert holder.returncode == 0
    first_events = [line for pid, line in _lines(events) if pid == first]
    released = next(i for i, line in enumerate(first_events) if "released the writer lock" in line)
    assert released < first_events.index("stopped")

    await eventually(lambda: second in holders(), timeout=20)
    before = len(_lines(rounds))
    for i in range(2):
        await send(sender="operator", recipient="world", body=f"接上之后第 {i} 条。")
    await eventually(lambda: len(_lines(rounds)) >= before + 2, timeout=20)

    after_switch = _lines(rounds)[before:]
    assert {pid for pid, _ in after_switch} == {second}
    assert ["接上之后第 0 条。" in after_switch[0][1], "接上之后第 1 条。" in after_switch[1][1]] == [
        True,
        True,
    ]
    # 第二个在拿到锁之前一轮都没跑过。
    assert all(pid == first for pid, _ in _lines(rounds)[:before])
