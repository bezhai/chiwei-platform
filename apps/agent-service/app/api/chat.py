"""
聊天相关API路由
"""

import logging

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

from app.services.chat_service import chat_service
from app.types.chat import (
    ChatRequest,
)
from app.utils.decorators import handle_errors

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/chat/sse")
@handle_errors()
async def chat_sse(request: ChatRequest):
    """
    SSE 聊天接口，接收消息并通过服务端推送事件返回处理步骤。

    Args:
        request: 聊天请求对象

    Returns:
        EventSourceResponse: SSE 响应流
    """

    return EventSourceResponse(
        chat_service.process_chat_sse(request), media_type="text/event-stream"
    )
