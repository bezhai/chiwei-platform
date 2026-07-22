"""Semantic checks for Python-specific dataflow governance rules.

Text grep remains appropriate for banned imports and fixed repository assets.
Call-site rules need Python syntax, otherwise comments/docstrings are counted and
unrelated additions can cancel reviewed removals in a repository-wide total.
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

BUSINESS_ROOTS = (
    Path("apps/agent-service/app/nodes"),
    Path("apps/agent-service/app/agent"),
    Path("apps/agent-service/app/chat"),
    Path("apps/agent-service/app/life"),
    Path("apps/agent-service/app/memory"),
    Path("apps/agent-service/app/skills"),
)
PROVIDER_ADAPTER_ROOT = Path("apps/agent-service/app/agent/adapters")

MANUAL_EMIT_ROSTER = Counter(
    {
        ("apps/agent-service/app/chat/context.py", "build_human_chat_context"): 1,
        ("apps/agent-service/app/chat/post_actions.py", "_publish_post_check"): 1,
        ("apps/agent-service/app/nodes/chat_node.py", "route_chat_node"): 1,
        ("apps/agent-service/app/nodes/chat_node.py", "chat_node"): 3,
        (
            "apps/agent-service/app/nodes/chat_node.py",
            "chat_node._emit_block_guard",
        ): 1,
        (
            "apps/agent-service/app/nodes/life_tools.py",
            "build_life_tools.send_message._render_and_emit_proactive",
        ): 1,
        (
            "apps/agent-service/app/nodes/life_tools.py",
            "build_life_tools.read_book",
        ): 1,
    }
)


def business_python_files() -> Iterator[Path]:
    for root in BUSINESS_ROOTS:
        for path in root.rglob("*.py"):
            if path.name.startswith("test_") or "__pycache__" in path.parts:
                continue
            if PROVIDER_ADAPTER_ROOT in path.parents:
                continue
            yield path


def call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def check_gap_7() -> int:
    violations: list[str] = []
    for path in business_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and call_name(node) == "insert_idempotent":
                violations.append(f"{path}:{node.lineno}")

    if not violations:
        return 0
    print(
        f"Gap 7 violation: {len(violations)} business insert_idempotent call sites",
        file=sys.stderr,
    )
    print("\n".join(violations), file=sys.stderr)
    return 1


class EmitVisitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.functions: list[str] = []
        self.calls: Counter[tuple[str, str]] = Counter()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.functions.append(node.name)
        self.generic_visit(node)
        self.functions.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Await(self, node: ast.Await) -> None:
        if isinstance(node.value, ast.Call) and call_name(node.value) == "emit":
            self.calls[(str(self.path), ".".join(self.functions))] += 1
        self.generic_visit(node)


def check_gap_8() -> int:
    actual: Counter[tuple[str, str]] = Counter()
    for path in business_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = EmitVisitor(path)
        visitor.visit(tree)
        actual.update(visitor.calls)

    if actual == MANUAL_EMIT_ROSTER:
        print(f"Gap 8 reviewed manual emit roster: {sum(actual.values())}")
        return 0

    print("Gap 8 violation: business await emit roster changed", file=sys.stderr)
    for path, function in sorted((actual - MANUAL_EMIT_ROSTER).elements()):
        print(f"  unexpected: {path}::{function}", file=sys.stderr)
    for path, function in sorted((MANUAL_EMIT_ROSTER - actual).elements()):
        print(f"  missing: {path}::{function}", file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("gap", choices=("7", "8"))
    args = parser.parse_args()
    return check_gap_7() if args.gap == "7" else check_gap_8()


if __name__ == "__main__":
    raise SystemExit(main())
