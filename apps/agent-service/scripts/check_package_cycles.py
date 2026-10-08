"""CI 规则：``app/`` 下的包之间不许出现新的环。

``app/`` 下每个子包（``app/runtime/``、``app/data/`` ……）和每个顶层模块（``app/main.py``）
是一个节点；包里任何一个模块 import 了另一个包里的东西，就是一条从前者指向后者的边。认
import 的方式和 ``scripts/check_world_life_imports.py`` 是同一套语法树扫描（函数里面的
import、``importlib.import_module`` / ``__import__`` 的字符串字面量都算，docstring 和注释
不算）。

互相能走到对方的几个包是一组环（强连通分量）。规则：

* 不在 :data:`KNOWN_CYCLES` 里的环一律失败，并列出把这组包连起来的每一条边各一处 import；
* :data:`KNOWN_CYCLES` 里的环不见了（拆开了，或者缩小成别的样子）也失败：名单要跟着改
  成现在的样子。所以名单只能缩，不能悄悄留着一条已经不存在的环。

用法：在 ``apps/agent-service`` 下 ``python3 -m scripts.check_package_cycles [app 目录]``，
默认是 ``app/``。有问题时逐条打印并以 1 退出。只用标准库，CI 上不装依赖直接跑。
"""
from __future__ import annotations

import ast
import sys
from collections.abc import Iterable
from pathlib import Path

from scripts.check_world_life_imports import imported_names, module_name

# 现在还在的环。agent ↔ capabilities ↔ living ↔ memory ↔ skills 由插件化（T5–T7）拆开；
# 拆开一部分就把这里改成剩下的样子。
KNOWN_CYCLES: list[frozenset[str]] = [
    frozenset({"agent", "capabilities", "living", "memory", "skills"}),
]

# 一条边：import 它的那一处，``app/<包>/<文件>.py:<行号>``。
Edges = dict[str, dict[str, list[str]]]


def _nodes(root: Path) -> set[str]:
    """``root``（``app`` 目录）下的节点：含 ``.py`` 的子目录，和顶层的 ``.py`` 模块。"""
    nodes = {p.stem for p in root.glob("*.py") if p.stem != "__init__"}
    nodes |= {d.name for d in root.iterdir() if d.is_dir() and any(d.rglob("*.py"))}
    return nodes


def _node_of(parts: list[str]) -> str | None:
    """模块名各段（``["app", "runtime", "db"]``）属于哪个节点；``app`` 本身不算。"""
    return parts[1] if len(parts) > 1 else None


def package_graph(root: Path) -> Edges:
    """包到包的边，每条边带上造成它的每一处 import。"""
    root = root.resolve()
    nodes = _nodes(root)
    graph: Edges = {n: {} for n in nodes}
    for path in sorted(root.rglob("*.py")):
        parts, is_package = module_name(root, path)
        source = _node_of(parts)
        if source is None:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for lineno, candidates in imported_names(tree, parts, is_package):
            targets = {
                _node_of(c.split("."))
                for c in candidates
                if c.split(".")[0] == root.name
            }
            for target in targets & nodes - {source}:
                site = f"{path.relative_to(root.parent)}:{lineno}"
                graph[source].setdefault(target, []).append(site)
    return graph


def strongly_connected_groups(graph: Edges) -> list[frozenset[str]]:
    """两个及以上的包互相走得到的每一组（Tarjan），按组里排第一的包名排序。"""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    groups: list[frozenset[str]] = []

    def visit(node: str) -> None:
        index[node] = low[node] = len(index)
        stack.append(node)
        on_stack.add(node)
        for target in graph.get(node, {}):
            if target not in index:
                visit(target)
                low[node] = min(low[node], low[target])
            elif target in on_stack:
                low[node] = min(low[node], index[target])
        if low[node] == index[node]:
            group = set()
            while True:
                member = stack.pop()
                on_stack.discard(member)
                group.add(member)
                if member == node:
                    break
            if len(group) > 1:
                groups.append(frozenset(group))

    for node in sorted(graph):
        if node not in index:
            visit(node)
    return sorted(groups, key=lambda g: sorted(g))


def _names(group: Iterable[str]) -> str:
    return "{" + ", ".join(sorted(group)) + "}"


def cycle_problems(root: Path, known: Iterable[frozenset[str]] = KNOWN_CYCLES) -> list[str]:
    """不在名单上的环、和名单上已经不在的环，每条一段说明。"""
    known = [frozenset(k) for k in known]
    graph = package_graph(root)
    groups = strongly_connected_groups(graph)
    problems: list[str] = []
    for group in groups:
        if group in known:
            continue
        lines = [f"new cycle between packages {_names(group)}; the imports that close it:"]
        for source in sorted(group):
            for target in sorted(set(graph[source]) & group):
                lines.append(f"  {source} -> {target}: {graph[source][target][0]}")
        problems.append("\n".join(lines))
    for group in known:
        if group not in groups:
            problems.append(
                f"cycle {_names(group)} is listed in KNOWN_CYCLES but no longer exists; "
                f"update KNOWN_CYCLES to the cycles that remain"
            )
    return problems


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent.parent / "app"
    problems = cycle_problems(root)
    for problem in problems:
        print(problem)
    if problems:
        print(f"{len(problems)} package cycle problem(s)")
        return 1
    groups = strongly_connected_groups(package_graph(root))
    print(
        "package cycles: only the listed ones remain: "
        + ", ".join(_names(g) for g in groups)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
