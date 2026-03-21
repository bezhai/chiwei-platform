"""
统一的 ARQ Worker 配置
整合了长期任务（long_tasks）的所有worker功能

启动命令：
    arq app.workers.unified_worker.UnifiedWorkerSettings

夜间处理链时序（CST）：
    01:00  DiaryEntry per-chat + PersonImpression + ChatImpression
    02:00  AkaoJournal(daily) 合成
    03:00  AkaoSchedule(daily, tomorrow) 生成
    周一 02:30  WeeklyReview per-chat
    周一 02:45  AkaoJournal(weekly) 合成
    周日 23:00  AkaoSchedule(weekly)
    每月1号 02:00  AkaoSchedule(monthly)
"""

import logging

from arq import cron
from arq.connections import RedisSettings
from inner_shared.logger import setup_logging

from app.config.config import settings
from app.long_tasks.executor import poll_and_execute_tasks
from app.workers.diary_worker import cron_generate_diaries, cron_generate_weekly_reviews
from app.workers.journal_worker import cron_generate_journal, cron_generate_weekly_journal
from app.workers.schedule_worker import (
    cron_generate_daily_plan,
    cron_generate_monthly_plan,
    cron_generate_weekly_plan,
)
from app.workers.vectorize_worker import cron_scan_pending_messages

logger = logging.getLogger(__name__)


# ==================== 长期任务相关 ====================
async def task_executor_job(ctx) -> None:
    """arq 定时任务：每分钟执行一次任务轮询"""
    await poll_and_execute_tasks(batch_size=5, lock_timeout_seconds=1800)


async def on_startup(ctx) -> None:
    """Worker 启动时配置日志"""
    setup_logging(log_dir="/logs/agent-service", log_file="arq-worker.log")
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

    # 所有定时任务（时间为 CST，服务器时区 UTC+8）
    cron_jobs = [
        # 1. 长期任务：每分钟执行一次
        cron(task_executor_job, minute=None),
        # 2. 向量化 pending 消息扫描：每 10 分钟一次
        cron(cron_scan_pending_messages, minute={0, 10, 20, 30, 40, 50}),
        # === 夜间处理链 ===
        # 3. 日记生成 + 人物印象 + 群氛围印象：CST 01:00
        cron(cron_generate_diaries, hour={1}, minute={0}),
        # 4. 个人日志合成：CST 02:00（日记之后 1h）
        cron(cron_generate_journal, hour={2}, minute={0}),
        # 5. 日计划生成：CST 03:00（日志之后 1h）
        cron(cron_generate_daily_plan, hour={3}, minute={0}),
        # 6. 周记生成（per-chat）：每周一 CST 02:30
        cron(cron_generate_weekly_reviews, weekday={0}, hour={2}, minute={30}),
        # 7. 周日志合成：每周一 CST 02:45
        cron(cron_generate_weekly_journal, weekday={0}, hour={2}, minute={45}),
        # 8. 周计划：每周日 CST 23:00
        cron(cron_generate_weekly_plan, weekday={6}, hour={23}, minute={0}),
        # 9. 月计划：每月1号 CST 02:00
        cron(cron_generate_monthly_plan, day={1}, hour={2}, minute={0}),
    ]
