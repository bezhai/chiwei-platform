"""工具层

提供 Agent 可调用的工具集合。
"""

from app.agents.tools.decorators import tool_error_handler

# 从各子模块导出工具
from app.agents.tools.history import (
    HISTORY_TOOLS,
    list_group_members,
)
from app.agents.tools.image import generate_image
from app.agents.tools.search import (
    SEARCH_TOOLS,
    search_web,
)

# Main 工具集（包含所有顶层工具）
MAIN_TOOLS = [
    generate_image,
]

__all__ = [
    # Decorators
    "tool_error_handler",
    # Search tools
    "SEARCH_TOOLS",
    "search_web",
    # History tools
    "HISTORY_TOOLS",
    "list_group_members",
    # Image tools
    "generate_image",
    # Main tools
    "MAIN_TOOLS",
]
