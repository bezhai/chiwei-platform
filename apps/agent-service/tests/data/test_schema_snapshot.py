"""The tables, columns, types and indexes each app's Data classes build, pinned per app.

The migrator derives a table's name from its Data class (``data_`` plus the class name in
snake_case; the module is not part of it) and each index name from the table. So moving a Data
class to another module keeps its table, while renaming the class, or changing how names are
derived, creates a new empty table next to the old one and leaves the old rows unread.

This test starts each app the way production does, in a fresh process
(:func:`tests.hosting.in_a_fresh_process`, so only that app's code is loaded). It then plans the
migration of every registered Data class against an empty database and compares the statements
with the golden file ``schema/<app>.sql``. The statements are ordered by table name, not by
module, so moving a class between modules leaves the file unchanged.

The golden files were written on HEAD a36138b6, before storage moved out of ``app.runtime``. A
difference is either intended, in which case the commit rewrites the file and says why, or a
regression. Together with ``tests/apps/test_surface.py`` (the tables each app creates at startup)
and the replay (the rows each round writes, by table), this shows that a refactor kept every
table name.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.hosting import in_a_fresh_process

_GOLDEN = Path(__file__).parent / "schema"

_PLAN = """
import json
from app.runtime.data import DATA_REGISTRY
from app.runtime.migrator import _table_name, plan_migration
plan = plan_migration(sorted(DATA_REGISTRY, key=_table_name), existing_schema={})
print(json.dumps([stmt.sql for stmt in plan.stmts]))
"""


def planned_schema(app_name: str) -> str:
    """The DDL ``app_name``'s Data classes plan on an empty database, one statement per line."""
    out = in_a_fresh_process(app_name, _PLAN)
    return "".join(f"{sql};\n" for sql in json.loads(out.strip().splitlines()[-1]))


@pytest.mark.parametrize("app_name", ["agent-service", "world"])
def test_each_app_plans_the_schema_it_planned_before(app_name):
    golden = (_GOLDEN / f"{app_name}.sql").read_text(encoding="utf-8")

    assert planned_schema(app_name) == golden
