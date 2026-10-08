"""Files a process keeps on its own disk (world's private volume).

* Read before and after a step; the baseline gets the files added, changed and removed, with
  their content (JSON parsed).
* Every write into the volume ends with ``os.replace`` of a temporary file
  (:func:`app.world.volume.write_atomically`), and every delete is an ``unlink`` / ``rmdir``;
  each goes onto the step's effects timeline (``{"file": "write" | "delete", "path": ...}``) in
  the order it happened relative to commits and publishes.
* The filesystem's clock: a file's modification time is a clock read (world shows when a record
  last changed). After the replace that writes a file, its mtime is set to the frozen clock, so
  it is the time of the write, as it would be, but reproducible.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

_REAL_REPLACE = os.replace
_REAL_UNLINK = os.unlink
_REAL_RMDIR = os.rmdir


def _temporary(path: Path) -> bool:
    return path.name.startswith(".") and path.name.endswith(".tmp")


def install(monkeypatch, root: Path, effects) -> None:
    root = root.resolve()

    def inside(path) -> str | None:
        resolved = Path(path).resolve()
        return (
            resolved.relative_to(root).as_posix()
            if resolved.is_relative_to(root)
            else None
        )

    def replace(src, dst, *args, **kwargs):
        where = inside(dst)
        if where is not None:
            effects.alive()
        _REAL_REPLACE(src, dst, *args, **kwargs)
        if where is not None and _temporary(Path(src)):
            now = time.time()
            os.utime(dst, (now, now))
            effects.append({"file": "write", "path": where})

    def unlink(path, *args, **kwargs):
        where = inside(path)
        if where is not None and not _temporary(Path(path)):
            effects.alive()
        existed = os.path.exists(path)
        _REAL_UNLINK(path, *args, **kwargs)
        if where is not None and existed and not _temporary(Path(path)):
            effects.append({"file": "delete", "path": where})

    def rmdir(path, *args, **kwargs):
        where = inside(path)
        if where is not None:
            effects.alive()
        _REAL_RMDIR(path, *args, **kwargs)
        if where is not None:
            effects.append({"file": "delete directory", "path": where})

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(os, "unlink", unlink)
    monkeypatch.setattr(os, "rmdir", rmdir)


def read_tree(root: Path) -> dict[str, Any]:
    files: dict[str, Any] = {}
    if not root.exists():
        return files
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        raw = path.read_text(encoding="utf-8")
        content: Any = raw
        if path.suffix == ".json":
            try:
                content = json.loads(raw)
            except ValueError:
                pass
        files[path.relative_to(root).as_posix()] = content
    return files


def file_changes(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    for name in sorted(set(before) | set(after)):
        if name not in before:
            changes[name] = {"added": after[name]}
        elif name not in after:
            changes[name] = {"removed": before[name]}
        elif before[name] != after[name]:
            changes[name] = {"before": before[name], "after": after[name]}
    return changes
