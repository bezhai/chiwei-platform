"""Gap 13: business modules must delegate database access to typed queries."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_APP_ROOT = Path(__file__).parents[3] / "app"
_BUSINESS_MODULES = (
    _APP_ROOT / "life" / "pages.py",
    _APP_ROOT / "life" / "persona_chain.py",
    _APP_ROOT / "memory" / "identity_registry.py",
)
_SESSION_CALLS = {"auto_tx", "current_session", "get_session"}
_SQL_BUILDERS = {"select", "text"}


def _database_boundary_violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    violations: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported = {alias.name for alias in node.names}
            if node.module == "app.data.session":
                violations.append(f"line {node.lineno}: imports app.data.session")
            if node.module == "app.runtime.db" and imported & _SESSION_CALLS:
                names = sorted(imported & _SESSION_CALLS)
                violations.append(f"line {node.lineno}: imports {names} from runtime.db")
            if node.module in {"sqlalchemy", "sqlalchemy.future"}:
                names = sorted(imported & _SQL_BUILDERS)
                if names:
                    violations.append(
                        f"line {node.lineno}: builds database statements with {names}"
                    )

        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id in _SESSION_CALLS:
            violations.append(f"line {node.lineno}: calls {node.func.id}()")
        if isinstance(node.func, ast.Attribute) and node.func.attr == "execute":
            violations.append(f"line {node.lineno}: calls .execute()")

    return violations


@pytest.mark.parametrize("path", _BUSINESS_MODULES, ids=lambda path: path.stem)
def test_business_module_has_no_database_session_or_statement_access(path: Path):
    """Business owns behavior; app.data.queries owns SQL and sessions."""
    assert not (violations := _database_boundary_violations(path)), (
        f"{path.relative_to(_APP_ROOT.parent)} crosses the Gap 13 boundary:\n"
        + "\n".join(violations)
    )
