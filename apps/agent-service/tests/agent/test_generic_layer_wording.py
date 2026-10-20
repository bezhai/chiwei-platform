"""The generic layer says nothing in the participants' words. The fixed loop, the model loop, tool
dispatch and the history's trimming and markers are shared by every app; what a model is told
when a tool call never ran, when a tool result or a picture was trimmed, when the history was
cleaned up or a round was not stored, or when a tool call's arguments do not fit, is the
participant's wording, given by the manifest (``HistoryTexts``) and the agent's declaration
(``AgentSpec.bad_arguments``).

Checked by reading the modules' syntax: no string literal in them holds CJK text, except
docstrings, log lines and exception messages, which are for whoever reads the code or the logs.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

GENERIC = ("core.py", "round.py", "tooling.py", "continuity.py")
AGENT = Path(__file__).resolve().parents[2] / "app" / "agent"
CJK = re.compile(r"[　-〿㐀-鿿＀-￯]")
LOGGING = {"debug", "info", "warning", "error", "exception", "critical", "log"}


def _exempt(tree: ast.AST) -> set[int]:
    """The string nodes that are docstrings, or inside a log call or a ``raise``."""
    exempt: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                exempt.add(id(first.value))
        logged = (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in LOGGING
        )
        if logged or isinstance(node, ast.Raise):
            exempt.update(id(inner) for inner in ast.walk(node))
    return exempt


@pytest.mark.parametrize("module", GENERIC)
def test_the_generic_layer_holds_no_participants_wording(module):
    tree = ast.parse((AGENT / module).read_text(encoding="utf-8"))
    exempt = _exempt(tree)

    worded = [
        f"{module}:{node.lineno}: {node.value[:40]!r}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in exempt
        and CJK.search(node.value)
    ]

    assert worded == []


def test_the_loop_reads_no_request_or_app_registry():
    """The loop does not reach into the HTTP layer for the lane or into the app registry for the
    app's name: both are the process's, read through ``app.infra``."""
    tree = ast.parse((AGENT / "core.py").read_text(encoding="utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }

    assert not {m for m in imported if m.startswith(("app.api", "app.deployment"))}


def _agents_with_tools():
    from app.agent.reading import READING_AGENT
    from app.living.moment import LIFE_MOMENT_AGENT
    from app.world.answer import ANSWER_AGENT
    from app.world.main_agent import ROUND_AGENT
    from app.world.npc import NPC_AGENT
    from app.world.perception import PERCEPTION_AGENT

    return [
        LIFE_MOMENT_AGENT,
        READING_AGENT,
        ROUND_AGENT,
        PERCEPTION_AGENT,
        NPC_AGENT,
        ANSWER_AGENT,
    ]


def test_every_agent_with_tools_says_how_a_tool_call_with_bad_arguments_is_answered():
    """The words a model gets back for arguments that do not fit a tool moved from the dispatch
    into the agents' declarations; every agent that is given tools keeps getting them."""
    from app.agent.tools.outcome import BAD_ARGUMENTS

    assert [spec.bad_arguments for spec in _agents_with_tools()] == [BAD_ARGUMENTS] * 6
