"""群成员查询工具"""

import logging

from langchain.tools import tool
from langgraph.runtime import get_runtime
from sqlalchemy import select

from app.agents.core.context import AgentContext
from app.orm.base import AsyncSessionLocal
from app.orm.models import LarkGroupMember, LarkUser

logger = logging.getLogger(__name__)


@tool
async def list_group_members(role: str | None = None) -> str:
    """
    列出群成员列表

    Args:
        role: 筛选角色（可选）
            - "owner": 群主
            - "manager": 管理员
            - None: 所有成员
    """
    context = get_runtime(AgentContext).context

    try:
        async with AsyncSessionLocal() as session:
            query = (
                select(LarkGroupMember, LarkUser)
                .join(LarkUser, LarkGroupMember.union_id == LarkUser.union_id)
                .where(
                    LarkGroupMember.chat_id == context.message.chat_id,
                    ~LarkGroupMember.is_leave,
                )
            )

            if role == "owner":
                query = query.where(LarkGroupMember.is_owner)
            elif role == "manager":
                query = query.where(LarkGroupMember.is_manager)

            result = await session.execute(query)
            rows = result.all()

            if not rows:
                return "群内无成员" if not role else f"未找到 {role} 角色的成员"

            # 格式化输出
            lines = [f"群成员列表（共{len(rows)}人）：\n"]
            for member, user in rows:
                role_tag = (
                    " [群主]"
                    if member.is_owner
                    else " [管理员]"
                    if member.is_manager
                    else ""
                )
                lines.append(f"• {user.name}{role_tag}")

            return "\n".join(lines)

    except Exception as e:
        logger.error(f"list_group_members error: {e}", exc_info=True)
        return f"查询失败: {e}"
