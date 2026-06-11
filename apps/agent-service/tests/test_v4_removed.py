"""v4 记忆整机 + afterthought 触发链已物理删除 — 负向断言.

旧 v4 自转机器（afterthought 碎片生成 / light+heavy reviewer cron /
recall+commit_abstract 工具 / fragment+abstract 向量化 / qdrant 基建）
全部删除。本文件断言模块不存在 + 源码零残留，防止任何形式的复活。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"

REMOVED_MODULES = [
    # afterthought 触发链
    "app.domain.memory_triggers",
    "app.wiring.memory_triggers",
    "app.nodes.memory_pipelines",
    # reviewer 全家
    "app.memory.reviewer",
    "app.memory.reviewer.light",
    "app.memory.reviewer.heavy",
    "app.memory.reviewer.tools",
    "app.nodes.life_dataflow",
    "app.domain.life_dataflow",
    # recall / abstract / notes 工具链
    "app.agent.tools.recall",
    "app.agent.tools.commit_abstract",
    "app.agent.tools.notes",
    "app.memory.recall_engine",
    "app.memory.conflict",
    "app.memory.notes_format",
    "app.memory._timeline",
    "app.domain.agent_tool_events",
    "app.wiring.agent_tool_events",
    # v4 向量化
    "app.wiring.memory_vectorize",
    "app.nodes.memory_vectorize",
    "app.memory.vectorize_memory",
    "app.domain.memory_request",
    # qdrant 基建（全仓零活调用方 → app 层一并删）
    "app.infra.qdrant",
    "app.capabilities.vector_store",
    "app.capabilities.embed",
    "app.agent.embedding",
    "app.nodes._ids",
    # v4 短 id 生成器（仅 v4 表使用）
    "app.data.ids",
    # v4 queries
    "app.data.queries.memory",
    "app.data.queries.memory_edges",
    "app.data.queries.memory_search",
]


def _spec_exists(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except ModuleNotFoundError:
        # 父包都没了（例如 app.memory.reviewer.* 的父包）→ 同样视为已删除
        return False


@pytest.mark.parametrize("module", REMOVED_MODULES)
def test_removed_module_does_not_exist(module):
    assert not _spec_exists(module), f"{module} 应已随 v4 记忆整机删除"


# 源码零残留：这些 token 不允许出现在 app/ 下任何 .py 文件里
# （本测试文件自身是负向断言，允许出现）。
FORBIDDEN_TOKENS = [
    # afterthought 触发链强指纹
    "AfterthoughtTrigger",
    # reviewer 强指纹（旧模块路径前缀保留在 REMOVED_MODULES；这里只扫类/函数名）
    "run_light_review",
    "run_heavy_review",
    # recall / commit_abstract 强指纹
    "recall_engine",
    "run_recall",
    "commit_abstract",
    # v4 请求对象强指纹
    "MemoryFragmentRequest",
    "MemoryAbstractRequest",
    # v4 队列名强指纹
    "memory_fragment_vectorize",
    "memory_abstract_vectorize",
]


@pytest.mark.parametrize("token", FORBIDDEN_TOKENS)
def test_no_token_residue_in_app_source(token):
    hits: list[str] = []
    for py in sorted(APP_DIR.rglob("*.py")):
        text = py.read_text(encoding="utf-8")
        if token in text:
            hits.append(str(py.relative_to(APP_DIR.parent)))
    assert hits == [], f"token {token!r} 残留于: {hits}"


def test_v4_models_removed():
    """Fragment / AbstractMemory / MemoryEdge / Note / MemoryEntity /
    ScheduleRevision 都是 SQLAlchemy Base（create_all 语义，删 model 不动库表），
    随 v4 一起删。"""
    import app.data.models as models

    for name in (
        "Fragment",
        "AbstractMemory",
        "MemoryEdge",
        "Note",
        "MemoryEntity",
        "ScheduleRevision",
    ):
        assert not hasattr(models, name), f"models.{name} 应已删除"


def test_mq_routes_no_v4_vectorize():
    from app.infra.rabbitmq import ALL_ROUTES, KNOWN_APPS_FOR_DELAYED_TRIGGER

    queues = {r.queue for r in ALL_ROUTES}
    assert "memory_fragment_vectorize" not in queues
    assert "memory_abstract_vectorize" not in queues
    # vectorize-worker 已无任何节点，runtime_delayed_trigger 队列不再声明
    assert KNOWN_APPS_FOR_DELAYED_TRIGGER == ["agent-service"]


def test_wiring_package_drops_v4_modules():
    import app.wiring as wiring

    for name in ("memory_triggers", "memory_vectorize", "agent_tool_events"):
        assert not hasattr(wiring, name), f"app.wiring.{name} 应已删除"


def test_queries_package_drops_v4_domains():
    from app.data import queries

    for fn in (
        "insert_fragment",
        "get_fragment_by_id",
        "insert_abstract_memory",
        "insert_memory_edge",
        "upsert_note",
        "list_active_notes",
        "select_notes_for_context",
        "list_fragments_window",
        "list_abstracts_window",
        "get_abstracts_by_subject",
        "find_messages_in_range",
        "find_group_name",
    ):
        assert not hasattr(queries, fn), f"queries.{fn} 应已删除"
