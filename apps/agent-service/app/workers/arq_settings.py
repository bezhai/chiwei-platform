"""ARQ Worker configuration — long_task executor cron + event-driven workers.

Phase 4 cutover: life-engine / glimpse / voice / review / daily-plan cron
迁到 dataflow Source.cron + graph fan-out node（在 agent-service 主进程
lifespan 里跑）。arq-worker 现在只剩：
  - task_executor cron（每分钟轮询 long_tasks 表 —— long_tasks 子系统独立）
  - sync_life_state_after_schedule（事件触发 worker function）

Start command:
    arq app.workers.arq_settings.WorkerSettings
"""

from __future__ import annotations

import logging

from arq import cron
from arq.connections import RedisSettings
from inner_shared.logger import setup_logging

from app.infra.config import settings
from app.workers.state_sync_worker import sync_life_state_after_schedule

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

    Phase 4: removed seed voice (cron_generate_voice). voice 由 dataflow
    主进程 graph cron 接管；arq-worker 不再承担 voice / life-engine /
    glimpse / review / daily-plan 调度。
    """
    setup_logging(log_dir="/logs/agent-service", log_file="arq-worker.log")
    logger.info("arq-worker started, file logging enabled")

    # MQ connect — sync_life_state_after_schedule 可能 emit 触发下游
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

    functions: list = [sync_life_state_after_schedule]

    cron_jobs = [
        # task_executor: long_tasks 子系统独立保留，不在 Phase 4 范围
        cron(task_executor_job, minute=None),
    ]
