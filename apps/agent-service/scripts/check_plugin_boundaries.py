"""CI 规则：一个插件不 import 另一个插件，插件之间只经服务交互。

插件在三个地方（:data:`SIDES`）：她的功能在 ``app/living/<插件>/``，world 的在 ``app/world/<插件>/``
（world 核心也是一个，``core/``），各边一个目录一个插件；``app/plugins/`` 里一个模块一个插件
（ops、operator、skills）。一个插件要另一个插件的东西，就在 setup 里 ``ctx.service`` 取它的服务；
工具、段落、步骤由清单按名字排，谁也不用 import 谁。所以在同一边里面，一个插件只许 import：

* 它自己目录里的模块；
* 这一边的共享模块（:attr:`Side.shared`）：living 是 ``services``（服务的 Protocol 和接口里出现的
  数据类型）、``common``（共用的纯函数）；world 是 ``services``、``agents``、``volume``。

共享模块和这一边的包本身（``__init__.py``）只许 import 共享模块：共享模块 import 了哪个插件，
用它的每个插件就都连上了那个插件；包本身在任何一个插件之前执行，它 import 的插件会被带进每个插件。
living 和 world 的目录下除了插件目录只有共享模块：目录外多出来的模块是几个插件绕开服务共用的
私下通道，也报出来。

另一边的插件同样不许 import：``app/plugins`` 的插件不 import 她的插件，她的插件、world 的插件也不
import ``app/plugins`` 的插件，三边的任何代码（插件、共享模块、包本身）都一样。另一边的包本身和
共享模块不是插件，这条规则不管；living 和 world 之间互不 import（共享模块也不行）由
``scripts/check_world_life_imports.py`` 管。三边以外的包（宿主、一轮、存储、消息……）不在这条规则
里：插件能 import 它们的哪些部分由 ``scripts/check_framework_private.py`` 管。

认 import 的方式和 ``scripts/check_world_life_imports.py`` 是同一套语法树扫描（函数里面的 import、
相对 import、``importlib.import_module`` / ``__import__`` 的字符串字面量都算，docstring 和注释
不算）。名单上的目录或共享模块不在了也报出来：它们搬走以后规则什么也扫不到、一直通过。一个目录里
没有 ``.py``（本地删掉插件后剩下的 ``__pycache__``）不是插件。

用法：在 ``apps/agent-service`` 下 ``python3 -m scripts.check_plugin_boundaries [app 目录]``，默认是
``app/``。有违规时逐条打印 ``文件:行号: 说明`` 并以 1 退出。只用标准库，CI 上不装依赖直接跑。
"""
from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

from scripts.check_world_life_imports import imported_names, module_name


@dataclass(frozen=True)
class Side:
    """``app/<name>/`` 下的插件怎么划分。"""

    # 每个插件都能 import 的平铺模块（不带 ``.py``）。
    shared: frozenset[str]
    # True：平铺模块各自是一个插件（``app/plugins``）。False：插件都是目录，平铺模块只能是共享模块。
    modules_are_plugins: bool = False


SIDES: dict[str, Side] = {
    "living": Side(shared=frozenset({"services", "common"})),
    "world": Side(shared=frozenset({"services", "agents", "volume"})),
    "plugins": Side(shared=frozenset(), modules_are_plugins=True),
}


def _is_package_dir(path: Path) -> bool:
    return path.is_dir() and any(path.rglob("*.py"))


def plugins_of(root: Path, side: str) -> list[str]:
    """``root``（``app`` 目录）下 ``side`` 这一边的插件名。"""
    directory = root / side
    names = {d.name for d in directory.iterdir() if _is_package_dir(d)}
    if SIDES[side].modules_are_plugins:
        names |= {p.stem for p in directory.glob("*.py") if p.stem != "__init__"}
    return sorted(names)


def _plugin_of(target: str, root_name: str) -> tuple[str, str] | None:
    """``target`` 落在哪一边的哪一段：``app.<边>.<这一段>...`` 的 ``(边, 这一段)``。不在任何一边、
    是一边的包本身、或者是那一边的共享模块时为 ``None``。"""
    parts = target.split(".")
    if len(parts) < 3 or parts[0] != root_name or parts[1] not in SIDES:
        return None
    side, part = parts[1], parts[2]
    if part in SIDES[side].shared:
        return None
    return side, part


def _check(root: Path, side: str, path: Path, own: str | None, who: str) -> list[str]:
    """``side`` 这一边的 ``path``（插件 ``own`` 里的，共享模块和包本身是 ``None``）越过插件边界的每一处。"""
    parts, is_package = module_name(root, path)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    for lineno, candidates in imported_names(tree, parts, is_package):
        for target in candidates:
            hit = _plugin_of(target, root.name)
            if hit is not None and hit != (side, own):
                found.append(
                    f"{path.relative_to(root.parent)}:{lineno}: {who} imports {target} "
                    f"(plugins only talk through services; see scripts/check_plugin_boundaries.py)"
                )
                break
    return found


def find_violations(root: Path) -> list[str]:
    """``root``（``app`` 目录）下越过插件边界的每一处，每条一行 ``相对路径[:行号]: 说明``。"""
    root = root.resolve()
    problems: list[str] = []
    for side, rule in SIDES.items():
        directory = root / side
        if not directory.is_dir():
            problems.append(
                f"{root.name}/{side}: plugins are listed here but it does not exist "
                f"(update SIDES in scripts/check_plugin_boundaries.py)"
            )
            continue
        for name in sorted(rule.shared):
            if not (directory / f"{name}.py").is_file():
                problems.append(
                    f"{root.name}/{side}/{name}.py: listed as a shared module but it does not "
                    f"exist (update SIDES in scripts/check_plugin_boundaries.py)"
                )
        plugins = plugins_of(root, side)
        for path in sorted(directory.glob("*.py")):
            name = path.stem
            if rule.modules_are_plugins and name != "__init__":
                problems += _check(root, side, path, name, f"plugin {name}")
            elif name == "__init__" or name in rule.shared:
                problems += _check(root, side, path, None, name)
            else:
                problems.append(
                    f"{path.relative_to(root.parent)}: not in a plugin directory and not a shared "
                    f"module of {side} (move it into the plugin that owns it, or make it a service)"
                )
        for plugin in plugins:
            plugin_dir = directory / plugin
            if not plugin_dir.is_dir():
                continue
            for path in sorted(plugin_dir.rglob("*.py")):
                problems += _check(root, side, path, plugin, f"plugin {plugin}")
    return problems


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent.parent / "app"
    problems = find_violations(root)
    for line in problems:
        print(line)
    if problems:
        print(f"{len(problems)} plugin boundary problem(s)")
        return 1
    print(
        "plugin boundaries: clean ("
        + "; ".join(f"{side}: {', '.join(plugins_of(root.resolve(), side))}" for side in SIDES)
        + ")"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
