"""CI 规则：``app/`` 下的包之间不许出现新的环。

规则本身在 ``scripts/check_package_cycles.py``，CI（``.github/workflows/grep-gate.yml``）直接跑它。
这里用临时目录造出每一种写法的环，证明它会拦；造出名单上的环消失、缩小的情形，证明名单
只能缩；再对真实代码树跑一遍，证明现在只剩名单上那一组。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import app as app_pkg
from scripts.check_package_cycles import (
    KNOWN_CYCLES,
    cycle_problems,
    package_graph,
    strongly_connected_groups,
)

SERVICE_DIR = Path(__file__).resolve().parents[3]


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "app"
    for rel, content in {
        "__init__.py": "",
        "a/__init__.py": "",
        "b/__init__.py": "",
        "c/__init__.py": "",
        **files,
    }.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def _groups(root: Path) -> list[frozenset[str]]:
    return strongly_connected_groups(package_graph(root))


# 每一种写法都是 a 指向 b 的一条边；b 那边固定 import a，所以只要这条边被认出来就成环。
A_REACHING_INTO_B = [
    "import app.b.x",
    "import app.b",
    "from app.b import x",
    "from app.b.x import thing",
    "from app import b",
    "from .. import b",
    "from ..b import x",
    "from ..b.x import thing",
    "import importlib\nimportlib.import_module('app.b.x')",
    "from importlib import import_module\nimport_module('app.b')",
    "__import__('app.b.x')",
    "def later():\n    from app.b import x\n    return x",
    "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from app.b.x import thing",
]


@pytest.mark.parametrize("source", A_REACHING_INTO_B)
def test_every_import_form_is_an_edge(tmp_path, source):
    root = _tree(
        tmp_path,
        {"a/mod.py": source, "b/x.py": "from app.a import mod\nthing = 1\n"},
    )

    assert _groups(root) == [frozenset({"a", "b"})]
    problems = cycle_problems(root, known=[])
    assert len(problems) == 1, problems
    assert "a, b" in problems[0]


def test_words_in_docstrings_and_comments_are_not_imports(tmp_path):
    root = _tree(
        tmp_path,
        {
            "a/mod.py": '"""see app.b.x"""\n# from app.b import x\nNOTE = "app.b"\n',
            "b/x.py": "from app.a import mod\n",
        },
    )

    assert _groups(root) == []
    assert cycle_problems(root, known=[]) == []


def test_one_direction_is_not_a_cycle(tmp_path):
    root = _tree(tmp_path, {"a/mod.py": "from app.b import x\n", "b/x.py": ""})

    assert package_graph(root)["a"].keys() == {"b"}
    assert _groups(root) == []


def test_imports_inside_one_package_are_not_a_cycle(tmp_path):
    root = _tree(
        tmp_path,
        {"a/one.py": "from app.a import two\n", "a/two.py": "from app.a import one\n"},
    )

    assert _groups(root) == []


def test_a_cycle_through_three_packages_is_one_group(tmp_path):
    root = _tree(
        tmp_path,
        {
            "a/mod.py": "from app.b import x\n",
            "b/x.py": "from app.c import y\n",
            "c/y.py": "from app.a import mod\n",
        },
    )

    assert _groups(root) == [frozenset({"a", "b", "c"})]


def test_a_top_level_module_is_a_package_of_its_own(tmp_path):
    root = _tree(
        tmp_path,
        {"main.py": "from app.a import mod\n", "a/mod.py": "import app.main\n"},
    )

    assert _groups(root) == [frozenset({"a", "main"})]


def test_a_listed_cycle_passes(tmp_path):
    root = _tree(
        tmp_path,
        {"a/mod.py": "from app.b import x\n", "b/x.py": "from app.a import mod\n"},
    )

    assert cycle_problems(root, known=[frozenset({"a", "b"})]) == []


def test_a_listed_cycle_that_disappeared_fails(tmp_path):
    root = _tree(tmp_path, {"a/mod.py": "from app.b import x\n", "b/x.py": ""})

    problems = cycle_problems(root, known=[frozenset({"a", "b"})])

    assert len(problems) == 1, problems
    assert "a, b" in problems[0]
    assert "KNOWN_CYCLES" in problems[0]


def test_a_listed_cycle_that_shrank_fails_as_both(tmp_path):
    root = _tree(
        tmp_path,
        {
            "a/mod.py": "from app.b import x\n",
            "b/x.py": "from app.a import mod\n",
            "c/y.py": "from app.a import mod\n",
        },
    )

    problems = cycle_problems(root, known=[frozenset({"a", "b", "c"})])

    assert len(problems) == 2, problems
    assert any("a, b, c" in p for p in problems)
    assert any("a, b" in p and "a, b, c" not in p for p in problems)


def test_an_unlisted_cycle_names_the_imports_that_close_it(tmp_path):
    root = _tree(
        tmp_path,
        {"a/mod.py": "\n\nfrom app.b import x\n", "b/x.py": "from app.a import mod\n"},
    )

    (problem,) = cycle_problems(root, known=[])

    assert "app/a/mod.py:3" in problem
    assert "app/b/x.py:1" in problem


def test_the_real_code_tree_has_only_the_listed_cycles():
    root = Path(app_pkg.__file__).parent

    assert cycle_problems(root, known=KNOWN_CYCLES) == []
    groups = _groups(root)
    assert groups == [frozenset({"agent", "capabilities", "living", "memory", "skills"})]
    in_a_cycle = set().union(*groups)
    assert {"runtime", "data", "messaging"}.isdisjoint(in_a_cycle)


def test_the_command_fails_on_a_new_cycle_and_passes_when_clean(tmp_path):
    dirty = _tree(
        tmp_path / "dirty",
        {"a/mod.py": "from app.b import x\n", "b/x.py": "from app.a import mod\n"},
    )
    clean = _tree(tmp_path / "clean", {"a/mod.py": "from app.b import x\n", "b/x.py": ""})

    def run(root: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "scripts.check_package_cycles", str(root)],
            cwd=SERVICE_DIR,
            capture_output=True,
            text=True,
        )

    failed = run(dirty)
    assert failed.returncode == 1, failed.stdout + failed.stderr
    assert "a, b" in failed.stdout

    # 临时树里没有名单上那一组，所以"干净"的树也会因为名单上的环消失而失败；
    # 真实代码树才是干净的那一棵。
    passed = run(SERVICE_DIR / "app")
    assert passed.returncode == 0, passed.stdout + passed.stderr
    assert run(clean).returncode == 1
