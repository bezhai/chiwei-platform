"""Fire-and-forget post-processing tasks

流式生成完成后的异步后处理：
- post safety check: 将回复发布到 RabbitMQ 审查队列
- identity drift: 触发身份漂移检测
- afterthought: 触发对话片段回顾
- guard message: 获取 persona 专属拒绝消息
"""

import asyncio
import logging

logger = logging.getLogger(__name__)


async def get_guard_message(persona_or_bot: str) -> str:
    """获取 guard 拒绝消息（persona/bot 专属，fallback 为通用消息）"""
    try:
        from app.orm.crud import get_bot_persona
        persona = await get_bot_persona(persona_or_bot)
        if persona and persona.error_messages:
            return persona.error_messages.get("guard", "不想讨论这个话题呢~")
    except Exception as e:
        logger.warning(f"Failed to get guard message for {persona_or_bot}: {e}")
    return "不想讨论这个话题呢~"


async def publish_post_check(
    session_id: str,
    response_text: str,
    chat_id: str,
    trigger_message_id: str,
) -> None:
    """发布 post safety check 消息到 RabbitMQ"""
    try:
        from app.clients.rabbitmq import SAFETY_CHECK, RabbitMQClient
        from app.utils.middlewares.trace import get_lane

        client = RabbitMQClient.get_instance()
        await client.publish(
            SAFETY_CHECK,
            {
                "session_id": session_id,
                "response_text": response_text,
                "chat_id": chat_id,
                "trigger_message_id": trigger_message_id,
                "lane": get_lane(),
            },
        )
        logger.info(f"Published post safety check: session_id={session_id}")
    except Exception as e:
        logger.error(f"Failed to publish post safety check: {e}")


def schedule_post_actions(
    full_content: str,
    session_id: str | None,
    chat_id: str,
    message_id: str,
    persona_id: str,
) -> None:
    """调度所有 fire-and-forget 后处理任务

    在主流式生成完成后调用，异步触发：
    1. Post safety check (RabbitMQ)
    2. Identity drift detection
    3. Afterthought (conversation fragment review)
    """
    if not full_content:
        return

    # Fire-and-forget: publish to post safety check queue
    if session_id:
        asyncio.create_task(
            publish_post_check(session_id, full_content, chat_id, message_id)
        )

    # Fire-and-forget: trigger identity drift
    try:
        from app.services.identity_drift import IdentityDriftManager

        asyncio.create_task(
            IdentityDriftManager.get_instance().on_event(chat_id, persona_id)
        )
    except Exception as e:
        logger.warning(f"Identity drift trigger failed: {e}")

    # Fire-and-forget: trigger afterthought (conversation fragment)
    try:
        from app.services.afterthought import AfterthoughtManager

        asyncio.create_task(
            AfterthoughtManager.get_instance().on_event(chat_id, persona_id)
        )
    except Exception as e:
        logger.warning(f"Afterthought trigger failed: {e}")
