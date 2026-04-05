"""实体解析器 — 飞书长 ID → MemoryEntity 短自增 ID 映射

碎片内容用 `名字(#id)` 格式引用实体，例如：
    "今天和阿儒(#3)在番剧群(#7)聊了新番"
"""

from sqlalchemy.future import select

from app.orm.base import AsyncSessionLocal
from app.orm.crud import get_username
from app.orm.memory_crud import batch_get_or_create_entities, get_or_create_entity
from app.orm.memory_models import MemoryEntity
from app.orm.models import LarkGroupChatInfo


async def _get_chat_display_name(chat_id: str, chat_type: str) -> str | None:
    """从 LarkGroupChatInfo 读取群名；p2p 无群名返回 None"""
    if chat_type == "p2p":
        return None
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(LarkGroupChatInfo.name).where(LarkGroupChatInfo.chat_id == chat_id)
        )
        return result.scalar_one_or_none()


async def resolve_participants(user_ids: list[str]) -> dict[str, MemoryEntity]:
    """解析用户 ID 列表，从 lark_user 获取展示名

    Args:
        user_ids: 飞书 union_id 列表

    Returns:
        {union_id: MemoryEntity}
    """
    if not user_ids:
        return {}

    items: list[tuple[str, str, str | None]] = []
    for uid in user_ids:
        display_name = await get_username(uid)
        items.append(("user", uid, display_name))

    return await batch_get_or_create_entities(items)


async def resolve_chat(chat_id: str, chat_type: str) -> MemoryEntity:
    """解析聊天 ID，从 LarkGroupChatInfo 获取群名（p2p 无名）

    Args:
        chat_id: 飞书 chat_id
        chat_type: "group" 或 "p2p"

    Returns:
        对应的 MemoryEntity
    """
    entity_type = "p2p" if chat_type == "p2p" else "group"
    display_name = await _get_chat_display_name(chat_id, chat_type)
    return await get_or_create_entity(entity_type, chat_id, display_name)


def format_entity_ref(entity: MemoryEntity) -> str:
    """格式化实体引用，用于碎片内容中

    Returns:
        "名字(#id)" 若有 display_name，否则 "#id"
    """
    if entity.display_name:
        return f"{entity.display_name}(#{entity.id})"
    return f"#{entity.id}"


async def build_entity_context(
    user_ids: list[str],
    chat_id: str,
    chat_type: str,
) -> tuple[dict[str, str], list[int]]:
    """构建碎片创建所需的实体上下文

    Args:
        user_ids: 参与者 union_id 列表
        chat_id: 聊天 ID
        chat_type: "group" 或 "p2p"

    Returns:
        (name_map, mentioned_ids)
        - name_map: {union_id: "名字(#id)"} 供 prompt 插值
        - mentioned_ids: [entity.id, ...] 供 mentioned_entity_ids 字段存储
    """
    user_entities = await resolve_participants(user_ids)
    chat_entity = await resolve_chat(chat_id, chat_type)

    name_map: dict[str, str] = {
        uid: format_entity_ref(entity) for uid, entity in user_entities.items()
    }
    name_map[chat_id] = format_entity_ref(chat_entity)

    mentioned_ids: list[int] = [chat_entity.id]
    mentioned_ids.extend(entity.id for entity in user_entities.values())

    return name_map, mentioned_ids
