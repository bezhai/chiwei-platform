"""CI 规则：业务代码只用框架公开的东西；跨包不 import 下划线开头的名字。

规则本身在 ``scripts/check_framework_private.py``，CI（``.github/workflows/grep-gate.yml``）直接跑
它。这里用临时目录造出每一种写法的违规，证明它报出文件和行；造出允许的写法（包导出的名字、同一个
包里的下划线名字、框架自己用私有模块），证明它们不算；再对真实代码树跑一遍，证明现在是干净的。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import app as app_pkg
from scripts.check_framework_private import SCOPE, find_violations

SERVICE_DIR = Path(__file__).resolve().parents[3]


def _tree(tmp_path: Path, files: dict[str, str], *, without: tuple[str, ...] = ()) -> Path:
    base = {
        "__init__.py": "",
        **{f"{d}/__init__.py": "" for d in SCOPE},
        "host/__init__.py": "from app.host.host import Context\n",
        "host/host.py": "class Context: ...\n",
        "host/turns.py": "",
        "host/clock.py": "",
        "host/manifest.py": "",
        "agent/__init__.py": "",
        "agent/core.py": "def _loop(): ...\nclass Agent: ...\n",
        "agent/continuity.py": "",
        "agent/contracts.py": "",
        "messaging/__init__.py": "",
        "messaging/receiving.py": "",
        "data/__init__.py": "",
        "data/session.py": "",
        "data/persist.py": "",
    }
    root = tmp_path / "app"
    for rel, content in {**base, **files}.items():
        if rel.split("/")[0] in without:
            continue
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def _sites(problems: list[str]) -> list[str]:
    return [p.split(": ")[0] for p in problems]


REACHING_INTO_THE_FRAMEWORK = [
    "from app.host.host import Context",
    "from app.host.turns import Turns",
    "from app.host.manifest import App",
    "import app.host.clock",
    "from app.host import host",
    "from app.agent.continuity import TrimPolicy",
    "from app.agent import continuity",
    "from app.messaging.receiving import DeclareInbox",
    "from app.messaging import receiving",
    "from app.data.session import get_session",
    "import importlib\nimportlib.import_module('app.messaging.receiving')",
    "def later():\n    from app.host.turns import Turns\n    return Turns",
]


@pytest.mark.parametrize("source", REACHING_INTO_THE_FRAMEWORK)
def test_business_code_importing_a_framework_module_is_reported_with_its_file_and_line(
    tmp_path, source
):
    root = _tree(tmp_path, {"world/core/rounds.py": "\n\n" + source + "\n"})

    problems = find_violations(root)

    assert len(problems) == 1, problems
    path, line = _sites(problems)[0].rsplit(":", 1)
    assert path == "app/world/core/rounds.py"
    assert int(line) in range(3, 3 + len(source.split("\n")))
    assert "framework module" in problems[0]


@pytest.mark.parametrize("place", SCOPE)
def test_every_business_place_is_covered(tmp_path, place):
    root = _tree(tmp_path, {f"{place}/x.py": "from app.host.turns import Turns\n"})

    assert _sites(find_violations(root)) == [f"app/{place}/x.py:1"]


PRIVATE_NAMES_OF_ANOTHER_PACKAGE = [
    "from app.agent.core import _loop",
    "from app.agent.tools._common import tool_error",
    "import app.capabilities._errors",
    "from app.capabilities import _errors",
    "import importlib\nimportlib.import_module('app.agent._private')",
]


@pytest.mark.parametrize("source", PRIVATE_NAMES_OF_ANOTHER_PACKAGE)
@pytest.mark.parametrize("importer", ["living/a/tools.py", "nodes/admin.py", "host/assembly.py"])
def test_a_private_name_of_another_package_is_reported_anywhere(tmp_path, source, importer):
    """Framework code included: the underscore is the other package saying the name is its own."""
    root = _tree(tmp_path, {importer: source + "\n"})

    problems = find_violations(root)

    assert len(problems) == 1, problems
    assert problems[0].startswith(f"app/{importer}:")
    assert "private name of another package" in problems[0]


def test_what_the_packages_export_and_private_names_within_a_package_are_allowed(tmp_path):
    root = _tree(
        tmp_path,
        {
            "world/core/rounds.py": "\n".join(
                [
                    '"""See app.messaging.receiving and app.host.turns."""',
                    "import app.host",
                    "from app.host import Context, Turns",
                    "from app.agent.core import Agent",
                    "from app.agent.contracts import TrimPolicy",
                    "from app.data.persist import fetch_all",
                    "from app.world.core._local import helper",
                    "from ._local import helper",
                ]
            ),
            "host/assembly.py": "from app.host.host import Context\nfrom app.host._x import y\n",
            "host/host.py": "from app.messaging.receiving import inbox\nfrom app.data.session import s\n",
            "agent/round.py": "from app.agent.core import _loop\n",
        },
    )

    assert find_violations(root) == []


@pytest.mark.parametrize("gone", SCOPE)
def test_a_listed_business_place_that_does_not_exist_is_reported(tmp_path, gone):
    root = _tree(tmp_path, {}, without=(gone,))

    problems = find_violations(root)

    assert _sites(problems) == [f"app/{gone}"]
    assert "does not exist" in problems[0]


def test_the_real_code_tree_is_clean():
    assert find_violations(Path(app_pkg.__file__).parent) == []


def test_the_command_fails_with_the_file_and_line_and_passes_when_clean(tmp_path):
    dirty = _tree(tmp_path / "dirty", {"manifests/world.py": "\n\nfrom app.host.manifest import App\n"})
    clean = _tree(tmp_path / "clean", {"manifests/world.py": "from app.host import App\n"})

    def run(root: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "scripts.check_framework_private", str(root)],
            cwd=SERVICE_DIR,
            capture_output=True,
            text=True,
        )

    failed = run(dirty)
    passed = run(clean)

    assert failed.returncode == 1, failed.stdout + failed.stderr
    assert "app/manifests/world.py:3" in failed.stdout
    assert passed.returncode == 0, passed.stdout + passed.stderr
    real = run(SERVICE_DIR / "app")
    assert real.returncode == 0, real.stdout + real.stderr
