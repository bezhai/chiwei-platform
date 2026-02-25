import asyncio
import logging
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from inner_shared import hello as shared_hello

from app.api.router import api_router
from app.config import settings
from app.services.qdrant import init_qdrant_collections
from app.utils.middlewares import HeaderContextMiddleware

load_dotenv()

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    应用生命周期管理
    """
    await init_qdrant_collections()
    logger.info("shared pkg loaded: %s", shared_hello())

    # 启动 post safety consumer（仅当 RabbitMQ 配置存在时）
    consumer_task = None
    if settings.rabbitmq_url:
        from app.workers.post_consumer import start_post_consumer

        consumer_task = asyncio.create_task(start_post_consumer())
        logger.info("Post safety consumer started")

    yield

    # 关闭 consumer
    if consumer_task:
        consumer_task.cancel()
        try:
            await consumer_task
        except (asyncio.CancelledError, Exception) as e:
            if not isinstance(e, asyncio.CancelledError):
                logger.warning("Post consumer task ended with error: %s", e)
    # 关闭 RabbitMQ 连接
    if settings.rabbitmq_url:
        from app.clients.rabbitmq import RabbitMQClient

        client = RabbitMQClient.get_instance()
        await client.close()


app = FastAPI(lifespan=lifespan)

# 添加TraceId中间件
app.add_middleware(HeaderContextMiddleware)

# 注册API路由
app.include_router(api_router)
