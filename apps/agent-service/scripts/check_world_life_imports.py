"""CI 规则：``app/world/`` 与 ``app/living/`` 互不 import，两个方向都不行。

world 和三姐妹的 life 是两个独立的引擎，唯一的连接是通信机制（``app.messaging``）。
这条规则扫两边每一个 ``.py`` 的语法树，找出指向另一边的 import：

* ``import app.living...`` / ``from app.living... import ...`` / ``from app import living``；
* 相对 import（按文件所在的包解析成绝对模块名再判）；
* ``importlib.import_module("app.living...")`` / ``import_module(...)`` / ``__import__(...)``
  里写成字符串字面量的模块名。

判的是 import，不是文字：docstring 和注释里提到另一边的路径不算。两边之外的包（基础层、
各 App 的接线）可以同时 import 两边。

用法：``python3 scripts/check_world_life_imports.py [app 目录]``，默认是本脚本旁边的
``app/``。有违规时逐条打印 ``文件:行号`` 并以 1 退出。只用标准库，CI 上不装依赖直接跑。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

# 哪一边的代码不许 import 哪一边。
FORBIDDEN = {
    "world": "app.living",
    "living": "app.world",
}

_DYNAMIC_IMPORTERS = {"import_module", "__import__"}


def _module_name(root: Path, path: Path) -> tuple[list[str], bool]:
    """文件对应的模块名各段，以及它是不是包的 ``__init__``。``root`` 是 ``app`` 目录。"""
    rel = path.relative_to(root).with_suffix("")
    parts = [root.name, *rel.parts]
    if parts[-1] == "__init__":
        return parts[:-1], True
    return parts, False


def _resolve_from(module_parts: list[str], is_package: bool, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    package = module_parts if is_package else module_parts[:-1]
    base = package[: len(package) - (node.level - 1)] if node.level > 1 else package
    return ".".join([*base, *([node.module] if node.module else [])])


def _hits(target: str, forbidden: str) -> bool:
    return target == forbidden or target.startswith(forbidden + ".")


def _imported_names(tree: ast.AST, module_parts: list[str], is_package: bool):
    """每一处 import 指向的完整模块名（可能是几个候选），连同行号。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, [alias.name]
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_from(module_parts, is_package, node)
            yield node.lineno, [base, *(f"{base}.{a.name}" for a in node.names)]
        elif isinstance(node, ast.Call):
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if name in _DYNAMIC_IMPORTERS and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    yield node.lineno, [first.value]


def find_violations(root: Path) -> list[str]:
    """``root``（``app`` 目录）下所有越界的 import，每条一行 ``相对路径:行号: 说明``。"""
    root = root.resolve()
    violations: list[str] = []
    for side, forbidden in FORBIDDEN.items():
        side_dir = root / side
        if not side_dir.is_dir():
            continue
        for path in sorted(side_dir.rglob("*.py")):
            module_parts, is_package = _module_name(root, path)
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for lineno, candidates in _imported_names(tree, module_parts, is_package):
                hit = next((c for c in candidates if _hits(c, forbidden)), None)
                if hit is not None:
                    rel = path.relative_to(root.parent)
                    violations.append(
                        f"{rel}:{lineno}: app/{side} imports {hit} "
                        f"(world and life only talk through app.messaging)"
                    )
    return violations


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent.parent / "app"
    violations = find_violations(root)
    for line in violations:
        print(line)
    if violations:
        print(f"{len(violations)} import(s) cross the world/life boundary")
        return 1
    print("world/life import boundary: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
