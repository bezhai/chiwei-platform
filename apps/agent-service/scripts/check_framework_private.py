"""CI 规则：业务代码只用框架公开的东西；跨包不 import 下划线开头的名字。

**框架的私有模块。** 业务代码（:data:`SCOPE`：她的插件、world 的插件、``app/plugins`` 的插件，它们调的
``nodes``、``skills``，和两个 App 的清单）从宿主只 import ``app.host`` 这个包导出的名字，不 import
``app/host/`` 下的任何一个模块；另外不 import :data:`PRIVATE_MODULES` 里那几个模块：一轮的历史怎么存、
怎么裁（``app.agent.continuity``，契约类型在 ``app.agent.contracts``），通信机制怎么收（
``app.messaging.receiving``，插件经宿主开收件箱），数据库会话（``app.data.session``）。这些模块的
内容会跟着框架改，业务一 import 就把它们钉住了；业务要的东西由包导出（``app.host``、``app.agent``），
或者由宿主交给插件（``ctx``）。

**跨包的下划线名字。** ``app/`` 下任何代码（框架也一样）不从另一个包 import 名字以下划线开头的东西，
模块路径里有一段以下划线开头的也不行（``app.x._y``、``from app.x import _y``）：下划线是那个包说"这是
我自己的"。同一个包里照常。包是 ``app/`` 下的一级目录或顶层模块，和 ``scripts/check_package_cycles.py``
的节点一样。

认 import 的方式和 ``scripts/check_world_life_imports.py`` 是同一套语法树扫描（函数里面的 import、
相对 import、``importlib.import_module`` / ``__import__`` 的字符串字面量都算，docstring 和注释不算）。
``SCOPE`` 里的目录或文件不在了也报出来，免得规则什么也扫不到、一直通过。

用法：在 ``apps/agent-service`` 下 ``python3 -m scripts.check_framework_private [app 目录]``，默认是
``app/``。有违规时逐条打印 ``文件:行号: 说明`` 并以 1 退出。只用标准库，CI 上不装依赖直接跑。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

from scripts.check_world_life_imports import imported_names, module_name

# 业务代码：插件、插件调的 nodes 和 skills，两个 App 的清单。
SCOPE: tuple[str, ...] = ("living", "world", "plugins", "nodes", "skills", "manifests")

# 宿主之外，业务不 import 的框架模块。宿主的模块（``app/host/`` 下每一个）另外算，见 :func:`private_modules`。
PRIVATE_MODULES: tuple[str, ...] = (
    "app.agent.continuity",
    "app.messaging.receiving",
    "app.data.session",
)


def private_modules(root: Path) -> tuple[str, ...]:
    """业务不 import 的模块：:data:`PRIVATE_MODULES`，和 ``app/host/`` 下的每个模块（只有包本身公开）。"""
    host = root / "host"
    own = sorted(
        f"{root.name}.host.{p.stem}"
        for p in host.glob("*.py")
        if p.stem != "__init__"
    ) + sorted(f"{root.name}.host.{d.name}" for d in host.iterdir() if (d / "__init__.py").is_file())
    return tuple(own) + tuple(m.replace("app.", f"{root.name}.", 1) for m in PRIVATE_MODULES)


def _hits(target: str, module: str) -> bool:
    return target == module or target.startswith(module + ".")


def _private_segment(target: str) -> bool:
    return any(s.startswith("_") and not s.startswith("__") for s in target.split(".")[1:])


def _package(parts: list[str]) -> str | None:
    return parts[1] if len(parts) > 1 else None


def _python_files(root: Path, place: str) -> list[Path]:
    path = root / place
    if path.is_dir():
        return sorted(path.rglob("*.py"))
    return [path] if path.is_file() else []


def find_violations(root: Path) -> list[str]:
    """``root``（``app`` 目录）下的每一处违规，每条一行 ``相对路径[:行号]: 说明``。"""
    root = root.resolve()
    problems: list[str] = []
    for place in SCOPE:
        if not (root / place).exists() and not (root / f"{place}.py").exists():
            problems.append(
                f"{root.name}/{place}: listed as business code but it does not exist "
                f"(update SCOPE in scripts/check_framework_private.py)"
            )
    private = private_modules(root)
    business = {p for place in SCOPE for p in _python_files(root, place)}
    for path in sorted(root.rglob("*.py")):
        parts, is_package = module_name(root, path)
        own_package = _package(parts)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for lineno, candidates in imported_names(tree, parts, is_package):
            ours = [c for c in candidates if c.split(".")[0] == root.name]
            site = f"{path.relative_to(root.parent)}:{lineno}"
            if path in business:
                hit = next((c for c in ours for m in private if _hits(c, m)), None)
                if hit is not None:
                    problems.append(
                        f"{site}: imports {hit}, a framework module business code does not use "
                        f"(take it from the package's exports or from the host's ctx)"
                    )
                    continue
            hit = next(
                (
                    c
                    for c in ours
                    if _private_segment(c) and _package(c.split(".")) != own_package
                ),
                None,
            )
            if hit is not None:
                problems.append(f"{site}: imports {hit}, a private name of another package")
    return problems


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent.parent / "app"
    problems = find_violations(root)
    for line in problems:
        print(line)
    if problems:
        print(f"{len(problems)} import(s) of something private")
        return 1
    print("framework-private imports: none")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
