"""
API路由汇总
"""

import os

from fastapi import APIRouter

from app.api.chat import router as chat_router
from app.api.extraction import router as extraction_router

# 创建主路由
api_router = APIRouter()

# 注册子路由
api_router.include_router(chat_router, tags=["Chat"])
api_router.include_router(extraction_router, tags=["Extraction"])


# 健康检查路由
@api_router.get("/")
async def root():
    return {"message": "FastAPI is running!"}


# 专用健康检查端点
@api_router.get("/health", tags=["Health"])
async def health_check():
    """
    服务健康检查端点
    """
    # 可在这里添加更多健康检查的逻辑
    return {
        "status": "ok",
        "timestamp": import_time(),
        "service": "agent-service",
        "version": os.environ.get("GIT_SHA", "unknown"),
    }


def import_time():
    """获取当前时间字符串"""
    from datetime import datetime

    return datetime.now().isoformat()
