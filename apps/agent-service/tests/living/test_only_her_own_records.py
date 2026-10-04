"""别人的经历和位置，life 里没有一条路读得到。

谁察觉到谁做了什么，由 world 判断后告诉她（第二期）；传到她这里的只有收件箱里那一段
（:mod:`app.living.received`）。life 这边每个人只读她自己的那几行：她自己做过、说过的
（:class:`~app.living.records.Happening`），她自己在哪、在做什么
（:class:`~app.living.records.Whereabouts`）。

这条用源码扫描守住。``app`` 下凡是碰到这两张表的地方，扫描都得认得出它是哪一种写法，
认不出来的一律算违规，而不是跳过——跳过的写法就是一条没人看着的路：

  * 表名只能用 :func:`~app.runtime.migrator._table_name` 从类上取。取到的要么直接赋给一个
    名字，要么直接嵌进 f-string 写的 SQL；那个名字之后也只能嵌进 f-string 写的 SQL，
    不能再被改写。拼接、``.format()``、``%``、换个名字再用、源码里直接写表名，
    都算认不出来。
  * 嵌进 SQL 的那一处，表名后面紧跟（可选的别名和）``WHERE``；``WHERE`` 顶层没有 ``OR``，
    并且有一条顶层 ``AND`` 条件恰好是"这一行属于谁的那一列 = 一个绑定参数"。``WHERE`` 里
    夹着别的 f-string 值、注释、引号，算认不出来。
  * 类本身只能当类型注解、构造一行、取 ``model_fields``，或者交给 :data:`_WRITERS` 里
    列出来的写入口；交给别的函数（比如 :func:`app.runtime.query.query`）算认不出来。

名字按 import 解析到它定义在哪个模块，所以 ``app.world`` 自己的 ``Happening`` 不算，从别的
模块 import 进来的表名照样跟着查。``type(x)``、``getattr(m, "...")`` 这类动态写法扫描跟不到，
它守的是照常写出来的代码。
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import NamedTuple

import pytest

from app.living.records import Happening, Whereabouts
from app.living.serial import append_in_commit_order
from app.runtime.migrator import _table_name

_APP = Path(__file__).resolve().parents[2] / "app"


def _qualified(obj: Callable | type) -> str:
    return f"{obj.__module__}.{obj.__qualname__}"


# 每张表里，哪一列说的是"这一行是谁的"。键是类定义所在的全名。
_OWNER_COLUMN = {_qualified(Happening): "actor", _qualified(Whereabouts): "persona_id"}
_TABLE_NAME_FN = _qualified(_table_name)
_LITERAL_TABLE = re.compile(
    "|".join(rf"\b{re.escape(_table_name(cls))}\b" for cls in (Happening, Whereabouts)),
    re.IGNORECASE,
)

# 把类交给这些函数是写一行，不是读。只有列在这里的才放行，每一项写明理由。
_WRITERS = {
    _qualified(append_in_commit_order): (
        "落她自己的一行。里面的读只有两处：按 scope 取 MAX(seq)，拿到的是一个号，"
        "不是谁的内容；重放撞上自然键时取回自然键相同的那一行，也就是她这一行早先落下的那份"
    ),
}

# 这几处一定在读这两张表；一处都扫不到说明扫描本身失效了（比如表名换了绑定方式），
# 那时候这条门禁绿着等于没有。
_KNOWN_READERS = {
    "app/living/happening.py",
    "app/living/outgoing.py",
    "app/living/phone.py",
    "app/living/snapshot.py",
    "app/living/whereabouts.py",
}


def _app_modules() -> dict[str, str]:
    return {
        str(path.relative_to(_APP.parent)): path.read_text(encoding="utf-8")
        for path in sorted(_APP.rglob("*.py"))
    }


class _Scan(NamedTuple):
    # 有 SQL 读这两张表的文件
    readers: set[str]
    # "文件:行号 哪里不对"
    violations: list[str]


class _Module(NamedTuple):
    where: str
    tree: ast.Module
    # 表达式指向的全名，比如 ``records.Happening`` -> app.living.records.Happening
    resolve: Callable[[ast.expr], str | None]
    # (行号, 被 * 导入的模块)
    star_imports: list[tuple[int, str]]


def _scan(modules: dict[str, str]) -> _Scan:
    """扫 ``modules``（相对路径 -> 源码），列出读这两张表的文件和所有违规。"""
    parsed = [_parse(where, source) for where, source in sorted(modules.items())]
    tables: dict[str, str] = {}  # 绑着表名的名字（全名）-> 哪个类的表
    for module in parsed:
        tables.update(_table_bindings(module))

    readers: set[str] = set()
    violations: list[str] = []
    for module in parsed:
        for line, reads, problem in _accesses(module, tables):
            if reads:
                readers.add(module.where)
            if problem is not None:
                violations.append(f"{module.where}:{line} {problem}")
    return _Scan(readers, violations)


def _parse(where: str, source: str) -> _Module:
    parts = where.removesuffix(".py").split("/")
    package = parts[:-1]  # 相对 import 从这里往上数
    name = ".".join(package if parts[-1] == "__init__" else parts)
    tree = ast.parse(source, filename=where)

    imported: dict[str, str] = {}
    star_imports: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    imported[alias.asname] = alias.name
                else:
                    top = alias.name.split(".")[0]
                    imported[top] = top
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = package[: len(package) - (node.level - 1)]
                base = ".".join([*anchor, base] if base else anchor)
            for alias in node.names:
                if alias.name == "*":
                    star_imports.append((node.lineno, base))
                else:
                    imported[alias.asname or alias.name] = f"{base}.{alias.name}"

    def resolve(expr: ast.expr) -> str | None:
        if isinstance(expr, ast.Name):
            return imported.get(expr.id, f"{name}.{expr.id}")
        if isinstance(expr, ast.Attribute):
            base = resolve(expr.value)
            return f"{base}.{expr.attr}" if base else None
        return None

    return _Module(where, tree, resolve, star_imports)


def _table_of(expr: ast.AST, module: _Module) -> str | None:
    """``_table_name(<两个类之一>)`` 这样的调用，返回是哪个类；别的返回 ``None``。"""
    if not (
        isinstance(expr, ast.Call)
        and module.resolve(expr.func) == _TABLE_NAME_FN
        and len(expr.args) == 1
        and not expr.keywords
    ):
        return None
    cls = module.resolve(expr.args[0])
    return cls if cls in _OWNER_COLUMN else None


def _binding_targets(
    stmt: ast.AST, module: _Module
) -> tuple[list[ast.Name], str] | None:
    """``X = _table_name(Happening)``：(被赋值的名字, 类)。别的赋值形状返回 ``None``。"""
    if isinstance(stmt, ast.Assign):
        targets = stmt.targets
    elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
        targets = [stmt.target]
    else:
        return None
    cls = _table_of(stmt.value, module)
    if cls is None or not all(isinstance(t, ast.Name) for t in targets):
        return None
    return targets, cls  # type: ignore[return-value]


def _table_bindings(module: _Module) -> dict[str, str]:
    found: dict[str, str] = {}
    for node in ast.walk(module.tree):
        bound = _binding_targets(node, module)
        if bound is not None:
            targets, cls = bound
            for target in targets:
                found[module.resolve(target)] = cls  # type: ignore[index]
    return found


def _accesses(
    module: _Module, tables: dict[str, str]
) -> Iterator[tuple[int, bool, str | None]]:
    """模块里每一处碰到这两张表的地方：(行号, 是不是一处 SQL 读, 违规说明或 ``None``)。"""
    parents = {
        child: node
        for node in ast.walk(module.tree)
        for child in ast.iter_child_nodes(node)
    }
    typing_only = {id(n) for a in _annotations(module.tree) for n in ast.walk(a)}

    for line, source in module.star_imports:
        if source == Happening.__module__ or any(
            name.startswith(f"{source}.") for name in tables
        ):
            yield line, False, f"from {source} import * 带进来的名字扫描跟不到"

    for node in ast.walk(module.tree):
        text = _static_text(node)
        if (
            text is not None
            and _LITERAL_TABLE.search(text)
            and _static_text(parents.get(node)) is None
            # 文档字符串、单独一行的字符串不会被执行
            and not isinstance(parents.get(node), ast.Expr)
        ):
            yield (
                node.lineno,
                False,
                f"源码里直接写了表名：{text!r}，表名要用 _table_name 从类上取",
            )

        if not isinstance(node, (ast.Name, ast.Attribute)):
            continue
        name = module.resolve(node)
        if not isinstance(node.ctx, ast.Load):
            # 绑着表名的名字只能由那一句绑定赋值；别处再改它（+=、重新赋值、当循环变量），
            # 之后每一处用它的 SQL 看着筛了人，实际表名后面已经接上了别的东西。
            if name in tables and _binding_targets(parents.get(node), module) is None:
                shown = ast.unparse(parents.get(node, node))
                yield node.lineno, False, f"绑着表名的名字被改写：{shown}"
            continue
        if name in _OWNER_COLUMN:
            if id(node) not in typing_only:
                yield from _class_use(node, name, module, parents, tables)
        elif name in tables:
            yield from _table_use(node, tables[name], module, parents, tables)


def _annotations(tree: ast.Module) -> Iterator[ast.expr]:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = node.args
            for arg in (*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg):
                if arg is not None and arg.annotation is not None:
                    yield arg.annotation
            if node.returns is not None:
                yield node.returns
        elif isinstance(node, ast.AnnAssign):
            yield node.annotation


def _static_text(node: ast.AST | None) -> str | None:
    """字符串常量，或者全由字符串常量 ``+`` 起来的表达式的值。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _static_text(node.left), _static_text(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _class_use(
    node: ast.expr,
    cls: str,
    module: _Module,
    parents: dict[ast.AST, ast.AST],
    tables: dict[str, str],
) -> Iterator[tuple[int, bool, str | None]]:
    parent = parents.get(node)
    if isinstance(parent, ast.Call) and parent.func is node:
        return  # 构造一行
    if isinstance(parent, ast.Attribute) and parent.attr == "model_fields":
        return
    if isinstance(parent, ast.Call) and parent.args and parent.args[0] is node:
        if _table_of(parent, module) is not None:
            yield from _table_use(parent, cls, module, parents, tables)
            return
        if module.resolve(parent.func) in _WRITERS:
            return
    shown = ast.unparse(parent if isinstance(parent, ast.expr) else node)
    yield node.lineno, False, f"{cls} 用在扫描认不出来的地方：{shown}"


def _table_use(
    expr: ast.expr,
    cls: str,
    module: _Module,
    parents: dict[ast.AST, ast.AST],
    tables: dict[str, str],
) -> Iterator[tuple[int, bool, str | None]]:
    parent = parents.get(expr)
    if isinstance(parent, (ast.Assign, ast.AnnAssign)) and parent.value is expr:
        if _binding_targets(parent, module) is not None:
            return  # 绑定：之后用到这个名字的每一处另查
    elif isinstance(parent, ast.FormattedValue) and isinstance(
        joined := parents.get(parent), ast.JoinedStr
    ):
        yield expr.lineno, True, _sql_problem(joined, parent, cls, module, tables)
        return
    shown = ast.unparse(parent if isinstance(parent, ast.expr) else expr)
    yield expr.lineno, False, f"{cls} 的表名用在扫描认不出来的地方：{shown}"


# 渲染 f-string 时，这两张表的表名换成 _TABLE_MARK，别的嵌入值换成 _OPAQUE_MARK。
_TABLE_MARK = "\x01"
_OPAQUE_MARK = "\x00"


def _sql_problem(
    joined: ast.JoinedStr,
    at: ast.FormattedValue,
    cls: str,
    module: _Module,
    tables: dict[str, str],
) -> str | None:
    def render(part: ast.expr) -> str:
        if isinstance(part, ast.Constant):
            return str(part.value)
        assert isinstance(part, ast.FormattedValue)
        value = part.value
        if _table_of(value, module) is not None or module.resolve(value) in tables:
            return _TABLE_MARK
        return _OPAQUE_MARK

    index = next(i for i, part in enumerate(joined.values) if part is at)
    tail = "".join(render(part) for part in joined.values[index + 1 :])
    column = _OWNER_COLUMN[cls]
    if _narrowed_by(column, tail):
        return None
    shown = tail.replace(_TABLE_MARK, "{表}").replace(_OPAQUE_MARK, "{…}").strip()
    return f"读 {cls} 没按 {column} 筛到一个人：{{表}} {shown}"


_TOKEN = re.compile(
    r"\s*(?:"
    rf"(?P<opaque>--|/\*|{_OPAQUE_MARK})"
    rf"|(?P<table>{_TABLE_MARK})"
    r"|(?P<word>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)"
    r"|(?P<param>:\w+)"
    r"|(?P<op>::|<>|!=|<=|>=|\d+(?:\.\d+)?|[=<>(),;+\-*/])"
    r")"
)
# WHERE 到这几个词（括号外）为止。
_CLAUSE_END = {
    "order",
    "group",
    "limit",
    "offset",
    "having",
    "union",
    "intersect",
    "except",
    "returning",
    "for",
    "window",
    "fetch",
}


def _tokens(sql: str) -> Iterator[str]:
    """切词；切不出来的字符（引号、``|``、``%``……）当成一个 :data:`_OPAQUE_MARK`。"""
    pos = 0
    while pos < len(sql):
        if sql[pos:].strip() == "":
            return
        match = _TOKEN.match(sql, pos)
        if match is None:
            yield _OPAQUE_MARK
            return
        kind = match.lastgroup
        token = match.group(kind)  # type: ignore[arg-type]
        yield _OPAQUE_MARK if kind == "opaque" else token.lower()
        pos = match.end()


def _narrowed_by(column: str, tail: str) -> bool:
    """``tail`` 是 SQL 里紧跟在表名后面的那一段，它有没有把这张表筛到 ``column`` 等于一个参数。

    认得的形状只有一种：（可选的 ``AS`` 和别名）``WHERE`` 条件，条件在括号外没有 ``OR``，
    按括号外的 ``AND`` 切开以后，有一段恰好是 ``列 = :参数``（有别名时列可以写成
    ``别名.列``）。``:actor = :actor`` 是参数跟自己比，``actor = actor`` 是列跟自己比，
    都恒真，什么都没筛。
    """
    tokens = list(_tokens(tail))
    i = 0
    if tokens[i : i + 1] == ["as"]:
        i += 1
    alias = None
    if (
        i < len(tokens)
        and re.fullmatch(r"[a-z_]\w*", tokens[i])
        and tokens[i] != "where"
    ):
        alias = tokens[i]
        i += 1
    if tokens[i : i + 1] != ["where"]:
        return False

    conditions: list[list[str]] = [[]]
    depth = 0
    for token in tokens[i + 1 :]:
        if depth == 0 and (token in (")", ";", _TABLE_MARK) or token in _CLAUSE_END):
            break
        if token == _OPAQUE_MARK:
            return False
        if token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
        elif depth == 0 and token in ("or", "between"):
            return False
        elif depth == 0 and token == "and":
            conditions.append([])
            continue
        conditions[-1].append(token)

    owner = {column, f"{alias}.{column}"} if alias else {column}
    return any(
        len(c) == 3
        and c[0] in owner
        and c[1] == "="
        and c[2].startswith(":")
        and c[2] != "::"
        for c in conditions
    )


# ---------------------------------------------------------------------------
# 扫描本身的用例：一段假想的 app 模块源码交给 :func:`_scan`，看它认不认得出来。
# ---------------------------------------------------------------------------

_PRELUDE = """\
from app.living import records
from app.living.records import Happening, Whereabouts
from app.living.records import Happening as Deed
from app.living.serial import append_in_commit_order
from app.runtime.migrator import _table_name
from app.runtime.query import query

_TABLE = _table_name(Happening)
_PLACES = _table_name(Whereabouts)
"""


def _violations(body: str, **others: str) -> list[str]:
    return _scan({"app/living/elsewhere.py": _PRELUDE + body, **others}).violations


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            'SQL = "SELECT * FROM data_happening WHERE lane = :lane"\n',
            id="literal-table-in-plain-string",
        ),
        pytest.param(
            'SQL = f"SELECT * FROM data_whereabouts WHERE lane = :lane LIMIT {1}"\n',
            id="literal-table-in-f-string",
        ),
        pytest.param(
            'SQL = "SELECT * FROM " + "data_" + "happening" + " WHERE lane = :lane"\n',
            id="literal-table-concatenated",
        ),
        pytest.param(
            'SQL = f"SELECT * FROM {_table_name(Happening)} WHERE lane = :lane"\n',
            id="inline-table-name-call",
        ),
        pytest.param(
            "T = _table_name(records.Happening)\n"
            'SQL = f"SELECT * FROM {T} WHERE lane = :lane"\n',
            id="class-through-its-module",
        ),
        pytest.param(
            'T = _table_name(Deed)\nSQL = f"SELECT * FROM {T} WHERE lane = :lane"\n',
            id="class-under-an-alias",
        ),
        pytest.param(
            'SQL = f"SELECT * FROM {Happening.table_name} WHERE lane = :lane"\n',
            id="table-name-attribute-on-the-class",
        ),
        pytest.param(
            "from .records import Happening as Mine\n"
            'SQL = f"SELECT * FROM {_table_name(Mine)} WHERE lane = :lane"\n',
            id="class-through-a-relative-import",
        ),
        pytest.param(
            "from app.living.records import *\n"
            'SQL = f"SELECT * FROM {_table_name(Whereabouts)} WHERE persona_id = :me"\n',
            id="star-import-from-records",
        ),
        pytest.param(
            'T = _TABLE\nSQL = f"SELECT * FROM {T} WHERE lane = :lane"\n',
            id="binding-renamed",
        ),
        pytest.param(
            '_TABLE += " h, other o"\n'
            'SQL = f"SELECT * FROM {_TABLE} WHERE actor = :me"\n',
            id="binding-overwritten",
        ),
        pytest.param(
            'SQL = "SELECT * FROM {} WHERE lane = :lane".format(_TABLE)\n',
            id="binding-through-str-format",
        ),
        pytest.param(
            'SQL = "SELECT * FROM " + _TABLE + " WHERE lane = :lane"\n',
            id="binding-concatenated",
        ),
        pytest.param(
            'SQL = "SELECT * FROM %s WHERE lane = :lane" % _PLACES\n',
            id="binding-through-percent",
        ),
        pytest.param(
            "async def everyone(lane):\n"
            "    return await query(Happening).where(lane=lane).all()\n",
            id="query-builder",
        ),
        pytest.param(
            'SQL = f"SELECT * FROM {_TABLE} WHERE actor = :me OR lane = :lane"\n',
            id="or-beside-the-owner-filter",
        ),
        pytest.param(
            'SQL = f"SELECT * FROM {_PLACES} WHERE (persona_id = :me OR lane = :lane)"\n',
            id="or-around-the-owner-filter",
        ),
        pytest.param(
            # 筛人那一条单独成段，但 AND 比 OR 先结合：(actor AND seq) OR lane
            'SQL = f"SELECT * FROM {_TABLE} '
            'WHERE actor = :me AND seq > :after OR lane = :lane"\n',
            id="or-after-the-owner-filter",
        ),
        pytest.param(
            'SQL = f"SELECT * FROM {_TABLE} WHERE lane = :lane AND actor = actor"\n',
            id="column-compared-with-itself",
        ),
        pytest.param(
            'SQL = f"SELECT * FROM {_TABLE} WHERE lane = :lane AND :actor = :actor"\n',
            id="parameter-compared-with-itself",
        ),
        pytest.param(
            'SQL = f"SELECT h.* FROM {_TABLE} h JOIN other o ON o.k = h.k '
            'WHERE o.actor = :me"\n',
            id="owner-filter-on-another-table",
        ),
        pytest.param(
            'SQL = f"SELECT h.* FROM {_TABLE} h '
            'LEFT JOIN (SELECT * FROM other WHERE actor = :me) o ON o.k = h.k"\n',
            id="owner-filter-inside-a-joined-subquery",
        ),
        pytest.param(
            'SQL = f"SELECT * FROM other o '
            'WHERE EXISTS (SELECT 1 FROM {_PLACES} w WHERE o.persona_id = :me)"\n',
            id="owner-filter-on-the-outer-query",
        ),
        pytest.param(
            "def q(extra):\n"
            '    return f"SELECT * FROM {_TABLE} WHERE actor = :me {extra}"\n',
            id="value-spliced-into-the-where",
        ),
        pytest.param(
            'SQL = f"SELECT * FROM {_TABLE} WHERE lane = :lane -- AND actor = :me"\n',
            id="owner-filter-in-a-comment",
        ),
    ],
)
def test_the_scan_flags_every_access_it_cannot_show_is_narrowed(body):
    assert _violations(body) != []


def test_the_scan_follows_a_table_name_imported_from_another_module():
    assert (
        _violations(
            "",
            **{
                "app/living/reader.py": (
                    "from app.living.elsewhere import _TABLE\n"
                    'SQL = f"SELECT * FROM {_TABLE} WHERE lane = :lane"\n'
                )
            },
        )
        != []
    )


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            'SQL = f"SELECT * FROM {_TABLE} WHERE lane = :lane AND actor = :actor '
            'ORDER BY seq DESC LIMIT :limit"\n',
            id="plain",
        ),
        pytest.param(
            'SQL = f"SELECT h.* FROM {_TABLE} AS h WHERE h.lane = :lane AND h.actor = :p"\n',
            id="aliased",
        ),
        pytest.param(
            'SQL = f"SELECT * FROM {_table_name(Whereabouts)} '
            'WHERE lane = :lane AND persona_id = :p"\n',
            id="inline-table-name-call",
        ),
        pytest.param(
            'SQL = f"SELECT (SELECT MAX(seq) FROM {_TABLE} WHERE lane = :lane AND actor = :p) '
            'AS a, (SELECT MAX(seq) FROM {_PLACES} WHERE lane = :lane AND persona_id = :p) AS b"\n',
            id="both-tables-in-one-statement",
        ),
        pytest.param(
            'SQL = f"SELECT * FROM {_TABLE} WHERE lane = :lane AND actor = :me '
            'AND (kind = :a OR kind = :b)"\n',
            id="or-inside-another-condition",
        ),
        pytest.param(
            '"""data_happening 只给她自己读。"""\n'
            "async def keep(lane: str, rows: list[dict]) -> list[Happening]:\n"
            '    await append_in_commit_order(Happening, stream="s", scope={"lane": lane})\n'
            "    return [Happening(**{k: r[k] for k in Happening.model_fields}) for r in rows]\n",
            id="type-constructor-fields-write",
        ),
    ],
)
def test_the_scan_accepts_what_is_narrowed_to_one_person(body):
    assert _violations(body) == []


def test_the_scan_follows_a_narrowed_table_name_imported_from_another_module():
    assert (
        _violations(
            "",
            **{
                "app/living/reader.py": (
                    "from app.living.elsewhere import _TABLE\n"
                    'SQL = f"SELECT * FROM {_TABLE} WHERE lane = :lane AND actor = :me"\n'
                )
            },
        )
        == []
    )


def test_the_scan_leaves_another_class_with_the_same_name_alone():
    assert (
        _scan(
            {
                "app/world/notes.py": (
                    "from app.runtime.query import query\n"
                    "class Happening:\n"
                    "    pass\n"
                    "x = query(Happening)\n"
                )
            }
        ).violations
        == []
    )


def test_every_query_on_her_records_is_narrowed_to_one_person():
    scan = _scan(_app_modules())

    assert scan.readers >= _KNOWN_READERS, (
        "扫描没找到已知会读这两张表的那几处 —— 扫描本身失效了，这条门禁绿着等于没有"
    )
    assert scan.violations == [], "\n".join(scan.violations)
