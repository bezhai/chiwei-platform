"""Main Agent 工具集

所有工具始终可用，通过 prompt 引导 Agent 根据复杂度调整行为。
"""

from app.agents.tools.history.members import list_group_members
from app.agents.tools.history.search import search_group_history
from app.agents.tools.image import generate_image, read_images
from app.agents.tools.memory import load_memory
from app.agents.tools.search.allcpp import search_donjin_event
from app.agents.tools.search.image import search_images
from app.agents.tools.search.web import search_web

# 所有可用工具
ALL_TOOLS = [
    search_web,
    search_images,
    search_donjin_event,
    search_group_history,
    list_group_members,
    generate_image,
    read_images,
    load_memory,
]
