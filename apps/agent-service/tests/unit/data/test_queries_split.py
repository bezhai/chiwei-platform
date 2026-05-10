"""Phase 6 第三刀验收：queries package 拆分完整性 + 无重名。

memory.py 在 spec §3.6 预估超 300 行后细拆为 memory.py / memory_edges.py /
memory_search.py，所以本测试覆盖 9 个 domain 模块。
"""
from __future__ import annotations

# 来自 spec §3.3 + §3.6 memory 细拆 + Phase 7d Task 5 hoist (6 个新增 messages
# query) + Notes redesign 2026-05-10，硬编码作为期望基线（81 函数）。
EXPECTED_FUNCTIONS = {
    # model_provider (3)
    "parse_model_id", "find_model_mapping", "find_provider_by_name",
    # persona (6)
    "find_persona", "list_all_persona_ids", "resolve_persona_id",
    "resolve_bot_name_for_persona", "resolve_mentioned_personas",
    "find_bot_names_for_persona",
    # messages (18 — 12 原始 + 6 Phase 7d Task 5 hoist)
    "find_cross_chat_messages", "find_message_content", "find_messages_in_range",
    "find_username", "find_group_name", "find_group_download_permission",
    "find_message_by_id", "resolve_message_id_by_row_id", "find_last_bot_reply_time",
    "find_context_messages_for_anchors", "find_group_members", "find_gray_config",
    "find_user_messages_after", "find_proactive_messages_in_chat",
    "insert_proactive_message",
    "find_messages_with_user_chat_persona_by_root",
    "find_messages_with_user_chat_persona_in_chat",
    "update_messages_tos_files",
    # agent_response (4)
    "set_agent_response_bot", "is_chat_request_completed",
    "get_safety_status", "set_safety_status",
    # schedule (11)
    "find_active_schedules_for_date", "find_latest_plan", "find_plan_for_period",
    "find_daily_entries", "list_schedules", "upsert_schedule", "delete_schedule",
    "insert_schedule_revision", "get_current_schedule", "get_schedule_revision_by_id",
    "list_recent_schedule_revisions",
    # life (9)
    "find_latest_life_state", "insert_life_state", "find_today_activity_states",
    "find_life_states_in_range", "find_latest_glimpse_state", "insert_glimpse_state",
    "insert_reply_style", "find_latest_reply_style", "list_recent_life_states",
    # memory (13) — fragments + abstracts CRUD
    "get_fragment_by_id", "get_abstract_by_id", "insert_fragment", "touch_fragment",
    "get_fragments_by_ids", "touch_fragments_bulk", "insert_abstract_memory",
    "touch_abstract", "touch_abstracts_bulk", "count_abstracts_by_persona",
    "update_abstract_content_query", "set_clarity", "delete_fragment_query",
    # memory_edges (8) — edges + notes
    "insert_memory_edge", "delete_edge", "list_edges_to", "list_edges_from",
    "upsert_note", "delete_note", "list_active_notes", "resolve_note",
    # memory_search (9) — read helpers
    "list_today_fragments", "find_fragments_since", "list_fragments_window",
    "list_abstracts_window", "get_abstracts_by_subject", "get_abstracts_by_subjects",
    "get_recent_abstract_titles", "count_abstracts_per_subject_prefix",
    "get_recent_fragments_for_injection",
}


def test_queries_all_complete():
    """app.data.queries.__all__ 与 spec §3.3 函数集合完全相等。"""
    from app.data import queries

    actual = set(queries.__all__)
    missing = EXPECTED_FUNCTIONS - actual
    extra = actual - EXPECTED_FUNCTIONS
    assert not missing, f"missing in queries.__all__: {sorted(missing)}"
    assert not extra, f"unexpected in queries.__all__: {sorted(extra)}"


def test_queries_no_duplicate_names():
    """9 个 domain 文件的 __all__ 两两交集为空。

    `from X import *` 重名时后者覆盖、不报错；ruff/mypy 也不一定能捕获。
    必须有测试兜底，否则一个 domain 漏写 __all__ 一项可能让 caller 拿到错误
    domain 的同名函数（极端情况下行为一致）。
    """
    from app.data.queries import (
        agent_response,
        life,
        memory,
        memory_edges,
        memory_search,
        messages,
        model_provider,
        persona,
        schedule,
    )

    modules = {
        "agent_response": agent_response,
        "life": life,
        "memory": memory,
        "memory_edges": memory_edges,
        "memory_search": memory_search,
        "messages": messages,
        "model_provider": model_provider,
        "persona": persona,
        "schedule": schedule,
    }

    seen: dict[str, str] = {}
    duplicates: list[str] = []
    for mod_name, mod in modules.items():
        for name in mod.__all__:
            if name in seen:
                duplicates.append(f"{name}: {seen[name]} & {mod_name}")
            else:
                seen[name] = mod_name
    assert not duplicates, f"duplicate names across domains: {duplicates}"


def test_queries_each_function_callable():
    """每个 export 都是 callable（防止 __all__ 列了不存在或非 callable 的名字）。"""
    from app.data import queries

    for name in queries.__all__:
        attr = getattr(queries, name, None)
        assert callable(attr), (
            f"queries.{name} is not callable (got {type(attr).__name__})"
        )
