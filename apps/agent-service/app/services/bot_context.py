"""Bot 上下文容器 — per-(chat_id, bot_name) 的所有上下文数据"""
import logging
from typing import TYPE_CHECKING

from langchain_core.messages import AIMessage, HumanMessage

from app.orm.crud import get_bot_persona


async def get_reply_style(persona_id: str, default_style: str) -> str:
    """转发到 memory_context.get_reply_style（lazy import 避免循环）"""
    from app.services.memory_context import get_reply_style as _impl
    return await _impl(persona_id, default_style)


if TYPE_CHECKING:
    from app.orm.models import BotPersona
    from app.services.quick_search import QuickSearchResult

logger = logging.getLogger(__name__)


async def _resolve_persona_id(bot_name: str) -> str:
    """从 bot_config 表查 persona_id，找不到则用 bot_name 自身"""
    from app.orm.base import AsyncSessionLocal
    from sqlalchemy import text

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("SELECT persona_id FROM bot_config WHERE bot_name = :bn"),
            {"bn": bot_name},
        )
        row = result.scalar_one_or_none()
        return row if row else bot_name


async def _resolve_bot_name_for_persona(persona_id: str, chat_id: str = "") -> str:
    """从 persona_id + chat_id 精确查找应该用哪个 bot 发消息

    查 bot_chat_presence JOIN bot_config，精确匹配群内的 bot。
    查不到则打告警日志并返回 persona_id（让调用方自行处理）。
    """
    from app.orm.base import AsyncSessionLocal
    from sqlalchemy import text

    if not chat_id:
        logger.warning(
            "[resolve_bot] chat_id 为空，无法精确匹配 bot: persona_id=%s",
            persona_id,
        )
        return persona_id

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT bc.bot_name FROM bot_config bc "
                "JOIN bot_chat_presence bp ON bc.bot_name = bp.bot_name "
                "WHERE bp.chat_id = :cid AND bp.is_active = true "
                "AND bc.persona_id = :pid AND bc.is_active = true "
                "LIMIT 1"
            ),
            {"cid": chat_id, "pid": persona_id},
        )
        row = result.scalar_one_or_none()
        if row:
            return row

    logger.error(
        "[resolve_bot] bot_chat_presence 未命中: persona_id=%s, chat_id=%s — "
        "请检查 bot_chat_presence 表是否有数据，或该 bot 是否在群里",
        persona_id,
        chat_id,
    )
    return persona_id


class BotContext:
    def __init__(self, chat_id: str, bot_name: str, chat_type: str) -> None:
        self.chat_id = chat_id
        self.bot_name = bot_name
        self.chat_type = chat_type
        self._persona_id: str = ""
        self._persona: "BotPersona | None" = None
        self._reply_style: str = ""
        self._inner_monologue: str = ""

    @property
    def persona_id(self) -> str:
        return self._persona_id

    @classmethod
    async def from_persona_id(
        cls, chat_id: str, persona_id: str, chat_type: str
    ) -> "BotContext":
        """从 persona_id 创建 BotContext（多 bot 路由场景）"""
        bot_name = await _resolve_bot_name_for_persona(persona_id, chat_id)
        ctx = cls(chat_id=chat_id, bot_name=bot_name, chat_type=chat_type)
        ctx._persona_id = persona_id
        await ctx._load_persona()
        return ctx

    async def load(self) -> None:
        """并行加载所有 per-bot 数据（从 bot_name 入口）"""
        self._persona_id = await _resolve_persona_id(self.bot_name)
        await self._load_persona()

    async def _load_persona(self) -> None:
        """加载 persona 数据和 reply_style"""
        self._persona = await get_bot_persona(self._persona_id)
        if self._persona is None:
            logger.warning(
                f"BotPersona not found for persona_id={self._persona_id} "
                f"(bot_name={self.bot_name}), using defaults"
            )

        default_style = self._persona.default_reply_style if self._persona else ""
        self._reply_style = await get_reply_style(
            self._persona_id, default_style
        )

        # 加载内心独白（替代 reply_style 的示例锚点）
        from app.orm.memory_crud import get_latest_inner_monologue
        self._inner_monologue = await get_latest_inner_monologue(self._persona_id) or ""

    @property
    def reply_style(self) -> str:
        return self._reply_style

    @property
    def inner_monologue(self) -> str:
        return self._inner_monologue

    def get_identity(self) -> str:
        """返回注入 {{identity}} 的人设文本"""
        return self._persona.persona_lite if self._persona else ""

    def get_appearance_detail(self) -> str:
        """返回画图专用外貌描述"""
        return self._persona.appearance_detail if self._persona and self._persona.appearance_detail else ""

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
            # 优先用 persona_id 判断，fallback 到 bot_name（兼容旧数据）
            msg_persona_id = getattr(msg, "persona_id", None)
            if msg_persona_id:
                is_self = msg.role == "assistant" and msg_persona_id == self._persona_id
            else:
                is_self = msg.role == "assistant" and getattr(msg, "bot_name", None) == self.bot_name
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
