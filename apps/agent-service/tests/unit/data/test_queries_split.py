"""queries package 拆分完整性 + 无重名。

v4 记忆整机删除后剩 4 个 domain 模块：model_provider / persona / messages /
agent_response（memory / memory_edges / memory_search 三个 v4 domain 已随
旧记忆机器删除；schedule 模块更早删除；life 模块随 voice 子系统拆除删除）。
"""
from __future__ import annotations

# 硬编码作为期望基线（spec §3.3 + 历次拆除后的现存集合）。
EXPECTED_FUNCTIONS = {
    # model_provider (3)
    "parse_model_id", "find_model_mapping", "find_provider_by_name",
    # persona (5)
    "find_persona", "list_all_persona_ids", "resolve_persona_id",
    "resolve_bot_name_for_persona", "find_bot_names_for_persona",
    # messages (13 — find_context_messages_for_anchors 随旧 RAG 管线删除；
    # find_messages_in_range / find_group_name 随 v4 afterthought 删除；
    # find_persona_spoken_chats_in_window 随睡前回顾新增；
    # find_proactive_messages_in_chat / insert_proactive_message 随旧
    # proactive 外部判断器旁路删除；find_recent_chat_messages 随 proactive
    # chat-turn 渲染新增——按 chat_id 捞历史给 life context 构建，不反查源消息)
    "find_cross_chat_messages", "find_message_content",
    "find_username", "find_group_download_permission",
    "find_message_by_id", "find_last_bot_reply_time",
    "find_gray_config",
    "find_user_messages_after",
    "find_recent_chat_messages",
    "find_messages_with_user_chat_persona_by_root",
    "find_messages_with_user_chat_persona_in_chat",
    "find_persona_spoken_chats_in_window",
    "update_messages_tos_files",
    # agent_response (5)
    "create_pending_agent_response", "set_agent_response_bot",
    "is_chat_request_completed", "get_safety_status", "set_safety_status",
}


def test_queries_all_complete():
    """app.data.queries.__all__ 与期望函数集合完全相等。"""
    from app.data import queries

    actual = set(queries.__all__)
    missing = EXPECTED_FUNCTIONS - actual
    extra = actual - EXPECTED_FUNCTIONS
    assert not missing, f"missing in queries.__all__: {sorted(missing)}"
    assert not extra, f"unexpected in queries.__all__: {sorted(extra)}"


def test_queries_no_duplicate_names():
    """4 个 domain 文件的 __all__ 两两交集为空。

    `from X import *` 重名时后者覆盖、不报错；ruff/mypy 也不一定能捕获。
    必须有测试兜底，否则一个 domain 漏写 __all__ 一项可能让 caller 拿到错误
    domain 的同名函数（极端情况下行为一致）。
    """
    from app.data.queries import (
        agent_response,
        messages,
        model_provider,
        persona,
    )

    modules = {
        "agent_response": agent_response,
        "messages": messages,
        "model_provider": model_provider,
        "persona": persona,
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
