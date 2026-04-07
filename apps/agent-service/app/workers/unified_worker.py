"""
统一的 ARQ Worker 配置
整合了长期任务（long_tasks）的所有worker功能

启动命令：
    arq app.workers.unified_worker.UnifiedWorkerSettings
"""

import asyncio
import logging

from arq import cron
from arq.connections import RedisSettings
from inner_shared.logger import setup_logging

from app.config.config import settings
from app.long_tasks.executor import poll_and_execute_tasks
from app.workers.dream_worker import cron_generate_dreams, cron_generate_weekly_dreams
from app.workers.schedule_worker import (
    cron_generate_daily_plan,
    cron_generate_monthly_plan,
    cron_generate_weekly_plan,
)
from app.workers.base_style_worker import cron_generate_base_reply_style
from app.workers.proactive_scanner import run_proactive_scan
from app.workers.vectorize_worker import cron_scan_pending_messages
from app.workers.life_engine_worker import cron_life_engine_tick

logger = logging.getLogger(__name__)


async def proactive_scan_job(ctx) -> None:
    """主动搭话扫描（cron 兜底，主触发走消息事件）"""
    import random
    if random.random() > 0.3:
        return
    from app.orm.crud import get_all_persona_ids
    from app.workers.proactive_manager import TARGET_CHAT_IDS
    for chat_id in TARGET_CHAT_IDS:
        for persona_id in await get_all_persona_ids():
            await run_proactive_scan(chat_id, persona_id, source="cron")


# ==================== 长期任务相关 ====================
async def task_executor_job(ctx) -> None:
    """arq 定时任务：每分钟执行一次任务轮询"""
    await poll_and_execute_tasks(batch_size=5, lock_timeout_seconds=1800)


async def on_startup(ctx) -> None:
    """Worker 启动时配置日志 + 初始化 MQ + 生成基线 reply_style"""
    setup_logging(log_dir="/logs/agent-service", log_file="arq-worker.log")
    logger.info("arq-worker started, file logging enabled")

    # Life Engine browsing → glimpse → 搭话 需要通过 MQ publish
    from app.clients.rabbitmq import RabbitMQClient
    client = RabbitMQClient.get_instance()
    await client.connect()
    await client.declare_topology()

    asyncio.create_task(cron_generate_base_reply_style(None))


class UnifiedWorkerSettings:
    """
    统一的 Worker 配置

    启动命令：
        arq app.workers.unified_worker.UnifiedWorkerSettings
    """

    on_startup = on_startup

    queue_name = f"arq:queue:{settings.lane}" if settings.lane else "arq:queue"

    redis_settings = RedisSettings(
        host=settings.redis_host or "localhost",
        port=6379,
        password=settings.redis_password,
        database=0,
    )

    # 所有任务函数
    functions = []

    # 所有定时任务（夜间管线时序，CST）
    # 注意：ArQ 默认 job_timeout=300s，对多 persona 的 LLM 管线远远不够，
    # 必须为耗时任务显式设置 timeout（单位：秒）。
    cron_jobs = [
        # 1. 长期任务：每分钟执行一次
        cron(task_executor_job, minute=None),
        # 1b. Life Engine tick：每分钟
        cron(cron_life_engine_tick, minute=None, timeout=120),
        # 2. 向量化 pending 消息扫描：每 10 分钟一次
        cron(cron_scan_pending_messages, minute={0, 10, 20, 30, 40, 50}),
        # 3. v3 做梦（daily）：每天 CST 03:00
        cron(cron_generate_dreams, hour={3}, minute={0}, timeout=3600),
        # 4. v3 做梦（weekly）：每周一 CST 04:00
        cron(cron_generate_weekly_dreams, weekday={0}, hour={4}, minute={0}, timeout=1800),
        # 5. 日程生成：日计划每天 CST 05:00（dream 之后），周计划每周日，月计划每月1号
        #    日计划用 Ideation+Writer+Critic 多 Agent 管线，最耗时
        cron(cron_generate_daily_plan, hour={5}, minute={0}, timeout=3600),
        cron(cron_generate_weekly_plan, weekday={6}, hour={23}, minute={0}, timeout=1800),
        cron(cron_generate_monthly_plan, day={1}, hour={2}, minute={0}, timeout=1800),
        # 6. 基线 reply_style：每天 CST 8:00/14:00/18:00（Schedule 之后）
        cron(cron_generate_base_reply_style, hour={8, 14, 18}, minute={0}, timeout=1800),
        # 7. 主动搭话扫描（cron 兜底）：每 30 分钟，30% 概率执行 [DISABLED]
        # cron(proactive_scan_job, minute={0, 30}),
    ]
