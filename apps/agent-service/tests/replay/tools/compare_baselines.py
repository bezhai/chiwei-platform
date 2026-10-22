"""Compare two directories of replay baselines: every document is in both, and two documents of the
same name differ only at the JSON paths allowed to differ.

An intended change re-records some baselines; this says that it changed nothing else::

    uv run python -m tests.replay.tools.compare_baselines OLD_DIR NEW_DIR \\
        --allow 'steps[*].model_calls[*].prompt.variables.currDate' \\
        --allow 'steps[*].model_calls[*].prompt.variables.currTime'

A path is written the way the replay reports a difference (``steps[1].model_calls[0].prompt``);
in a pattern ``[*]`` stands for any index and ``*`` for any one key. A difference at or under an
allowed path is allowed: a key present on one side only is a difference at that key, a list that
changed length is a difference at the list. Prints, per document, the allowed differences and the
others; exits 1 when there is any other difference or a document is in one directory only.

``tool_schemas.json`` (the tools' schemas, kept once for all baselines) is compared like any other
document; its paths start at the tool name (``search_web.description``).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

Path_ = tuple[str | int, ...]

_ANY_INDEX = object()
_ANY_KEY = object()
_TOKEN = re.compile(r"\[(\*|\d+)\]|\.?([^.\[\]]+)")


def _pattern(text: str) -> tuple[object, ...]:
    tokens: list[object] = []
    for index, key in _TOKEN.findall(text):
        if index:
            tokens.append(_ANY_INDEX if index == "*" else int(index))
        else:
            tokens.append(_ANY_KEY if key == "*" else key)
    return tuple(tokens)


def _render(path: Path_) -> str:
    out = ""
    for token in path:
        out += (
            f"[{token}]" if isinstance(token, int) else (f".{token}" if out else token)
        )
    return out


def _matches(path: Path_, pattern: tuple[object, ...]) -> bool:
    if len(pattern) > len(path):
        return False
    for token, want in zip(path, pattern, strict=False):
        if want is _ANY_INDEX:
            if not isinstance(token, int):
                return False
        elif want is _ANY_KEY:
            if not isinstance(token, str):
                return False
        elif token != want:
            return False
    return True


def differences(old: Any, new: Any, path: Path_ = ()) -> list[Path_]:
    """Where two JSON documents differ."""
    if type(old) is not type(new):
        return [path]
    if isinstance(old, dict):
        found: list[Path_] = []
        for key in list(old) + [k for k in new if k not in old]:
            if key not in old or key not in new:
                found.append((*path, key))
            else:
                found += differences(old[key], new[key], (*path, key))
        return found
    if isinstance(old, list):
        if len(old) != len(new):
            return [path]
        found = []
        for i, (a, b) in enumerate(zip(old, new, strict=True)):
            found += differences(a, b, (*path, i))
        return found
    return [] if old == new else [path]


@dataclass
class Report:
    allowed: dict[str, list[str]] = field(default_factory=dict)
    disallowed: dict[str, list[str]] = field(default_factory=dict)
    only_in_one: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.disallowed and not self.only_in_one


def compare(old_dir: Path, new_dir: Path, *, allowed: Sequence[str]) -> Report:
    patterns = [_pattern(a) for a in allowed]
    old_docs = {p.relative_to(old_dir).as_posix() for p in old_dir.rglob("*.json")}
    new_docs = {p.relative_to(new_dir).as_posix() for p in new_dir.rglob("*.json")}
    report = Report()
    report.only_in_one = sorted(
        [f"{d} (only in old)" for d in old_docs - new_docs]
        + [f"{d} (only in new)" for d in new_docs - old_docs]
    )
    for name in sorted(old_docs & new_docs):
        old = json.loads((old_dir / name).read_text(encoding="utf-8"))
        new = json.loads((new_dir / name).read_text(encoding="utf-8"))
        for path in differences(old, new):
            ok = any(_matches(path, p) for p in patterns)
            (report.allowed if ok else report.disallowed).setdefault(name, []).append(
                _render(path)
            )
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("old", type=Path)
    parser.add_argument("new", type=Path)
    parser.add_argument(
        "--allow", action="append", default=[], help="a JSON path pattern"
    )
    args = parser.parse_args(argv)
    report = compare(args.old, args.new, allowed=args.allow)
    names = sorted(set(report.allowed) | set(report.disallowed))
    for name in names:
        allowed, others = report.allowed.get(name, []), report.disallowed.get(name, [])
        print(f"{name}: {len(allowed)} allowed, {len(others)} other")
        for path in others:
            print(f"  NOT ALLOWED {path}")
    for line in report.only_in_one:
        print(f"MISSING {line}")
    total = sum(len(v) for v in report.allowed.values())
    print(
        f"{len(names)} document(s) differ: {total} allowed difference(s), "
        f"{sum(len(v) for v in report.disallowed.values())} other(s), "
        f"{len(report.only_in_one)} document(s) in one directory only"
    )
    return 0 if report.clean else 1


if __name__ == "__main__":
    sys.exit(main())
