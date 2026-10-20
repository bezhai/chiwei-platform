"""她这边插件之间的边界：插件目录之间不 import，只经服务交互。

``app/living/<插件>/`` 是一个插件。一个插件目录里的代码只许 import 自己目录里的模块、
:mod:`app.living.services`（服务的接口和接口里的数据类型）、:mod:`app.living.common`（共用的
纯函数）和 living 之外的包。另一个插件的模块不行：要别人的东西，就在 setup 里 ``ctx.service`` 取它的
服务。``services.py`` 和 ``common.py`` 不 import 任何插件。

``app/living`` 下除了插件目录只有包本身、``services.py`` 和 ``common.py``，每个目录都是插件，所以这条
规则管着 living 的每一行代码。

认 import 的方式和 CI 的包检查是同一套（:func:`scripts.check_world_life_imports.imported_names`：
函数里的 import、相对 import、``import_module`` 的字符串字面量都算，docstring 和注释不算）。
"""
from __future__ import annotations

import ast
from pathlib import Path

import app as app_pkg
from scripts.check_world_life_imports import imported_names, module_name

# 插件目录之外、每个插件都可以 import 的两份。
SHARED = ("services", "common")


def living_plugins(app_dir: Path) -> list[str]:
    """``app_dir``（``app`` 目录）下 living 的插件：``app/living`` 里带 ``__init__.py`` 的目录。"""
    living = app_dir / "living"
    return sorted(
        d.name for d in living.iterdir() if d.is_dir() and (d / "__init__.py").is_file()
    )


def boundary_violations(app_dir: Path) -> list[str]:
    """``app_dir``（``app`` 目录）下越过插件边界的每一处 import，一条 ``文件:行号: 说明``。"""
    living = app_dir / "living"
    problems: list[str] = []

    def check(path: Path, allowed: set[str], who: str) -> None:
        parts, is_package = module_name(app_dir, path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for lineno, candidates in imported_names(tree, parts, is_package):
            for name in candidates:
                segments = name.split(".")
                if segments[:2] == [app_dir.name, "living"] and len(segments) > 2:
                    if segments[2] not in allowed:
                        problems.append(
                            f"{path.relative_to(app_dir.parent)}:{lineno}: {who} imports "
                            f"{name} (plugins only talk through app.living.services)"
                        )
                        break

    for plugin in living_plugins(app_dir):
        for path in sorted((living / plugin).rglob("*.py")):
            check(path, {plugin, *SHARED}, f"plugin {plugin}")
    for shared in SHARED:
        path = living / f"{shared}.py"
        if path.is_file():
            check(path, set(SHARED), shared)
    return problems


def test_living_plugins_import_only_themselves_services_and_common():
    assert boundary_violations(Path(app_pkg.__file__).parent) == []


def test_living_is_its_plugins_services_and_common():
    """``app/living`` 下散放的模块只有包本身、``services`` 和 ``common``，每个目录都是上面那条检查管着的
    插件。多出一个散放的模块，或者一个没有 ``__init__.py`` 的目录，它的 import 就没人管了。"""
    app_dir = Path(app_pkg.__file__).parent
    living = app_dir / "living"

    assert sorted(p.name for p in living.glob("*.py")) == [
        "__init__.py",
        "common.py",
        "services.py",
    ]
    directories = sorted(
        d.name for d in living.iterdir() if d.is_dir() and d.name != "__pycache__"
    )
    assert directories, "app/living 下一个插件目录都没有"
    assert living_plugins(app_dir) == directories


def _tree(root: Path, files: dict[str, str]) -> Path:
    app_dir = root / "app"
    for rel, text in {"__init__.py": "", "living/__init__.py": "", **files}.items():
        path = app_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return app_dir


def test_the_check_reports_every_way_one_plugin_reaches_into_another(tmp_path):
    app_dir = _tree(
        tmp_path,
        {
            "living/services.py": "",
            "living/common.py": "from app.living.services import Residents\n",
            "living/flat.py": "",
            "living/a/__init__.py": (
                "from app.living.a.store import thing\n"
                "from app.living import services, common\n"
                "from app.host import Plugin\n"
                "from app.data.persist import fetch_all\n"
            ),
            "living/a/store.py": (
                "from . import other\n"
                "from app.living.b.store import thing\n"
                "from ..b import store\n"
                "from app.living import flat\n"
                "def later():\n"
                "    import importlib\n"
                "    importlib.import_module('app.living.b')\n"
            ),
            "living/a/other.py": "",
            "living/b/__init__.py": "",
            "living/b/store.py": "thing = 1\n",
        },
    )
    (app_dir / "living/services.py").write_text("from app.living.a import store\n")

    problems = boundary_violations(app_dir)

    assert [p.split(": ")[0] for p in problems] == [
        "app/living/a/store.py:2",
        "app/living/a/store.py:3",
        "app/living/a/store.py:4",
        "app/living/a/store.py:7",
        "app/living/services.py:1",
    ]
    assert "plugin a imports app.living.b.store" in problems[0]
    assert "services imports app.living.a" in problems[-1]
