"""ARQ Worker configuration — cron schedule + Redis connection.

Start command:
    arq app.workers.arq_settings.WorkerSettings
"""

from __future__ import annotations

import asyncio
import logging

from arq import cron
from arq.connections import RedisSettings
from inner_shared.logger import setup_logging

from app.infra.config import settings
from app.workers.cron import (
    cron_generate_daily_plan,
    cron_generate_dreams,
    cron_generate_monthly_plan,
    cron_generate_voice,
    cron_generate_weekly_dreams,
    cron_generate_weekly_plan,
    cron_glimpse,
    cron_life_engine_tick,
)
from app.workers.vectorize import cron_scan_pending_messages

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
    """Worker startup: configure logging, connect MQ, seed voice."""
    setup_logging(log_dir="/logs/agent-service", log_file="arq-worker.log")
    logger.info("arq-worker started, file logging enabled")

    # Life Engine + Glimpse need MQ to publish proactive chat messages
    from app.infra.rabbitmq import mq

    await mq.connect()
    await mq.declare_topology()

    # Generate voice on startup for all personas
    asyncio.create_task(cron_generate_voice(None))


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

    # Cron schedule (CST timezone, night pipeline sequence)
    # NOTE: ArQ default job_timeout=300s is too short for multi-persona LLM
    # pipelines. Timeouts are set explicitly per task.
    cron_jobs = [
        # 1. Long-task executor: every minute
        cron(task_executor_job, minute=None),
        # 1b. Life Engine tick: every minute
        cron(cron_life_engine_tick, minute=None, timeout=120),
        # 1c. Glimpse: every 5 minutes (browsing state only)
        cron(
            cron_glimpse,
            minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55},
            timeout=120,
        ),
        # 2. Vectorize pending scan: every 10 minutes
        cron(cron_scan_pending_messages, minute={0, 10, 20, 30, 40, 50}),
        # 3. Daily dream: CST 03:00
        cron(cron_generate_dreams, hour={3}, minute={0}, timeout=3600),
        # 4. Weekly dream: Monday CST 04:00
        cron(
            cron_generate_weekly_dreams, weekday={0}, hour={4}, minute={0}, timeout=1800
        ),
        # 5. Daily plan: CST 05:00 (after dreams)
        cron(cron_generate_daily_plan, hour={5}, minute={0}, timeout=3600),
        # 5b. Weekly plan: Sunday CST 23:00
        cron(
            cron_generate_weekly_plan, weekday={6}, hour={23}, minute={0}, timeout=1800
        ),
        # 5c. Monthly plan: 1st of month CST 02:00
        cron(cron_generate_monthly_plan, day={1}, hour={2}, minute={0}, timeout=1800),
        # 6. Voice: CST 08:00-23:00 every hour
        cron(cron_generate_voice, hour=set(range(8, 24)), minute={0}, timeout=1800),
    ]
