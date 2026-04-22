"""Tests for exported tool sets."""


def test_main_agent_excludes_history_search_tools():
    from app.agent.tools import ALL_TOOLS
    from app.agent.tools.history import check_chat_history, search_group_history

    assert check_chat_history not in ALL_TOOLS
    assert search_group_history not in ALL_TOOLS


def test_main_agent_keeps_supported_main_tools():
    from app.agent.tools import ALL_TOOLS
    from app.agent.tools.delegation import deep_research
    from app.agent.tools.history import list_group_members
    from app.agent.tools.sandbox import sandbox_bash
    from app.agent.tools.skill import load_skill

    assert list_group_members in ALL_TOOLS
    assert deep_research in ALL_TOOLS
    assert load_skill in ALL_TOOLS
    assert sandbox_bash in ALL_TOOLS
