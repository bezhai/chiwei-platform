"""CI 规则：业务代码不自己起任务、不起线程，也够不着事务里的数据库会话。

业务代码（:data:`BUSINESS`：她的插件、world 的插件、``app/plugins`` 的插件，和它们调的 ``nodes``、
``skills``）要在后台跑的东西交给宿主：周期性的登记成钟（``ctx.clock``、``ctx.wake``），一直跑的
登记成任务（``ctx.task``）。宿主知道它们，停下时取消、等它们收尾；业务自己起的任务宿主不知道，停的
时候没人取消，出错了也没人看见。所以业务代码里不许：

* ``create_task``（``asyncio.create_task``、``loop.create_task``、``TaskGroup`` 的 ``create_task``）、
  ``ensure_future``、``asyncio.TaskGroup``；
* ``threading.Thread``、``threading.Timer``。

换了名字也一样：``import threading as th`` 之后的 ``th.Thread``、``from threading import Thread as T``、
``from threading import *`` 之后的 ``Thread``，从 asyncio 的子模块（``asyncio.tasks``、
``asyncio.taskgroups``）import 的，都按 import 解析回完整名字再判。

等着的并发照常：``await asyncio.gather(...)``、``await asyncio.to_thread(...)``、``asyncio.wait_for``
在这一轮里等到结束，不留下后台的东西。

数据库只有存储（``app/data``）开会话、管事务；业务读写走 ``app.data.persist`` 的函数，几条要一起落地
就包一层 ``async with tx():``。所以业务代码里也不许碰 ``current_session``（拿到它就能在 ``tx()`` 里跑
任意语句，绕开那几个读写函数），也不 import ``app.data.tx`` 整个模块（同样够得着它；
``importlib.import_module`` / ``__import__`` 的字符串字面量也算，解析方式和
``scripts/check_world_life_imports.py`` 同一套）；``from app.data.tx import tx`` 照常。``app.data.session``、``get_session``、``AsyncSession`` 由
grep-gate 的 Gap 13 管，那一条覆盖 ``app/data`` 之外的全部代码。

``app/agent``（固定循环和一轮）、宿主、存储、消息机制、能力层是框架，不在这条规则里：替插件起任务、
开会话正是它们的事。

判的是语法树，不是文字：docstring、注释、字符串里提到这些名字不算。``BUSINESS`` 里的目录不在了也
报出来，免得规则什么也扫不到、一直通过。

用法：在 ``apps/agent-service`` 下 ``python3 -m scripts.check_business_tasks_and_sessions [app 目录]``，
默认是 ``app/``。有违规时逐条打印 ``文件:行号: 说明`` 并以 1 退出。只用标准库，CI 上不装依赖直接跑。
"""
from __future__ import annotations

import ast
import sys
from collections.abc import Iterator
from pathlib import Path

from scripts.check_world_life_imports import (
    dynamic_import,
    dynamic_importers,
    module_name,
    resolve_from,
)

BUSINESS: tuple[str, ...] = ("living", "world", "plugins", "nodes", "skills")

# 起一个后台任务的名字（调用的函数名或方法名、import 进来的名字）。
_TASK_STARTERS = frozenset({"create_task", "ensure_future", "TaskGroup"})
# 起任务或线程的模块（连同子模块，``asyncio.tasks`` 算 ``asyncio``）和其中起任务、线程的名字。
_STARTERS: dict[str, frozenset[str]] = {
    "asyncio": _TASK_STARTERS,
    "threading": frozenset({"Thread", "Timer"}),
}

_TASKS = "starts a task or a thread; hand background work to the host (ctx.task, ctx.clock)"
_SESSION = (
    "reaches the transaction's session; business code reads and writes through "
    "app.data.persist inside tx()"
)


def _starters_in(module: str) -> frozenset[str]:
    """``module``（或它所在的顶层包）里起任务、线程的名字；不是这样的模块时为空。"""
    return _STARTERS.get(module.split(".")[0], frozenset())


def _bindings(tree: ast.AST, parts: list[str], is_package: bool) -> dict[str, str]:
    """文件里 import 绑定的名字指向的完整名字：``import a.b`` 绑定 ``a``，``import a.b as x`` 把
    ``x`` 绑到 ``a.b``，``from m import n as k`` 把 ``k`` 绑到 ``m.n``，``from m import *`` 绑定 ``m``
    里起任务、线程的那几个名字。不分作用域：同一个文件里一个名字指两样东西的情况不管。"""
    bound: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bound[alias.asname] = alias.name
                else:
                    top = alias.name.split(".")[0]
                    bound[top] = top
        elif isinstance(node, ast.ImportFrom):
            base = resolve_from(parts, is_package, node)
            for alias in node.names:
                if alias.name == "*":
                    bound.update({name: f"{base}.{name}" for name in _starters_in(base)})
                else:
                    bound[alias.asname or alias.name] = f"{base}.{alias.name}"
    return bound


def _qualified(node: ast.expr, bound: dict[str, str]) -> str | None:
    """``x.y.z`` 这样的表达式按 import 解析出的完整名字；开头的名字不是 import 来的时为 ``None``。"""
    if isinstance(node, ast.Name):
        return bound.get(node.id)
    if isinstance(node, ast.Attribute):
        base = _qualified(node.value, bound)
        return f"{base}.{node.attr}" if base is not None else None
    return None


def _starts_something(qualified: str) -> bool:
    module, _, name = qualified.rpartition(".")
    return name in _starters_in(module)


def _problems(tree: ast.AST, parts: list[str], is_package: bool) -> Iterator[tuple[int, str]]:
    bound = _bindings(tree, parts, is_package)
    importers = dynamic_importers(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            base = resolve_from(parts, is_package, node)
            names = {alias.name for alias in node.names}
            if names & _starters_in(base):
                yield node.lineno, _TASKS
            elif base == "app.data.tx" and "current_session" in names:
                yield node.lineno, _SESSION
            elif base == "app.data" and "tx" in names:
                yield node.lineno, _SESSION
        elif isinstance(node, ast.Import):
            if any(alias.name == "app.data.tx" for alias in node.names):
                yield node.lineno, _SESSION
        elif isinstance(node, ast.Attribute):
            qualified = _qualified(node, bound)
            # 任何东西上的 create_task（loop、TaskGroup 实例）都算；Thread、Timer 只算 threading 的。
            if node.attr in _TASK_STARTERS or (qualified and _starts_something(qualified)):
                yield node.lineno, _TASKS
            elif node.attr == "current_session":
                yield node.lineno, _SESSION
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            qualified = _qualified(node, bound)
            if node.id in {"create_task", "ensure_future"} or (
                qualified and _starts_something(qualified)
            ):
                yield node.lineno, _TASKS
            elif node.id == "current_session":
                yield node.lineno, _SESSION
        elif isinstance(node, ast.Call):
            if "app.data.tx" in (dynamic_import(node, parts, is_package, importers) or ()):
                yield node.lineno, _SESSION


def find_violations(root: Path) -> list[str]:
    """``root``（``app`` 目录）下业务代码的每一处违规，每条一行 ``相对路径[:行号]: 说明``，按文件和行排。"""
    root = root.resolve()
    problems: list[str] = []
    for directory in BUSINESS:
        place = root / directory
        if not place.is_dir():
            problems.append(
                f"{root.name}/{directory}: listed as business code but it does not exist "
                f"(update BUSINESS in scripts/check_business_tasks_and_sessions.py)"
            )
            continue
        for path in sorted(place.rglob("*.py")):
            parts, is_package = module_name(root, path)
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            seen: set[tuple[int, str]] = set()
            for lineno, what in sorted(_problems(tree, parts, is_package)):
                if (lineno, what) not in seen:
                    seen.add((lineno, what))
                    problems.append(f"{path.relative_to(root.parent)}:{lineno}: {what}")
    return problems


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent.parent / "app"
    problems = find_violations(root)
    for line in problems:
        print(line)
    if problems:
        print(f"{len(problems)} task or session problem(s) in business code")
        return 1
    print(f"business code ({', '.join(BUSINESS)}): starts no task or thread, reaches no session")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
