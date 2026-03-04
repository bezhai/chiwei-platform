"""
聊天服务层
处理聊天相关的业务逻辑
"""

import logging
import traceback
from collections.abc import AsyncGenerator

from app.agents import stream_chat
from app.types.chat import (
    ChatNormalResponse,
    ChatProcessResponse,
    ChatRequest,
    ChatStatusResponse,
    Step,
)
from app.utils.decorators import auto_json_serialize

logger = logging.getLogger(__name__)


class ChatService:
    """聊天服务类"""

    @staticmethod
    @auto_json_serialize
    async def process_chat_sse(
        request: ChatRequest,
    ) -> AsyncGenerator[
        ChatNormalResponse | ChatProcessResponse | ChatStatusResponse, None
    ]:
        """
        处理 SSE 聊天流程

        Args:
            request: 聊天请求对象

        Yields:
            ChatNormalResponse | ChatProcessResponse: 聊天响应对象
        """

        try:
            # 1. 接收消息确认
            yield ChatNormalResponse(step=Step.ACCEPT)

            # 3. 开始生成回复
            yield ChatNormalResponse(step=Step.START_REPLY)

            # 4. 生成并发送回复
            last_content = ""  # 用于跟踪最后的内容
            last_reason_content = ""  # 用于跟踪最后的思维链内容

            # 使用原有服务
            async for chunk in stream_chat(
                request.message_id, session_id=request.session_id
            ):
                if chunk.status_message:
                    yield ChatStatusResponse(
                        step=Step.SEND, status_message=chunk.status_message
                    )
                elif chunk.content or chunk.reason_content:
                    last_content = chunk.content  # 保存最后的内容
                    last_reason_content = chunk.reason_content  # 保存最后的思维链内容
                    yield ChatProcessResponse(
                        step=Step.SEND,
                        content=chunk.content,
                        reason_content=chunk.reason_content,
                    )

            # 5. 回复成功，返回完整内容
            yield ChatProcessResponse(
                step=Step.SUCCESS,
                content=last_content,
                reason_content=last_reason_content,
            )

        except Exception as e:
            logger.error(f"SSE 聊天处理失败: {str(e)}\n{traceback.format_exc()}")
            yield ChatNormalResponse(step=Step.FAILED)
        finally:
            # 7. 流程结束
            yield ChatNormalResponse(step=Step.END)


# 创建服务实例
chat_service = ChatService()
