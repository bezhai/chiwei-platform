"""queries package 拆分完整性 + 无重名。

剩 3 个 domain 模块：model_provider / persona / messages（memory / memory_edges /
memory_search 三个 v4 domain 已随旧记忆机器删除；schedule 模块更早删除；life 模块
随 voice 子系统拆除删除；agent_response 随旧 chat 管线删除后零调用方，整个模块
删除）。
"""
from __future__ import annotations

# 硬编码作为期望基线（spec §3.3 + 历次拆除后的现存集合）。
EXPECTED_FUNCTIONS = {
    # model_provider (3)
    "parse_model_id", "find_model_mapping", "find_provider_by_name",
    # persona (4 — find_bot_user_ids_for_persona /
    # find_conversations_with_persona_bot 随 app.living 的数据访问收口新增；
    # list_all_persona_ids / resolve_persona_id / resolve_bot_name_for_persona
    # 随旧 chat 管线失去最后一个调用方，一并删除)
    "find_persona", "find_bot_names_for_persona",
    "find_bot_user_ids_for_persona", "find_conversations_with_persona_bot",
    # messages (9 — 旧 chat 管线那 14 个导出零调用方、整批删除；这 9 个是
    # app.living 读公共层的那批查询收口进来的)
    "find_unread_summary", "find_unread_senders",
    "find_newest_unread_summons", "find_conversation_window",
    "find_messages_known_through", "search_conversations_by_name",
    "find_file_items_in_conversations",
    "find_messages_by_outbound_ids", "find_recall_state_by_outbound_ids",
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
    """3 个 domain 文件的 __all__ 两两交集为空。

    `from X import *` 重名时后者覆盖、不报错；ruff/mypy 也不一定能捕获。
    必须有测试兜底，否则一个 domain 漏写 __all__ 一项可能让 caller 拿到错误
    domain 的同名函数（极端情况下行为一致）。
    """
    from app.data.queries import messages, model_provider, persona

    modules = {
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


def test_queries_covers_every_domain_module():
    """``modules`` 那张表就是 ``app/data/queries/`` 下的全部 domain 模块。

    新加一个 domain 文件却忘了登记，上面那条重名检查会**静默**漏掉它 —— 那正是
    这条检查存在的理由被绕开的样子。所以拿磁盘上的实际文件名对一遍。
    """
    from pathlib import Path

    from app.data import queries

    on_disk = {
        p.stem
        for p in Path(queries.__file__).parent.glob("*.py")
        if p.stem != "__init__"
    }
    assert on_disk == {"messages", "model_provider", "persona"}


def test_queries_each_function_callable():
    """每个 export 都是 callable（防止 __all__ 列了不存在或非 callable 的名字）。"""
    from app.data import queries

    for name in queries.__all__:
        attr = getattr(queries, name, None)
        assert callable(attr), (
            f"queries.{name} is not callable (got {type(attr).__name__})"
        )
