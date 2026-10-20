"""world 的插件目录之间不互相 import。

``app/world/`` 下每个子目录是一个插件目录（world 核心、records、reality、told、perception、npc、
answer）。插件目录里的代码在 world 里面只 import 自己这个目录、插件之间的契约
（:mod:`app.world.services`）和 world 的共享模块（:data:`SHARED`）；world 以外的东西不在这里管
（框架只许 import 公开接口，由 T8 的检查管）。共享模块自己也只 import 共享模块。一个插件要用另一个
插件的东西走服务；工具、段落、步骤由清单按名字排，谁也不用 import 谁。

这里先在临时目录里造出各种写法的越界，证明检查真的会拦；再对真实代码树跑一遍。拆分还没做完的
地方列在 :data:`KNOWN_CROSSINGS`，拆开一处就删掉一处。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

import app as app_pkg
from scripts.check_world_life_imports import imported_names, module_name

# world 的共享模块：插件之间的契约，和几个插件都要用、又不属于哪一个插件的东西。
SHARED = frozenset({"services", "agents", "volume"})

# 拆分还没做完、现在还越界的 import：``文件 -> 它 import 的 world 模块``。拆完是空的。
KNOWN_CROSSINGS = frozenset(
    {
        "world/agents.py -> app.world.wake",
        "world/sources/records.py -> app.world.records",
        "world/sources/told.py -> app.world.wake",
    }
)


def _world_part(target: str) -> str | None:
    """``app.world.<这一段>...`` 里的那一段；不在 world 里面、或者就是 ``app.world`` 包本身时为 ``None``。"""
    parts = target.split(".")
    if parts[:2] != ["app", "world"] or len(parts) < 3:
        return None
    return parts[2]


def crossings(root: Path) -> list[str]:
    """``root``（``app`` 目录）下越界的 import，每处一行 ``world/<文件>:<行号> -> app.world.<模块>``。"""
    root = root.resolve()
    world = root / "world"
    checked: list[tuple[Path, str | None]] = [
        (world / f"{name}.py", None) for name in sorted(SHARED) if (world / f"{name}.py").exists()
    ]
    for directory in sorted(world.iterdir()):
        if directory.is_dir() and any(directory.rglob("*.py")):
            checked += [(path, directory.name) for path in sorted(directory.rglob("*.py"))]
    found = []
    for path, own in checked:
        parts, is_package = module_name(root, path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for lineno, candidates in imported_names(tree, parts, is_package):
            for target in candidates:
                part = _world_part(target)
                if part is not None and part != own and part not in SHARED:
                    found.append(f"{path.relative_to(root)}:{lineno} -> app.world.{part}")
                    break
    return found


def _without_lines(found: list[str]) -> set[str]:
    return {f"{where.rsplit(':', 1)[0]} -> {target}" for where, target in (f.split(" -> ") for f in found)}


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "app"
    for rel, content in {
        "__init__.py": "",
        "world/__init__.py": "",
        "world/services.py": "",
        "world/volume.py": "",
        "world/agents.py": "",
        "world/rounds.py": "",
        "world/records/__init__.py": "",
        "world/records/tree.py": "",
        "world/told/__init__.py": "",
        "world/told/store.py": "",
        **files,
    }.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


REACHING_INTO_ANOTHER_PLUGIN = [
    "import app.world.records.tree",
    "import app.world.records",
    "from app.world import records",
    "from app.world.records import tree",
    "from app.world.records.tree import RecordsRead",
    "from ..records import tree",
    "from ..records.tree import RecordsRead",
    "from .. import records",
    "import importlib\nimportlib.import_module('app.world.records.tree')",
    "__import__('app.world.records')",
    "def later():\n    from app.world.records import tree\n    return tree",
]


@pytest.mark.parametrize("source", REACHING_INTO_ANOTHER_PLUGIN)
def test_a_plugin_importing_another_plugin_is_caught(tmp_path, source):
    root = _tree(tmp_path, {"world/told/plugin.py": source})

    found = crossings(root)

    assert len(found) == 1
    assert found[0].startswith("world/told/plugin.py:")
    assert found[0].endswith("-> app.world.records")


def test_a_plugin_importing_a_world_module_that_is_not_shared_is_caught(tmp_path):
    root = _tree(tmp_path, {"world/told/plugin.py": "from app.world.rounds import Rounds\n"})

    assert crossings(root) == ["world/told/plugin.py:1 -> app.world.rounds"]


def test_a_shared_module_importing_a_plugin_is_caught(tmp_path):
    root = _tree(tmp_path, {"world/agents.py": "from app.world.told import store\n"})

    assert crossings(root) == ["world/agents.py:1 -> app.world.told"]


def test_its_own_directory_the_services_the_shared_modules_and_the_rest_of_the_app_are_allowed(
    tmp_path,
):
    root = _tree(
        tmp_path,
        {
            "world/told/plugin.py": "\n".join(
                [
                    "import app.world",
                    "from app.world.told import store",
                    "from app.world.told.store import kept",
                    "from . import store",
                    "from .store import kept",
                    "from app.world import services",
                    "from app.world.services import WorldIntake",
                    "from app.world.volume import write_atomically",
                    "from ..agents import when",
                    "from app.host import Context, Plugin",
                    "from app.agent.round import current_round",
                ]
            ),
            "world/agents.py": "from app.world.volume import lane_dir\n",
        },
    )

    assert crossings(root) == []


def test_the_world_plugins_cross_only_where_the_split_is_not_done_yet():
    found = crossings(Path(app_pkg.__file__).parent)

    assert _without_lines(found) == KNOWN_CROSSINGS, "\n".join(found)
