"""ARQ Worker configuration — long_tasks subsystem (state_sync moved to dataflow).

Phase 4 cutover: life-engine / glimpse / voice / review / daily-plan cron
迁到 dataflow Source.cron + graph fan-out。
Phase 6 v4 cutover: sync_life_state_after_schedule 改 dataflow durable wire +
sync_life_state_node（app/nodes/sync_life_state.py）。

arq-worker 现在只剩：
  - task_executor cron（每分钟轮询 long_tasks 表 —— long_tasks 子系统独立）

Start command:
    arq app.workers.arq_settings.WorkerSettings
"""

from __future__ import annotations

import logging

from arq import cron
from arq.connections import RedisSettings
from inner_shared.logger import setup_logging

from app.infra.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Long-task executor (every-minute poll)
# ---------------------------------------------------------------------------


async def task_executor_job(ctx) -> None:
    """arq cron: poll and execute long-running tasks."""
    from app.infra.config import settings as _s
    from app.long_tasks.executor import poll_and_execute_tasks

    await poll_and_execute_tasks(
        batch_size=_s.long_task_batch_size,
        lock_timeout_seconds=_s.long_task_lock_timeout,
    )


# ---------------------------------------------------------------------------
# Startup hook
# ---------------------------------------------------------------------------


async def on_startup(ctx) -> None:
    """Worker startup: configure logging, connect MQ.

    MQ connect is kept in case long_tasks executor needs to publish.
    """
    setup_logging(log_dir="/logs/agent-service", log_file="arq-worker.log")
    logger.info("arq-worker started, file logging enabled")

    from app.infra.rabbitmq import mq

    await mq.connect()
    await mq.declare_topology()


# ---------------------------------------------------------------------------
# Worker settings
# ---------------------------------------------------------------------------


class WorkerSettings:
    """Unified ARQ Worker configuration.

    Start command:
        arq app.workers.arq_settings.WorkerSettings
    """

    on_startup = on_startup

    queue_name = f"arq:queue:{settings.lane}" if settings.lane else "arq:queue"

    redis_settings = RedisSettings(
        host=settings.redis_host or "localhost",
        port=6379,
        password=settings.redis_password,
        database=0,
    )

    functions: list = []

    cron_jobs = [
        # task_executor: long_tasks 子系统独立保留，不在 Phase 4 范围
        cron(task_executor_job, minute=None),
    ]
