"""别人的经历和位置，life 里没有一条路读得到。

谁察觉到谁做了什么，由 world 判断后告诉她（第二期）；传到她这里的只有收件箱里那一段
（:mod:`app.living.received`）。life 这边每个人只读她自己的那几行：她自己做过、说过的
（:class:`~app.living.records.Happening`），她自己在哪、在做什么
（:class:`~app.living.records.Whereabouts`）。

这条用源码扫描守住：``app`` 下每一段碰这两张表的 SQL，都得按那一行属于谁的那一列等值筛到
一个人。新写一条不筛人的查询（按位置把别人的经历分给她、读全员的位置），这里当场红。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

_APP = Path(__file__).resolve().parents[2] / "app"

# 每张表里，哪一列说的是"这一行是谁的"。
_OWNER_COLUMN = {"Happening": "actor", "Whereabouts": "persona_id"}

# 这几处一定在读这两张表；一处都扫不到说明扫描本身失效了（比如表名换了绑定方式），
# 那时候这条门禁绿着等于没有。
_KNOWN_READERS = {
    "app/living/happening.py",
    "app/living/outgoing.py",
    "app/living/phone.py",
    "app/living/snapshot.py",
    "app/living/whereabouts.py",
}


def _table_bindings(tree: ast.Module) -> dict[str, str]:
    """模块里 ``X = _table_name(Happening)`` 这样的绑定：变量名 -> 类名。"""
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)):
            continue
        call = node.value
        if not (
            isinstance(call.func, ast.Name)
            and call.func.id == "_table_name"
            and len(call.args) == 1
            and isinstance(call.args[0], ast.Name)
            and call.args[0].id in _OWNER_COLUMN
        ):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                found[target.id] = call.args[0].id
    return found


def _queries() -> list[tuple[str, int, str, str]]:
    """``app`` 下每一处在 SQL 里用到这两张表：(文件, 行号, 类名, 表名后面那一段 SQL)。

    "后面那一段"到下一次用到这两张表之前为止，所以一条语句里两张表各管各的条件
    （:func:`app.living.outgoing._starting_mark` 一条语句里读了两张表）。相邻的几段 f-string
    在解析时已经拼成一个节点。
    """
    found: list[tuple[str, int, str, str]] = []
    for path in sorted(_APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        tables = _table_bindings(tree)
        if not tables:
            continue
        where = str(path.relative_to(_APP.parent))
        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr):
                continue
            parts = node.values
            for i, part in enumerate(parts):
                if not _names_a_table(part, tables):
                    continue
                tail: list[str] = []
                for later in parts[i + 1 :]:
                    if _names_a_table(later, tables):
                        break
                    if isinstance(later, ast.Constant):
                        tail.append(str(later.value))
                found.append(
                    (where, node.lineno, tables[part.value.id], "".join(tail))
                )
    return found


def _names_a_table(part: ast.expr, tables: dict[str, str]) -> bool:
    return (
        isinstance(part, ast.FormattedValue)
        and isinstance(part.value, ast.Name)
        and part.value.id in tables
    )


def _narrowed_by(column: str, sql: str) -> bool:
    """``sql`` 里有没有"这一列等于某个绑定参数"：``actor = :persona_id``、``m.actor = :p``。

    列名前面不能是 ``:``：``:actor = :actor`` 是参数跟自己比，恒真，什么都没筛。
    """
    return re.search(rf"(?<![:\w.])(?:\w+\.)?{column}\s*=\s*:\w", sql) is not None


def test_the_check_tells_a_real_filter_from_a_parameter_compared_with_itself():
    assert _narrowed_by("actor", "WHERE lane = :lane AND actor = :persona_id")
    assert _narrowed_by("actor", "WHERE m.lane = :lane AND m.actor = :p")
    assert not _narrowed_by("actor", "WHERE lane = :lane AND :actor = :actor")
    assert not _narrowed_by("actor", "WHERE lane = :lane ORDER BY actor")
    assert not _narrowed_by("persona_id", "WHERE lane = :lane ORDER BY persona_id, seq")


def test_every_query_on_her_records_is_narrowed_to_one_person():
    queries = _queries()

    assert {where for where, *_ in queries} >= _KNOWN_READERS, (
        "扫描没找到已知会读这两张表的那几处 —— 扫描本身失效了，这条门禁绿着等于没有"
    )
    unnarrowed = [
        f"{where}:{line} 读 {table} 没按 {_OWNER_COLUMN[table]} 筛到一个人：{sql.strip()}"
        for where, line, table, sql in queries
        if not _narrowed_by(_OWNER_COLUMN[table], sql)
    ]
    assert unnarrowed == [], "\n".join(unnarrowed)
