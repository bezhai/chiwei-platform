"""Main Agent 工具集

所有工具始终可用，通过 prompt 引导 Agent 根据复杂度调整行为。
BASE_TOOLS 是子 Agent 可复用的基础工具集（不含委派工具）。
"""

from app.agents.tools.delegation.research import deep_research
from app.agents.tools.history.members import list_group_members
from app.agents.tools.history.search import search_group_history
from app.agents.tools.image import generate_image, read_images
from app.agents.tools.memory import load_memory
from app.agents.tools.sandbox_bash import sandbox_bash
from app.agents.tools.search.image import search_images
from app.agents.tools.search.web import search_web
from app.agents.tools.skill import load_skill

# 基础工具（子 Agent 默认继承此集合）
# 注：search_donjin_event 已迁移为 skill（donjin_search）
BASE_TOOLS = [
    search_web,
    search_images,
    search_group_history,
    list_group_members,
    generate_image,
    read_images,
    load_memory,
]

# 主 Agent 完整工具集（基础 + 委派 + 技能）
ALL_TOOLS = [
    *BASE_TOOLS,
    deep_research,
    load_skill,
    sandbox_bash,
]
