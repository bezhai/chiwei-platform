import logging
import os
from contextlib import asynccontextmanager

import jieba.analyse
from fastapi import FastAPI

from app.api.extraction import router as extraction_router
from app.api.router import api_router
from app.infrastructure.database import close_database, init_database
from app.infrastructure.lark_client import init_lark_clients
from app.infrastructure.redis_client import close_redis, init_redis

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    jieba.analyse.extract_tags("warmup", topK=1)
    logger.info("jieba warmed up")

    await init_redis()
    init_database()
    await init_lark_clients()

    yield

    # Shutdown
    await close_redis()
    await close_database()


app = FastAPI(title="tool-service", version=os.getenv("GIT_SHA", "dev"), lifespan=lifespan)

app.include_router(api_router, prefix="/api")
app.include_router(extraction_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
