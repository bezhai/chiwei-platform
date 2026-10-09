"""测试用的一个 world 进程：真的 broker、真的 Postgres、真的写锁，模型和上下文存储换成替身。

``test_single_writer.py`` 同时起两个这样的进程。每跑一轮，往 ``WORLD_TEST_ROUNDS`` 追加一行
"<pid> <这一轮眼前那段话，换行换成 | >"；写锁拿到、放开，以及进程起来、停下，各往
``WORLD_TEST_EVENTS`` 追加一行。收到 SIGTERM 就按正常关闭的顺序停下（停消费 → 等正在处理
的那一轮 → 放锁），然后退出。
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import timedelta
from unittest.mock import MagicMock, patch

patch("inner_shared.logger.setup_logging", MagicMock()).start()

ROUNDS = os.environ["WORLD_TEST_ROUNDS"]
EVENTS = os.environ["WORLD_TEST_EVENTS"]
PID = os.getpid()


def _append(path: str, line: str) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{PID} {line}\n")


class _EventsHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        _append(EVENTS, record.getMessage())


class _Runner:
    async def run(self, messages, *, context, transcript_sink, **_):
        from app.agent.neutral import Message as Turn
        from app.agent.neutral import Role
        from app.agent.runtime_context import agent_context
        from app.infra.cst_time import now_cst
        from app.world.actions import wake_me_at

        _append(ROUNDS, " | ".join(messages[-1].content.splitlines()))
        with agent_context(context):
            at = now_cst() + timedelta(days=1)
            await wake_me_at.invoke({"at": at.isoformat(), "reason": "明天再看。"})
        reply = Turn(role=Role.ASSISTANT, content="好。")
        transcript_sink.append(reply)
        return reply


async def main() -> None:
    from inner_shared.dynamic_config import dynamic_config

    from app.host import Host
    from app.world import agents, main_agent, volume

    volume.WRITER_LOCK_POLL_SECONDS = 0.2
    logging.getLogger("app.world.volume").addHandler(_EventsHandler())
    logging.getLogger("app.world.volume").setLevel(logging.INFO)

    async def load_session(key):
        return [], 0

    async def nothing(*args, **kwargs):
        return None

    main_agent.load_session = load_session
    main_agent.commit_transcript = nothing
    agents.record_round_cost = nothing
    agents.build_runner = lambda config, tools: _Runner()
    dynamic_config.get = lambda key, default="": default
    dynamic_config.get_int = lambda key, default=0: default

    # world 的进程怎么起就怎么起（``Host.for_app``），只是不建表、不挂 HTTP、不起钟和后台任务。
    host = Host.for_app("world")
    stop = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
    await host.start(http=None, schema=False, mq=True, clocks=False, tasks=False)
    _append(EVENTS, "started")
    await stop.wait()
    await host.stop()
    _append(EVENTS, "stopped")


if __name__ == "__main__":
    asyncio.run(main())
