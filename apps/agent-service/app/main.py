import asyncio
import logging
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from inner_shared import hello as shared_hello

from app.api.router import api_router
from app.api.schedule import router as schedule_router
from app.config import settings
from app.services.qdrant import init_qdrant_collections
from app.utils.middlewares import HeaderContextMiddleware

load_dotenv()

logger = logging.getLogger(__name__)


async def _maybe_migrate_bot_chat_presence():
    """bot_chat_presence 为空时，调飞书 API 填充存量数据（一次性）"""
    import httpx
    from app.orm.base import AsyncSessionLocal
    from sqlalchemy import text

    FEISHU_BASE = "https://open.feishu.cn/open-apis"

    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(text("SELECT COUNT(*) FROM bot_chat_presence"))
            count = result.scalar()
            if count and count > 0:
                logger.info("bot_chat_presence already has %d rows, skip migration", count)
                return

        logger.info("bot_chat_presence is empty, starting migration...")

        # 读取所有 active bot 凭据
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("SELECT bot_name, app_id, app_secret FROM bot_config WHERE is_active = true")
            )
            bots = [{"bot_name": r[0], "app_id": r[1], "app_secret": r[2]} for r in result.fetchall()]

        total = 0
        for bot in bots:
            bot_name = bot["bot_name"]
            try:
                # 获取 tenant_access_token
                async with httpx.AsyncClient() as client:
                    resp = await client.post(
                        f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
                        json={"app_id": bot["app_id"], "app_secret": bot["app_secret"]},
                    )
                    token = resp.json().get("tenant_access_token")
                    if not token:
                        logger.warning("[migrate] Failed to get token for %s", bot_name)
                        continue

                # 分页拉取 bot 所在的群
                chats = []
                page_token = ""
                async with httpx.AsyncClient() as client:
                    while True:
                        params = {"page_size": 100}
                        if page_token:
                            params["page_token"] = page_token
                        resp = await client.get(
                            f"{FEISHU_BASE}/im/v1/chats",
                            headers={"Authorization": f"Bearer {token}"},
                            params=params,
                        )
                        data = resp.json()
                        for item in data.get("data", {}).get("items", []):
                            if item.get("chat_status") == "normal" and item.get("chat_mode") != "p2p":
                                chats.append(item["chat_id"])
                        if not data.get("data", {}).get("has_more"):
                            break
                        page_token = data["data"]["page_token"]

                # 写入
                if chats:
                    async with AsyncSessionLocal() as session:
                        for chat_id in chats:
                            await session.execute(
                                text(
                                    "INSERT INTO bot_chat_presence (chat_id, bot_name) "
                                    "VALUES (:cid, :bn) "
                                    "ON CONFLICT (chat_id, bot_name) DO UPDATE "
                                    "SET is_active = true, updated_at = now()"
                                ),
                                {"cid": chat_id, "bn": bot_name},
                            )
                        await session.commit()
                    total += len(chats)
                logger.info("[migrate] %s: %d group chats", bot_name, len(chats))
            except Exception as e:
                logger.error("[migrate] %s failed: %s", bot_name, e)

        logger.info("bot_chat_presence migration done: %d total records", total)
    except Exception as e:
        logger.error("bot_chat_presence migration failed: %s", e, exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    应用生命周期管理
    """
    await init_qdrant_collections()
    logger.info("shared pkg loaded: %s", shared_hello())

    # 加载 Skill 定义
    from pathlib import Path

    from app.skills.registry import SkillRegistry

    skills_dir = Path(__file__).parent / "skills" / "definitions"
    SkillRegistry.load_all(skills_dir)

    # 一次性迁移：bot_chat_presence 为空时自动填充
    asyncio.create_task(_maybe_migrate_bot_chat_presence())

    # 启动 MQ consumers（仅当 RabbitMQ 配置存在时）
    consumer_tasks: list[asyncio.Task] = []
    if settings.rabbitmq_url:
        from app.workers.chat_consumer import start_chat_consumer
        from app.workers.post_consumer import start_post_consumer
        from app.workers.proactive_consumer import start_proactive_consumer

        consumer_tasks.append(asyncio.create_task(start_post_consumer()))
        logger.info("Post safety consumer started")

        consumer_tasks.append(asyncio.create_task(start_chat_consumer()))
        logger.info("Chat request consumer started")

        consumer_tasks.append(asyncio.create_task(start_proactive_consumer()))
        logger.info("Proactive eval consumer started")

    yield

    # 关闭 consumers
    for task in consumer_tasks:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception) as e:
            if not isinstance(e, asyncio.CancelledError):
                logger.warning("Consumer task ended with error: %s", e)
    # 关闭 RabbitMQ 连接
    if settings.rabbitmq_url:
        from app.clients.rabbitmq import RabbitMQClient

        client = RabbitMQClient.get_instance()
        await client.close()


app = FastAPI(lifespan=lifespan)

# 添加 Prometheus metrics 中间件（最外层，记录所有请求）
from app.middleware.metrics import PrometheusMiddleware

app.add_middleware(PrometheusMiddleware)

# 添加TraceId中间件
app.add_middleware(HeaderContextMiddleware)

# 注册API路由
app.include_router(api_router)
app.include_router(schedule_router)
