"""The host never imports living or world (design risk 12).

The world process loads only the plugins its manifest names; if the host pulled in the living
code, every world process would carry it again. Checked twice: the import statements in
``app/host`` (lazy ones inside functions included), and what ``import app.host`` actually loads
in a fresh interpreter (imports of imports included).
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

from scripts.check_world_life_imports import imported_names, module_name

APP = Path(__file__).resolve().parents[2] / "app"
FORBIDDEN = ("app.living", "app.world", "app.wiring", "app.plugins")


def _forbidden(name: str) -> bool:
    return any(name == f or name.startswith(f + ".") for f in FORBIDDEN)


def test_no_module_in_app_host_imports_living_world_or_the_old_wiring():
    found: list[str] = []
    for path in sorted((APP / "host").rglob("*.py")):
        parts, is_package = module_name(APP, path)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for lineno, candidates in imported_names(tree, parts, is_package):
            found += [f"{path.name}:{lineno} {c}" for c in candidates if _forbidden(c)]

    assert found == []


def test_importing_the_host_loads_none_of_them():
    probe = (
        "import json, sys; import app.host; "
        "print(json.dumps(sorted(m for m in sys.modules if m.startswith('app.'))))"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=APP.parent,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    loaded = json.loads(out.stdout.strip().splitlines()[-1])

    assert [m for m in loaded if _forbidden(m)] == []
