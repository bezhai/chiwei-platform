"""
统一的 ARQ Worker 配置
整合了长期任务（long_tasks）的所有worker功能

启动命令：
    arq app.workers.unified_worker.UnifiedWorkerSettings
"""

import logging

from arq import cron
from arq.connections import RedisSettings
from inner_shared.logger import setup_logging

from app.config.config import settings
from app.long_tasks.executor import poll_and_execute_tasks
from app.workers.memory_worker import cron_consolidate_profiles
from app.workers.vectorize_worker import cron_scan_pending_messages

logger = logging.getLogger(__name__)


# ==================== 长期任务相关 ====================
async def task_executor_job(ctx) -> None:
    """arq 定时任务：每分钟执行一次任务轮询"""
    await poll_and_execute_tasks(batch_size=5, lock_timeout_seconds=1800)


async def on_startup(ctx) -> None:
    """Worker 启动时配置日志"""
    setup_logging(log_dir="/logs/ai-service", log_file="arq-worker.log")
    logger.info("arq-worker started, file logging enabled")


class UnifiedWorkerSettings:
    """
    统一的 Worker 配置

    启动命令：
        arq app.workers.unified_worker.UnifiedWorkerSettings
    """

    on_startup = on_startup

    redis_settings = RedisSettings(
        host=settings.redis_host or "localhost",
        port=6379,
        password=settings.redis_password,
        database=0,
    )

    # 所有任务函数
    functions = []

    # 所有定时任务
    cron_jobs = [
        # 1. 长期任务：每分钟执行一次
        cron(task_executor_job, minute=None),
        # 2. 向量化 pending 消息扫描：每 10 分钟一次
        cron(cron_scan_pending_messages, minute={0, 10, 20, 30, 40, 50}),
        # 3. 记忆画像沉淀：每 4 小时一次
        cron(
            cron_consolidate_profiles,
            hour={2, 6, 10, 14, 18, 22},
            minute={15},
        ),
    ]
