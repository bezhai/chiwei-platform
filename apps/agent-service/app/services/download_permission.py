"""群聊下载权限检查（带内存缓存）"""

import logging
import time

from sqlalchemy.future import select

from app.orm.base import AsyncSessionLocal
from app.orm.models import LarkGroupChatInfo

logger = logging.getLogger(__name__)

# 下载权限缓存: chat_id -> (allows_download, expire_time)
_download_permission_cache: dict[str, tuple[bool, float]] = {}
_PERMISSION_CACHE_TTL = 600  # 10 分钟


async def check_group_allows_download(chat_id: str, chat_type: str) -> bool:
    """检查群聊是否允许下载资源（带缓存）

    - P2P 直接返回 True
    - group 类型查 DB，download_has_permission_setting != 'not_anyone' 时允许
    - DB 查询失败时 fail-open（返回 True）
    """
    if chat_type == "p2p":
        return True

    now = time.monotonic()
    cached = _download_permission_cache.get(chat_id)
    if cached and cached[1] > now:
        return cached[0]

    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(LarkGroupChatInfo.download_has_permission_setting).where(
                    LarkGroupChatInfo.chat_id == chat_id
                )
            )
            row = result.scalar_one_or_none()
            # 无记录或字段为空 → 默认允许；仅 'not_anyone' 时禁止
            allows = row != "not_anyone"
    except Exception:
        logger.warning(f"查询群 {chat_id} 下载权限失败，默认允许")
        allows = True

    _download_permission_cache[chat_id] = (allows, now + _PERMISSION_CACHE_TTL)
    return allows
