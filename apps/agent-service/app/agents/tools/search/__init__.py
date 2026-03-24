"""搜索工具集"""

from app.agents.tools.search.web import search_web

# 基础搜索工具集合
SEARCH_TOOLS = [
    search_web,
]

__all__ = [
    "search_web",
    "SEARCH_TOOLS",
]
