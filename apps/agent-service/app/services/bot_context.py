"""Bot 上下文容器 — per-(chat_id, bot_name) 的所有上下文数据"""
import logging
from typing import TYPE_CHECKING

from langchain_core.messages import AIMessage, HumanMessage

if TYPE_CHECKING:
    from app.orm.models import BotPersona
    from app.services.quick_search import QuickSearchResult

logger = logging.getLogger(__name__)


async def _resolve_persona_id(bot_name: str) -> str:
    """从 bot_config 表查 persona_id，找不到或列不存在则 fallback bot_name"""
    from app.orm.base import AsyncSessionLocal
    from sqlalchemy import text

    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("SELECT persona_id FROM bot_config WHERE bot_name = :bn"),
                {"bn": bot_name},
            )
            row = result.scalar_one_or_none()
            return row if row else bot_name
    except Exception:
        logger.debug("resolve_persona_id fallback: bot_config.persona_id not available")
        return bot_name


class BotContext:
    def __init__(self, chat_id: str, bot_name: str, chat_type: str) -> None:
        self.chat_id = chat_id
        self.bot_name = bot_name
        self.chat_type = chat_type
        self._persona_id: str = ""
        self._persona: "BotPersona | None" = None
        self._reply_style: str = ""

    @property
    def persona_id(self) -> str:
        return self._persona_id

    async def load(self) -> None:
        """并行加载所有 per-bot 数据"""
        import asyncio
        from app.orm.crud import get_bot_persona
        from app.services.memory_context import get_reply_style

        self._persona_id = await _resolve_persona_id(self.bot_name)

        self._persona = await get_bot_persona(self._persona_id)
        if self._persona is None:
            logger.warning(
                f"BotPersona not found for persona_id={self._persona_id} "
                f"(bot_name={self.bot_name}), using defaults"
            )

        default_style = self._persona.default_reply_style if self._persona else ""
        self._reply_style = await get_reply_style(
            self.chat_id, self._persona_id, default_style
        )

    @property
    def reply_style(self) -> str:
        return self._reply_style

    def get_identity(self) -> str:
        """返回注入 {{identity}} 的人设文本"""
        return self._persona.persona_lite if self._persona else ""

    def get_display_name(self) -> str:
        return self._persona.display_name if self._persona else self.bot_name

    def get_error_message(self, kind: str) -> str:
        """返回 bot 专属错误消息"""
        name = self.get_display_name()
        if self._persona and self._persona.error_messages:
            return self._persona.error_messages.get(kind, f"{name}遇到了问题QAQ")
        return f"{name}遇到了问题QAQ"

    def build_chat_history(
        self, messages: "list[QuickSearchResult]"
    ) -> list[AIMessage | HumanMessage]:
        """构建 LLM 对话历史：当前 bot → AIMessage，其余 → HumanMessage（带名字前缀）"""
        result: list[AIMessage | HumanMessage] = []
        for msg in messages:
            # 判断是否为当前 bot 的发言
            is_self = (msg.role == "assistant" and getattr(msg, "bot_name", None) == self.bot_name)
            if is_self:
                result.append(AIMessage(content=msg.content))
            else:
                # 人类用户或其他 bot：统一作为 HumanMessage，带发言者名字
                if msg.username:
                    content = f"{msg.username}: {msg.content}"
                else:
                    content = msg.content
                result.append(HumanMessage(content=content))
        return result
