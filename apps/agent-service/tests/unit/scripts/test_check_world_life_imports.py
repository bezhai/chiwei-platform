"""CI 规则：``app/world/`` 与 ``app/living/`` 互不 import（双向）。

规则本身在 ``scripts/check_world_life_imports.py``，CI（``.github/workflows/grep-gate.yml``）
直接跑它。这里用临时目录造出每一种写法的违规，证明它真的会拦；再对真实代码树跑一遍，
证明现在是干净的。
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
    "from app.living.moment import LifeMomentTick",
    "from app import living",
    "from .. import living",
    "from ..living import moment",
    "from ..living.moment import LifeMomentTick",
    "import importlib\nimportlib.import_module('app.living.moment')",
    "from importlib import import_module\nimport_module('app.living')",
    "__import__('app.living.moment')",
    "def later():\n    from app.living import moment\n    return moment",
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


def test_look_alike_names_are_not_the_other_side(tmp_path):
    root = _tree(
        tmp_path,
        {
            "world/engine.py": "import app.living_room\nfrom app.worldwide import x\n",
            "living/moment.py": "from app.worlds import y\nfrom .world_view import z\n",
        },
    )
    assert find_violations(root) == []


def test_everyone_else_may_import_both(tmp_path):
    root = _tree(
        tmp_path,
        {
            "wiring/__init__.py": "",
            "wiring/both.py": "import app.living.moment\nimport app.world.engine\n",
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
