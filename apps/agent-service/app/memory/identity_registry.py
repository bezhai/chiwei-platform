"""认主人:按 ``common_user_id`` 查可信关系标签,只认主人。

这是 prompt 里**唯一**的身份权威。``"owner"`` 标签只来自 DB 里
``common_user.is_owner = true`` 的那些 ``common_user_id``,不取决于用户可改的显示名,
也不从对话正文推断。这次只防"别人改名冒充主人",不做姐妹概念。

fail-closed:拿不到 ``common_user_id`` / 查询异常(含 ``is_owner`` 列还没加)
一律返回 ``None``(无关系标签)。调用方**绝不**回退到拿显示名当身份——
回退就把"改名叫原智鸿冒充主人"原样放回来。

owner 集合进程内缓存 + lazy load:首次调用 load 一次,成功才缓存;load 失败返回空集合
且**不写缓存**(下次可重试)。一人可能有多个 ``common_user_id``(同一 union_id 在不同
lark bot 下 per-app 分裂出多条 common_user),只要任一被标 ``is_owner`` 就认得出。
"""

from __future__ import annotations

import logging

from sqlalchemy.future import select

from app.data.models import CommonUser
from app.runtime.db import auto_tx, current_session

logger = logging.getLogger(__name__)

# 进程内缓存:``None`` = 还没 load 过;``set[str]`` = load 成功后的 owner id 集合。
# 测试 monkeypatch 这个符号注入 fake 集合 / 验证缓存只 load 一次。
_OWNER_IDS: set[str] | None = None


async def _load_owner_ids() -> set[str]:
    """从 DB 读所有 ``is_owner=true`` 的 ``common_user_id``,只查必要列。

    返回 str 化的 UUID 集合。``is_owner`` 列还没加 / 查询异常会向上抛,由
    ``_owner_ids`` fail-closed 接住。
    """
    async with auto_tx():
        result = await current_session().execute(
            select(CommonUser.common_user_id).where(CommonUser.is_owner.is_(True))
        )
        return {str(cid) for cid in result.scalars().all()}


async def _owner_ids() -> set[str]:
    """拿 owner id 集合:命中缓存直接返,否则 load 一次。

    只有 load 到**非空**集合才写缓存;load 成功但为空(还没人被打标 is_owner)或失败
    (含"列不存在")都返回空集合且不污染缓存,下次调用还能重试(修复 4)。否则 is_owner
    列已加但人工 UPDATE 打标晚于首次请求时,空集合会被永久缓存 → 主人到进程重启前一直
    认不出。
    """
    global _OWNER_IDS
    if _OWNER_IDS is not None:
        return _OWNER_IDS
    try:
        ids = await _load_owner_ids()
    except Exception:
        logger.exception("identity_registry: load owner ids failed (fail-closed)")
        return set()
    if not ids:
        # 空集合不缓存:下次可重试(打标晚于首次请求时仍认得出),与异常同处理。
        return set()
    _OWNER_IDS = ids
    return ids


async def get_relation(common_user_id: str | None) -> str | None:
    """``common_user_id`` 的可信关系标签:命中主人 → ``"owner"``,否则 ``None``。

    fail-closed:缺 id、查询异常一律 ``None``,绝不回退显示名。
    """
    if not common_user_id:
        return None
    try:
        return "owner" if common_user_id in await _owner_ids() else None
    except Exception:
        logger.exception("identity_registry: get_relation failed (fail-closed)")
        return None
