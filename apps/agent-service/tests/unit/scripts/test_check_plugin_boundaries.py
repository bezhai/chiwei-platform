"""CI 规则：一个插件不 import 另一个插件，插件之间只经服务交互。

规则本身在 ``scripts/check_plugin_boundaries.py``，CI（``.github/workflows/grep-gate.yml``）直接跑
它。这里用临时目录造出每一种写法的越界，证明它报出越界的文件和行；造出插件目录之外多出来的
模块、规则名单上已经不在的目录和共享模块，证明它们也报出来；再对真实代码树跑一遍，证明现在是
干净的。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import app as app_pkg
from scripts.check_plugin_boundaries import SIDES, find_violations

SERVICE_DIR = Path(__file__).resolve().parents[3]


def _tree(tmp_path: Path, files: dict[str, str], *, without: tuple[str, ...] = ()) -> Path:
    """一棵最小的 ``app`` 目录：三边各有包本身和名单上的共享模块，living、world 各两个插件
    （``a``、``b``），``app/plugins`` 两个插件模块；``files`` 加上或覆盖，``without`` 里的不建。"""
    base = {
        "__init__.py": "",
        "living/__init__.py": "",
        "living/services.py": "",
        "living/common.py": "",
        "living/a/__init__.py": "",
        "living/a/store.py": "thing = 1\n",
        "living/b/__init__.py": "",
        "living/b/store.py": "thing = 1\n",
        "world/__init__.py": "",
        "world/services.py": "",
        "world/agents.py": "",
        "world/volume.py": "",
        "world/a/__init__.py": "",
        "world/a/store.py": "thing = 1\n",
        "world/b/__init__.py": "",
        "world/b/store.py": "thing = 1\n",
        "plugins/__init__.py": "",
        "plugins/ops.py": "",
        "plugins/skills.py": "",
    }
    root = tmp_path / "app"
    for rel, content in {**base, **files}.items():
        if rel in without:
            continue
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def _sites(problems: list[str]) -> list[str]:
    return [p.split(": ")[0] for p in problems]


# 每一种写法都是 living 的插件 a 伸进插件 b；world 一边把 living 换成 world。
A_REACHING_INTO_B = [
    "import app.{side}.b.store",
    "import app.{side}.b",
    "from app.{side} import b",
    "from app.{side}.b import store",
    "from app.{side}.b.store import thing",
    "from ..b import store",
    "from ..b.store import thing",
    "from .. import b",
    "import importlib\nimportlib.import_module('app.{side}.b.store')",
    "from importlib import import_module\nimport_module('app.{side}.b')",
    "__import__('app.{side}.b')",
    "import importlib\nimportlib.import_module('.b', 'app.{side}')",
    "import importlib\nimportlib.import_module('..b.store', __package__)",
    "from importlib import import_module as load\nload('app.{side}.b')",
    "import importlib as il\nil.import_module('.b.store', package='app.{side}')",
    "__import__('b.store', globals(), locals(), [], 2)",
    "__import__('app.{side}', fromlist=['b'])",
    "def later():\n    from app.{side}.b import store\n    return store",
    "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from app.{side}.b.store import thing",
]


@pytest.mark.parametrize("side", ["living", "world"])
@pytest.mark.parametrize("source", A_REACHING_INTO_B)
def test_a_plugin_reaching_into_another_plugin_is_reported_with_its_file_and_line(
    tmp_path, side, source
):
    root = _tree(tmp_path, {f"{side}/a/tools.py": "\n\n" + source.format(side=side) + "\n"})

    problems = find_violations(root)

    assert len(problems) == 1, problems
    lines = len(source.split("\n"))
    assert problems[0].startswith(f"app/{side}/a/tools.py:")
    assert int(_sites(problems)[0].rsplit(":", 1)[1]) in range(3, 3 + lines)
    assert f"plugin a imports app.{side}.b" in problems[0]


def test_one_plugin_module_importing_another_in_app_plugins_is_reported(tmp_path):
    root = _tree(tmp_path, {"plugins/ops.py": "from app.plugins.skills import setup\n"})

    problems = find_violations(root)

    assert _sites(problems) == ["app/plugins/ops.py:1"]
    assert "plugin ops imports app.plugins.skills" in problems[0]


# 一边的代码伸进另一边的插件：(文件, 写法, 报出的说明里那一段)。
REACHING_ACROSS_SIDES = [
    ("living/a/__init__.py", "from app.plugins.ops import setup", "plugin a imports app.plugins.ops"),
    ("living/a/tools.py", "import app.plugins.skills", "plugin a imports app.plugins.skills"),
    ("living/a/tools.py", "from app.plugins import ops", "plugin a imports app.plugins.ops"),
    ("living/a/tools.py", "from ...plugins import ops", "plugin a imports app.plugins.ops"),
    (
        "living/a/tools.py",
        "import importlib\nimportlib.import_module('app.plugins.ops')",
        "plugin a imports app.plugins.ops",
    ),
    ("living/a/tools.py", "from app.world.b import store", "plugin a imports app.world.b"),
    ("world/a/tools.py", "from app.plugins.skills import setup", "plugin a imports app.plugins.skills"),
    ("world/a/tools.py", "from app.living.b.store import thing", "plugin a imports app.living.b"),
    ("plugins/ops.py", "from app.living.a import store", "plugin ops imports app.living.a"),
    ("plugins/ops.py", "from app.world.b.store import thing", "plugin ops imports app.world.b"),
    ("plugins/ops.py", "from ..living import a", "plugin ops imports app.living.a"),
    ("living/services.py", "from app.plugins.ops import setup", "services imports app.plugins.ops"),
    ("world/agents.py", "from app.living.a import store", "agents imports app.living.a"),
    ("world/__init__.py", "from app.plugins import skills", "__init__ imports app.plugins.skills"),
    ("plugins/__init__.py", "from app.world.a import store", "__init__ imports app.world.a"),
]


@pytest.mark.parametrize(("where", "source", "said"), REACHING_ACROSS_SIDES)
def test_reaching_into_a_plugin_on_another_side_is_reported(tmp_path, where, source, said):
    """插件在三个地方，哪一边的代码伸进另一边的插件都是绕开服务：``app/plugins`` 的插件不 import
    她的插件，她的插件也不 import ``app/plugins`` 的插件。"""
    root = _tree(tmp_path, {where: "\n\n" + source + "\n"})

    problems = find_violations(root)

    assert len(problems) == 1, problems
    assert problems[0].startswith(f"app/{where}:")
    assert int(_sites(problems)[0].rsplit(":", 1)[1]) in range(3, 3 + len(source.split("\n")))
    assert said in problems[0]


def test_another_sides_package_and_shared_modules_are_not_a_plugin(tmp_path):
    """另一边的包本身和它的共享模块不是插件，这条规则不管；living 和 world 之间互不 import 由
    ``scripts/check_world_life_imports.py`` 管。"""
    root = _tree(
        tmp_path,
        {
            "plugins/ops.py": "\n".join(
                [
                    "import app.living",
                    "from app import world",
                    "from app.living.services import X",
                    "from app.living import common",
                    "from app.world.agents import Y",
                    "from app.world import services, volume",
                    "from ..living.services import Z",
                ]
            ),
            "living/a/tools.py": "import app.plugins\nfrom app import plugins\n",
        },
    )

    assert find_violations(root) == []


@pytest.mark.parametrize(
    ("side", "shared", "plugin"),
    [("living", "services", "a"), ("living", "common", "b"), ("world", "agents", "a")],
)
def test_a_shared_module_importing_a_plugin_is_reported(tmp_path, side, shared, plugin):
    """共享模块是插件之间的契约，它 import 了哪个插件，用它的每个插件就都连上了那个插件。"""
    root = _tree(tmp_path, {f"{side}/{shared}.py": f"from app.{side}.{plugin} import store\n"})

    problems = find_violations(root)

    assert _sites(problems) == [f"app/{side}/{shared}.py:1"]
    assert f"{shared} imports app.{side}.{plugin}" in problems[0]


@pytest.mark.parametrize("side", ["living", "world"])
def test_the_side_package_itself_importing_a_plugin_is_reported(tmp_path, side):
    """``app/<边>/__init__.py`` 在任何一个插件被 import 之前先执行：它 import 一个插件，就把那个
    插件带进了每一个插件。"""
    root = _tree(tmp_path, {f"{side}/__init__.py": f"from app.{side}.a import store\n"})

    assert _sites(find_violations(root)) == [f"app/{side}/__init__.py:1"]


@pytest.mark.parametrize("side", ["living", "world"])
def test_a_module_outside_every_plugin_directory_that_is_not_shared_is_reported(tmp_path, side):
    """living、world 的功能都在插件目录里；目录外多出来一个模块，几个插件都 import 它，就是绕开
    服务的一条私下通道。要共用就放进共享模块，或者做成服务。"""
    root = _tree(tmp_path, {f"{side}/rounds.py": "", f"{side}/a/tools.py": ""})

    problems = find_violations(root)

    assert _sites(problems) == [f"app/{side}/rounds.py"]
    assert "not in a plugin directory" in problems[0]


@pytest.mark.parametrize("side", ["living", "world"])
def test_a_plugin_importing_a_module_outside_the_plugin_directories_is_reported(tmp_path, side):
    root = _tree(
        tmp_path, {f"{side}/rounds.py": "", f"{side}/a/tools.py": f"from app.{side}.rounds import x\n"}
    )

    sites = _sites(find_violations(root))

    assert f"app/{side}/a/tools.py:1" in sites


@pytest.mark.parametrize("side", ["living", "world"])
def test_its_own_directory_the_shared_modules_and_the_rest_of_the_app_are_allowed(tmp_path, side):
    shared = " ".join(sorted(SIDES[side].shared))
    root = _tree(
        tmp_path,
        {
            f"{side}/a/tools.py": "\n".join(
                [
                    f"import app.{side}",
                    f"from app import {side}",
                    f"from app.{side}.a import store",
                    f"from app.{side}.a.store import thing",
                    "from . import store",
                    "from .store import thing",
                    *(f"from app.{side} import {name}" for name in shared.split()),
                    *(f"from app.{side}.{name} import x" for name in shared.split()),
                    *(f"from ..{name} import x" for name in shared.split()),
                    "from app.host import Context, Plugin",
                    "from app.data.persist import fetch_all",
                    "from app.agent.round import current_round",
                ]
            ),
            f"{side}/services.py": f"from app.{side} import common\n" if side == "living" else "",
        },
    )

    assert find_violations(root) == []


def test_words_in_docstrings_and_comments_are_not_imports(tmp_path):
    root = _tree(
        tmp_path,
        {
            "living/a/tools.py": '"""see app.living.b.store"""\n# from app.living.b import store\n',
            "world/a/tools.py": "NOTE = 'app.world.b.store'\n",
        },
    )

    assert find_violations(root) == []


def test_look_alike_names_are_not_another_plugin(tmp_path):
    root = _tree(
        tmp_path,
        {"living/a/tools.py": "import app.living_room\nfrom app.worldwide import b\n"},
    )

    assert find_violations(root) == []


def test_a_directory_with_no_python_file_is_not_a_plugin(tmp_path):
    """git 不留空目录；本地删掉一个插件之后剩下的 ``__pycache__`` 不是插件，也不是越界。"""
    root = _tree(tmp_path, {})
    (root / "world" / "sources" / "__pycache__").mkdir(parents=True)
    (root / "world" / "sources" / "__pycache__" / "x.cpython-312.pyc").write_bytes(b"")

    assert find_violations(root) == []


@pytest.mark.parametrize("gone", ["living", "world", "plugins"])
def test_a_side_that_no_longer_exists_is_reported(tmp_path, gone):
    """规则名单上的目录搬走了，规则就什么也不扫、一直通过；所以名单上有而目录没有也算违规。"""
    root = _tree(tmp_path, {})
    for path in sorted((root / gone).rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    (root / gone).rmdir()

    problems = find_violations(root)

    assert _sites(problems) == [f"app/{gone}"]
    assert "does not exist" in problems[0]


@pytest.mark.parametrize(("side", "shared"), [("living", "common"), ("world", "volume")])
def test_a_shared_module_that_no_longer_exists_is_reported(tmp_path, side, shared):
    root = _tree(tmp_path, {}, without=(f"{side}/{shared}.py",))

    problems = find_violations(root)

    assert _sites(problems) == [f"app/{side}/{shared}.py"]
    assert "does not exist" in problems[0]


def test_the_real_code_tree_is_clean():
    assert find_violations(Path(app_pkg.__file__).parent) == []


def test_the_real_plugins_are_the_ones_the_rule_sees():
    """名单之外的东西规则看不见；这里把它看见的插件和真实目录对一遍，免得哪天名单和目录对不上、
    规则绿着什么也没扫。"""
    from scripts.check_plugin_boundaries import plugins_of

    root = Path(app_pkg.__file__).parent

    assert plugins_of(root, "living") == sorted(
        d.name for d in (root / "living").iterdir() if d.is_dir() and any(d.rglob("*.py"))
    )
    assert "life" in plugins_of(root, "living")
    assert {"core", "records", "told"} <= set(plugins_of(root, "world"))
    assert plugins_of(root, "plugins") == ["operator", "ops", "skills"]


def test_the_command_fails_with_the_file_and_line_and_passes_when_clean(tmp_path):
    dirty = _tree(tmp_path / "dirty", {"world/a/tools.py": "\n\nfrom app.world.b import store\n"})
    clean = _tree(tmp_path / "clean", {"world/a/tools.py": "from app.world.services import X\n"})

    def run(root: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "scripts.check_plugin_boundaries", str(root)],
            cwd=SERVICE_DIR,
            capture_output=True,
            text=True,
        )

    failed = run(dirty)
    passed = run(clean)

    assert failed.returncode == 1, failed.stdout + failed.stderr
    assert "app/world/a/tools.py:3" in failed.stdout
    assert passed.returncode == 0, passed.stdout + passed.stderr
    real = run(SERVICE_DIR / "app")
    assert real.returncode == 0, real.stdout + real.stderr
