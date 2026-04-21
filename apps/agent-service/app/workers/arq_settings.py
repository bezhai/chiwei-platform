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
    cron_generate_voice,
    cron_glimpse,
    cron_life_engine_tick,
    cron_memory_reviewer_light_day,
    cron_memory_reviewer_light_night,
)
from app.workers.state_sync_worker import sync_life_state_after_schedule
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


async def _run_memory_v4_migration_once() -> None:
    """One-shot memory v4 migrations, gated by redis flag so each kind runs once.

    After cutover the first arq-worker startup in prod triggers the relationship
    and fragment migrations. Set ``memory_v4:migration:done:<kind>`` in redis
    manually to re-run a specific kind.
    """
    from app.infra.redis import get_redis

    redis = await get_redis()
    for kind in ("relationship", "fragment"):
        key = f"memory_v4:migration:done:{kind}"
        if await redis.get(key):
            logger.info("memory v4 migration (%s): already done, skip", kind)
            continue
        try:
            logger.info("memory v4 migration (%s): starting", kind)
            if kind == "relationship":
                from scripts.migrate_relationship_to_abstract import main
                await main(dry_run=False, limit=None)
            else:
                from scripts.migrate_fragment_to_fragment import main
                await main(dry_run=False, limit=None, days=7)
            await redis.set(key, "1")
            logger.info("memory v4 migration (%s): done", kind)
        except Exception:
            logger.exception("memory v4 migration (%s): failed", kind)


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

    # One-shot memory v4 migrations (runs once per redis flag; cutover path)
    asyncio.create_task(_run_memory_v4_migration_once())


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
        # 1d. Memory reviewer — light (daytime, every 30min, 08:00-21:00 CST)
        cron(
            cron_memory_reviewer_light_day,
            hour=set(range(8, 22)),  # 8..21
            minute={0, 30},
            timeout=600,
        ),
        # 1e. Memory reviewer — light (nighttime, hourly, skips 03:00 = heavy slot)
        cron(
            cron_memory_reviewer_light_night,
            hour={22, 23, 0, 1, 2, 4, 5, 6, 7},
            minute={0},
            timeout=600,
        ),
        # 2. Vectorize pending scan: every 10 minutes
        cron(cron_scan_pending_messages, minute={0, 10, 20, 30, 40, 50}),
        # 3. Heavy reviewer (daily consolidation): CST 03:00
        cron(cron_generate_dreams, hour={3}, minute={0}, timeout=3600),
        # 4. Daily plan (Agent Team pipeline): CST 05:00 (after heavy review)
        cron(cron_generate_daily_plan, hour={5}, minute={0}, timeout=3600),
        # 6. Voice: CST 08:00-23:00 every hour
        cron(cron_generate_voice, hour=set(range(8, 24)), minute={0}, timeout=1800),
    ]
