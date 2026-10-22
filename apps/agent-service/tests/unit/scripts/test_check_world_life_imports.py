"""CI 规则：``app/world/`` 与 ``app/living/`` 互不 import（双向），两个 App 的清单也一样：
``app/manifests/world.py`` 不 import life，``app/manifests/agent_service.py`` 不 import world。

规则本身在 ``scripts/check_world_life_imports.py``，CI（``.github/workflows/grep-gate.yml``）
直接跑它。这里用临时目录造出每一种写法的违规，证明它真的会拦；规则里列的目录或文件不在了也
报出来，不静默跳过；再对真实代码树跑一遍，证明现在是干净的。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import app as app_pkg
from scripts.check_world_life_imports import find_violations

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check_world_life_imports.py"


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "app"
    for rel, content in {
        "__init__.py": "",
        "world/__init__.py": "",
        "living/__init__.py": "",
        "manifests/__init__.py": "",
        "manifests/world.py": "",
        "manifests/agent_service.py": "",
        **files,
    }.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


WORLD_REACHING_INTO_LIFE = [
    "import app.living.moment",
    "import app.living",
    "from app.living import moment",
    "from app.living.moment import LifeMoment",
    "from app import living",
    "from .. import living",
    "from ..living import moment",
    "from ..living.moment import LifeMoment",
    "import importlib\nimportlib.import_module('app.living.moment')",
    "from importlib import import_module\nimport_module('app.living')",
    "__import__('app.living.moment')",
    "def later():\n    from app.living import moment\n    return moment",
    # 动态 import 的相对名字、换了名字的 import_module，按字面量解析成完整模块名再判。
    "import importlib\nimportlib.import_module('.living', 'app')",
    "import importlib\nimportlib.import_module('..living.moment', package=__package__)",
    "from importlib import import_module as load\nload('app.living')",
    "import importlib as il\nil.import_module('.living.moment', 'app')",
    "__import__('living.moment', globals(), locals(), [], 2)",
    "__import__('app', fromlist=['living'])",
]

LIFE_REACHING_INTO_WORLD = [
    "import app.world.engine",
    "from app.world import engine",
    "from app import world",
    "from ..world.engine import run",
    "import importlib\nimportlib.import_module('app.world')",
]


@pytest.mark.parametrize("source", WORLD_REACHING_INTO_LIFE)
def test_world_importing_life_is_caught(tmp_path, source):
    root = _tree(tmp_path, {"world/engine.py": source})

    violations = find_violations(root)

    assert len(violations) == 1, violations
    assert "world/engine.py" in violations[0]


@pytest.mark.parametrize("source", LIFE_REACHING_INTO_WORLD)
def test_life_importing_world_is_caught(tmp_path, source):
    root = _tree(tmp_path, {"living/moment.py": source})

    violations = find_violations(root)

    assert len(violations) == 1, violations
    assert "living/moment.py" in violations[0]


def test_deeper_packages_on_either_side_are_covered(tmp_path):
    root = _tree(
        tmp_path,
        {
            "world/agents/__init__.py": "",
            "world/agents/npc.py": "from ...living import moment",
            "living/tools/__init__.py": "",
            "living/tools/look.py": "from app.world.agents import npc",
        },
    )

    violations = find_violations(root)

    assert len(violations) == 2
    assert any("world/agents/npc.py" in v for v in violations)
    assert any("living/tools/look.py" in v for v in violations)


def test_words_in_docstrings_and_comments_are_not_imports(tmp_path):
    root = _tree(
        tmp_path,
        {
            "living/moment.py": '"""旧引擎（app/world、app.world.engine）已经删掉。"""\n# app.world\n',
            "world/engine.py": "NOTE = 'see app.living.moment for the old design'\n",
        },
    )
    assert find_violations(root) == []


def test_a_dynamic_import_that_cannot_be_resolved_statically_is_not_an_import(tmp_path):
    """名字不是字面量、相对名字没有 package、相对得超出顶层包：运行时才知道（或者运行时就报错），
    规则不猜，也不因此出错退出。"""
    root = _tree(
        tmp_path,
        {
            "world/engine.py": "\n".join(
                [
                    "import importlib",
                    "importlib.import_module(name)",
                    "importlib.import_module('.living')",
                    "importlib.import_module('....living', 'app')",
                    "importlib.import_module('.living', package=somewhere)",
                    "__import__('living', globals(), locals(), [], level)",
                    "__import__('living', level=True)",
                ]
            )
        },
    )

    assert find_violations(root) == []


def test_look_alike_names_are_not_the_other_side(tmp_path):
    root = _tree(
        tmp_path,
        {
            "world/engine.py": "import app.living_room\nfrom app.worldwide import x\n",
            "living/moment.py": "from app.worlds import y\nfrom .world_view import z\n",
        },
    )
    assert find_violations(root) == []


@pytest.mark.parametrize("source", WORLD_REACHING_INTO_LIFE)
def test_the_world_manifest_importing_life_is_caught(tmp_path, source):
    """world 的进程按 world 的清单起插件：清单带进 life 的代码，world 的进程就背上了 life。"""
    root = _tree(tmp_path, {"manifests/world.py": source})

    violations = find_violations(root)

    assert len(violations) == 1, violations
    assert "manifests/world.py" in violations[0]


@pytest.mark.parametrize("source", LIFE_REACHING_INTO_WORLD)
def test_the_agent_service_manifest_importing_world_is_caught(tmp_path, source):
    root = _tree(tmp_path, {"manifests/agent_service.py": source})

    violations = find_violations(root)

    assert len(violations) == 1, violations
    assert "manifests/agent_service.py" in violations[0]


@pytest.mark.parametrize(
    "gone", ["world", "living", "manifests/world.py", "manifests/agent_service.py"]
)
def test_a_listed_place_that_no_longer_exists_is_reported(tmp_path, gone):
    """规则列的目录或文件搬走了，规则就什么也不扫、一直通过；所以列了而不存在的也算违规。"""
    root = _tree(tmp_path, {})
    target = root / gone
    if target.is_dir():
        for path in sorted(target.rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        target.rmdir()
    else:
        target.unlink()

    violations = find_violations(root)

    assert len(violations) == 1, violations
    assert f"app/{gone}" in violations[0]
    assert "does not exist" in violations[0]


def test_everyone_else_may_import_both(tmp_path):
    """规则只管 world、life 和它们各自的插件；别的插件、别的包不在这条规则里。"""
    root = _tree(
        tmp_path,
        {
            "plugins/__init__.py": "",
            "plugins/ops.py": "import app.living.moment\nimport app.world.engine\n",
            "data/__init__.py": "",
            "data/both.py": "from app import living, world\n",
        },
    )
    assert find_violations(root) == []


def test_the_real_code_tree_is_clean():
    assert find_violations(Path(app_pkg.__file__).parent) == []


def test_the_command_fails_on_a_violation_and_passes_when_clean(tmp_path):
    dirty = _tree(tmp_path / "dirty", {"world/engine.py": "from app.living import moment"})
    clean = _tree(tmp_path / "clean", {"world/engine.py": "import app.messaging"})

    failed = subprocess.run(
        [sys.executable, str(SCRIPT), str(dirty)], capture_output=True, text=True
    )
    passed = subprocess.run(
        [sys.executable, str(SCRIPT), str(clean)], capture_output=True, text=True
    )

    assert failed.returncode == 1
    assert "world/engine.py:1" in failed.stdout
    assert passed.returncode == 0, passed.stdout + passed.stderr
