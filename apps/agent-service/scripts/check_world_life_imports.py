"""CI 规则：``app/world/`` 与 ``app/living/`` 互不 import，两个方向都不行；两个 App 各自的清单
（``app/manifests/world.py``、``app/manifests/agent_service.py``）也一样。

world 和三姐妹的 life 是两个独立的引擎，唯一的连接是通信机制（``app.messaging``）。一个 App
的进程只起它清单里的插件（``app.deployment.APPS`` 指向清单模块），清单模块又 import 插件模块里的
agent 定义，所以 world 的插件或清单带进 life 的代码，world 的进程就背上了 life，反过来也一样。
这条规则扫这些文件每一个 ``.py`` 的语法树，找出指向另一边的 import：

* ``import app.living...`` / ``from app.living... import ...`` / ``from app import living``；
* 相对 import（按文件所在的包解析成绝对模块名再判）；
* ``importlib.import_module("app.living...")`` / ``import_module(...)`` / ``__import__(...)``
  里写成字符串字面量的模块名：相对的名字按字面量的 ``package``（或 ``__package__``）、
  ``__import__`` 的 ``level`` 解析，``__import__`` 的 ``fromlist`` 算 ``from ... import``，
  换了名字的 ``from importlib import import_module as load`` 同样认。

判的是 import，不是文字：docstring 和注释里提到另一边的路径不算。规则之外的代码（基础层、
``app/plugins/`` 下两边都不属于的插件、两份清单共用的 ``app/manifests/history.py``）不在这条规则里。
规则列的目录或文件不存在也算违规：它们搬走以后规则什么也扫不到、一直通过，这种情况要改规则。

用法：``python3 scripts/check_world_life_imports.py [app 目录]``，默认是本脚本旁边的
``app/``。有违规时逐条打印 ``文件:行号`` 并以 1 退出。只用标准库，CI 上不装依赖直接跑。
"""
from __future__ import annotations

import ast
import sys
from importlib.util import resolve_name
from pathlib import Path

# 哪一边的代码（``app`` 目录下的目录或文件）不许 import 哪一边。
FORBIDDEN: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("world", ("world", "manifests/world.py"), "app.living"),
    ("life", ("living", "manifests/agent_service.py"), "app.world"),
)

_DYNAMIC_IMPORTERS = {"import_module", "__import__"}


def module_name(root: Path, path: Path) -> tuple[list[str], bool]:
    """文件对应的模块名各段，以及它是不是包的 ``__init__``。``root`` 是 ``app`` 目录。"""
    rel = path.relative_to(root).with_suffix("")
    parts = [root.name, *rel.parts]
    if parts[-1] == "__init__":
        return parts[:-1], True
    return parts, False


def resolve_from(module_parts: list[str], is_package: bool, node: ast.ImportFrom) -> str:
    """``from ... import`` 的那个模块的完整名字：相对 import 按文件所在的包解析。"""
    if node.level == 0:
        return node.module or ""
    package = module_parts if is_package else module_parts[:-1]
    base = package[: len(package) - (node.level - 1)] if node.level > 1 else package
    return ".".join([*base, *([node.module] if node.module else [])])


def _hits(target: str, forbidden: str) -> bool:
    return target == forbidden or target.startswith(forbidden + ".")


def dynamic_importers(tree: ast.AST) -> dict[str, str]:
    """文件里能当 import 函数调用的名字 -> 它是哪一个（``import_module`` 或 ``__import__``）：
    这两个名字本身，和 ``from importlib import import_module as load`` 这样换的名字。属性调用
    （``importlib.import_module``、``il.import_module``）按属性名认，不用查这里。"""
    found = {name: name for name in _DYNAMIC_IMPORTERS}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            if node.module in {"importlib", "builtins"}:
                for alias in node.names:
                    if alias.name in _DYNAMIC_IMPORTERS and alias.asname:
                        found[alias.asname] = alias.name
    return found


def _argument(call: ast.Call, position: int, keyword: str) -> ast.expr | None:
    if len(call.args) > position:
        return call.args[position]
    return next((k.value for k in call.keywords if k.arg == keyword), None)


def _literal(node: ast.expr | None, package: str) -> object:
    """字面量的值；``__package__`` 是文件所在的包。别的表达式是 ``...``（静态解析不出来）。"""
    if node is None:
        return None
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name) and node.id == "__package__":
        return package
    if isinstance(node, (ast.List, ast.Tuple)):
        items = [_literal(item, package) for item in node.elts]
        return items if all(isinstance(i, str) for i in items) else ...
    return ...


def dynamic_import(
    call: ast.Call, module_parts: list[str], is_package: bool, importers: dict[str, str]
) -> list[str] | None:
    """一次 ``import_module(...)`` / ``__import__(...)`` 调用 import 的完整模块名（``__import__`` 的
    ``fromlist`` 里每一项也是一个候选，和 ``from ... import`` 一样）；不是这样的调用、名字不是
    字面量、或者相对名字解析不出来时为 ``None``。``importers`` 是 :func:`dynamic_importers` 的结果。"""
    func = call.func
    if isinstance(func, ast.Name):
        kind = importers.get(func.id)
    elif isinstance(func, ast.Attribute) and func.attr in _DYNAMIC_IMPORTERS:
        kind = func.attr
    else:
        kind = None
    if kind is None:
        return None
    package = ".".join(module_parts if is_package else module_parts[:-1])
    name = _literal(_argument(call, 0, "name"), package)
    if not isinstance(name, str):
        return None
    fromlist: object = []
    if kind == "import_module":
        anchor = _literal(_argument(call, 1, "package"), package)
    else:
        level = _literal(_argument(call, 4, "level"), package)
        if level is None:
            level = 0
        if not isinstance(level, int) or isinstance(level, bool) or level < 0:
            return None
        name, anchor = "." * level + name, package
        fromlist = _literal(_argument(call, 3, "fromlist"), package) or []
    try:
        resolved = resolve_name(name, anchor if isinstance(anchor, str) else None)
    except (ImportError, ValueError):
        return None
    extra = fromlist if isinstance(fromlist, list) else []
    return [resolved, *(f"{resolved}.{item}" for item in extra)]


def imported_names(tree: ast.AST, module_parts: list[str], is_package: bool):
    """每一处 import 指向的完整模块名（可能是几个候选），连同行号。"""
    importers = dynamic_importers(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, [alias.name]
        elif isinstance(node, ast.ImportFrom):
            base = resolve_from(module_parts, is_package, node)
            yield node.lineno, [base, *(f"{base}.{a.name}" for a in node.names)]
        elif isinstance(node, ast.Call):
            names = dynamic_import(node, module_parts, is_package, importers)
            if names:
                yield node.lineno, names


def find_violations(root: Path) -> list[str]:
    """``root``（``app`` 目录）下所有越界的 import，每条一行 ``相对路径:行号: 说明``。"""
    root = root.resolve()
    violations: list[str] = []
    for side, places, forbidden in FORBIDDEN:
        for place in places:
            if not (root / place).exists():
                violations.append(
                    f"{root.name}/{place}: {side} code is listed here but it does not exist "
                    f"(update FORBIDDEN in scripts/check_world_life_imports.py)"
                )
        for path in _python_files(root, places):
            module_parts, is_package = module_name(root, path)
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for lineno, candidates in imported_names(tree, module_parts, is_package):
                hit = next((c for c in candidates if _hits(c, forbidden)), None)
                if hit is not None:
                    rel = path.relative_to(root.parent)
                    violations.append(
                        f"{rel}:{lineno}: {side} code imports {hit} "
                        f"(world and life only talk through app.messaging)"
                    )
    return violations


def _python_files(root: Path, places: tuple[str, ...]) -> list[Path]:
    """``places`` 下的 ``.py``：目录取其中每一个，文件取它自己（不存在的由调用方报出）。"""
    files: list[Path] = []
    for place in places:
        path = root / place
        if path.is_dir():
            files.extend(sorted(path.rglob("*.py")))
        elif path.is_file():
            files.append(path)
    return files


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
