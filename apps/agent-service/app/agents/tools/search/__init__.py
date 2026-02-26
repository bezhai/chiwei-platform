"""搜索工具集"""

from app.agents.tools.search.allcpp import search_donjin_event
from app.agents.tools.search.bangumi import (
    get_character_persons,
    get_character_subjects,
    get_person_characters,
    get_person_subjects,
    get_subject_characters,
    get_subject_persons,
    get_subject_relations,
    search_characters,
    search_persons,
    search_subjects,
)
from app.agents.tools.search.web import search_web

# 基础搜索工具集合
SEARCH_TOOLS = [
    search_web,
    search_donjin_event,
]

# Bangumi 工具集合
BANGUMI_TOOLS = [
    search_subjects,
    search_characters,
    search_persons,
    get_subject_characters,
    get_subject_persons,
    get_subject_relations,
    get_character_subjects,
    get_character_persons,
    get_person_characters,
    get_person_subjects,
]

__all__ = [
    # Web 搜索
    "search_web",
    # 同人展搜索
    "search_donjin_event",
    # Bangumi 搜索工具
    "search_subjects",
    "search_characters",
    "search_persons",
    "get_subject_characters",
    "get_subject_persons",
    "get_subject_relations",
    "get_character_subjects",
    "get_character_persons",
    "get_person_characters",
    "get_person_subjects",
    # 工具集合
    "SEARCH_TOOLS",
    "BANGUMI_TOOLS",
]
