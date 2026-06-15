"""Tests for exported tool sets."""


def test_main_agent_keeps_supported_main_tools():
    from app.agent.tools import ALL_TOOLS
    from app.agent.tools.delegation import deep_research
    from app.agent.tools.no_reply import no_reply
    from app.agent.tools.sandbox import sandbox_bash
    from app.agent.tools.skill import load_skill

    assert deep_research in ALL_TOOLS
    assert load_skill in ALL_TOOLS
    assert sandbox_bash in ALL_TOOLS
    assert no_reply in ALL_TOOLS


def test_no_reply_is_main_only_tool():
    from app.agent.tools import ALL_TOOLS, BASE_TOOLS

    assert "no_reply" not in {t.definition.name for t in BASE_TOOLS}
    assert "no_reply" in {t.definition.name for t in ALL_TOOLS}


def test_chat_toolsets_drop_legacy_rag_tools():
    """chat agent 不再读也不再写旧 RAG：recall（读）+ commit_abstract_memory（写）
    都从 BASE_TOOLS / ALL_TOOLS 移除。只删召回不删写入会留一条没人读还在写的废链。"""
    from app.agent.tools import ALL_TOOLS, BASE_TOOLS

    names_base = {t.definition.name for t in BASE_TOOLS}
    names_all = {t.definition.name for t in ALL_TOOLS}

    assert "recall" not in names_base
    assert "recall" not in names_all
    assert "commit_abstract_memory" not in names_base
    assert "commit_abstract_memory" not in names_all


def test_notes_tools_removed_from_toolsets():
    """v4 notes 工具链随整机删除：upsert/list/resolve/delete_note 不再注册。"""
    from app.agent.tools import ALL_TOOLS, BASE_TOOLS

    names = {t.definition.name for t in BASE_TOOLS} | {
        t.definition.name for t in ALL_TOOLS
    }
    for tool_name in ("upsert_note", "list_note", "resolve_note", "delete_note"):
        assert tool_name not in names, f"{tool_name} 应已随 v4 notes 删除"


def test_world_tools_unaffected_by_chat_rag_removal():
    """world 工具集独立、不含两个旧 RAG 工具——删 chat 工具不应波及它。"""
    from app.world.tools import WORLD_TOOLS

    names = {t.definition.name for t in WORLD_TOOLS}
    assert "recall" not in names
    assert "commit_abstract_memory" not in names


def test_life_tools_unaffected_by_chat_rag_removal():
    """life 工具集独立、不含两个旧 RAG 工具——删 chat 工具不应波及它。"""
    from app.nodes.life_tools import build_life_tools

    names = {
        t.definition.name
        for t in build_life_tools(
            lane="prod",
            persona_id="chiwei",
            act_id="00000000-0000-0000-0000-000000000000",
            observed_at="2026-01-01T00:00:00+08:00",
        )
    }
    assert "recall" not in names
    assert "commit_abstract_memory" not in names
