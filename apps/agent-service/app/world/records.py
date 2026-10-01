"""world 的记录：私有卷上的一棵自然语言文档树，放在 ``$WORLD_DATA_DIR/<泳道>/records/``。

记录是 world 自己对这个世界的认识：有哪些地方、那里什么样、有哪些人和机构、什么东西
现在是什么状态。它是自然语言文档，不是库存表或地点表；一份一个 ``.md`` 文件，目录怎么
分由 world 自己定。代码里不预设任何目录名或文档名。

**两个写者。** 主 agent 的写记录动作（:mod:`app.world.actions`）和人工读写接口
（:mod:`app.world.admin`）都写这棵树，走的都是这里的四个入口。写和删的规矩是同一条：

* 新建一份：``expected=None``，这一份必须还不存在；
* 改写或删除一份已有的：``expected`` 必须是它现在的指纹（:func:`fingerprint_of`）。

对不上就抛 :class:`RecordConflict`，一个字都不动。整份改写是"读 → 想 → 写"，中间别的
写者可能已经改过；不带指纹的覆盖会把那一次改动连同它以为自己做成了的事一起静默丢掉。
指纹只从读和列目录里拿得到，所以带得出指纹就等于看过现在写的是什么。

**路径走不出这棵树。** 路径由模型或者人给，所以每一段都要是正常的名字（不空、不是
``.`` / ``..``、不以点开头、没有反斜杠和控制字符），最后一段以 ``.md`` 结尾；解析掉符号
链接之后还得在 ``records/`` 里面。同一个目录下的私有状态文件因此碰不到。

读写都是同步的，理由见 :mod:`app.world.volume`。写和删只有拿着这条泳道写锁的进程能做
（:func:`app.world.volume.require_writer_lock`），读不受限制。
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.infra.cst_time import CST
from app.world.volume import lane_dir, require_writer_lock, write_atomically

# 一份记录最多多少字。一份跑飞的文档能把主 agent 一轮的上下文顶掉；超过就拒，让写的人
# 拆成几份。
MAX_RECORD_CHARS = 12_000
MAX_PATH_CHARS = 200

_SUFFIX = ".md"


class RecordError(Exception):
    """对记录的一次操作没有做成，什么都没改。"""


class InvalidRecordPath(RecordError, ValueError):
    """这不是树里一份记录的路径。"""


class InvalidRecordText(RecordError, ValueError):
    """正文是空的，或者超过了 :data:`MAX_RECORD_CHARS`。"""


class RecordNotFound(RecordError):
    """没有这一份。"""


class RecordConflict(RecordError):
    """指纹对不上：这一份已经存在（新建时）、不存在（改写时）、或者在读过之后被改过。"""


@dataclass(frozen=True)
class RecordEntry:
    """目录里的一行。"""

    path: str
    chars: int
    updated_at: datetime
    fingerprint: str


@dataclass(frozen=True)
class Record:
    path: str
    text: str
    fingerprint: str
    updated_at: datetime


def fingerprint_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def records_root() -> Path:
    return lane_dir() / "records"


def _normal_part(part: str) -> bool:
    return (
        part not in ("", ".", "..")
        and not part.startswith(".")
        and "\\" not in part
        and not any(ord(c) < 32 or ord(c) == 127 for c in part)
    )


def _resolve(path: object) -> Path:
    """把一条记录路径换成盘上的位置；不是树里一份记录的路径就抛 :class:`InvalidRecordPath`。"""
    if not isinstance(path, str) or not path or len(path) > MAX_PATH_CHARS:
        raise InvalidRecordPath(
            f"记录的路径是相对记录根目录的 1 到 {MAX_PATH_CHARS} 个字符，"
            f"形如 目录/名字{_SUFFIX}"
        )
    parts = path.split("/")
    if not all(_normal_part(p) for p in parts):
        raise InvalidRecordPath(
            f"「{path}」不是一条记录路径：每一段都得是普通的名字（不空、不是 . 或 ..、"
            f"不以点开头、没有反斜杠和控制字符）"
        )
    if not parts[-1].endswith(_SUFFIX) or parts[-1] == _SUFFIX:
        raise InvalidRecordPath(f"「{path}」不是一条记录路径：记录是 {_SUFFIX} 文件")
    root = records_root()
    target = root.joinpath(*parts)
    if root.resolve() not in target.resolve().parents:
        raise InvalidRecordPath(f"「{path}」走出了记录的目录树")
    return target


def _updated_at(target: Path) -> datetime:
    return datetime.fromtimestamp(target.stat().st_mtime, UTC).astimezone(CST)


def _current_text(target: Path, path: str) -> str | None:
    """盘上现在的正文；没有这一份是 ``None``。同名的是个目录就不是记录。"""
    if target.is_dir():
        raise InvalidRecordPath(f"「{path}」是一个目录，不是一份记录")
    try:
        return target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except NotADirectoryError as exc:
        raise InvalidRecordPath(f"「{path}」上面有一层是一份记录，不是目录") from exc


def _check_expected(path: str, current: str | None, expected: str | None) -> None:
    if expected is None and current is not None:
        raise RecordConflict(
            f"「{path}」已经有了：要改它，先读到它现在的样子"
        )
    if expected is not None and current is None:
        raise RecordConflict(f"「{path}」不存在，没有可以改的")
    if expected is not None and fingerprint_of(current) != expected:
        raise RecordConflict(
            f"「{path}」在读过之后被改过（指纹 {expected} 已经不是现在的了）："
            f"重新读一遍再决定"
        )


def listing() -> list[RecordEntry]:
    """树里的每一份记录，按路径排。以点开头的文件和目录（写到一半的临时文件）不算。"""
    root = records_root()
    if not root.is_dir():
        return []
    entries: list[RecordEntry] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith(".") or not name.endswith(_SUFFIX):
                continue
            target = Path(dirpath) / name
            text = target.read_text(encoding="utf-8")
            entries.append(
                RecordEntry(
                    path=target.relative_to(root).as_posix(),
                    chars=len(text),
                    updated_at=_updated_at(target),
                    fingerprint=fingerprint_of(text),
                )
            )
    return sorted(entries, key=lambda e: e.path)


def read(path: str) -> Record:
    target = _resolve(path)
    text = _current_text(target, path)
    if text is None:
        raise RecordNotFound(f"没有「{path}」这一份")
    return Record(path, text, fingerprint_of(text), _updated_at(target))


def write(path: str, text: str, *, expected: str | None) -> Record:
    """整份写下一份记录。``expected`` 的规矩见模块说明。"""
    target = _resolve(path)
    require_writer_lock()
    if not isinstance(text, str) or not text.strip():
        raise InvalidRecordText("记录不能是空的")
    if len(text) > MAX_RECORD_CHARS:
        raise InvalidRecordText(
            f"一份记录最多 {MAX_RECORD_CHARS} 字，这一份有 {len(text)} 字：拆成几份"
        )
    _check_expected(path, _current_text(target, path), expected)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except (FileExistsError, NotADirectoryError) as exc:
        raise InvalidRecordPath(f"「{path}」上面有一层是一份记录，不是目录") from exc
    write_atomically(target, text)
    return Record(path, text, fingerprint_of(text), _updated_at(target))


def delete(path: str, *, expected: str) -> None:
    """删掉一份记录，``expected`` 必须是它现在的指纹。删空的目录一并收掉。"""
    target = _resolve(path)
    require_writer_lock()
    current = _current_text(target, path)
    if current is None:
        raise RecordNotFound(f"没有「{path}」这一份")
    _check_expected(path, current, expected)
    target.unlink()
    root = records_root()
    parent = target.parent
    while parent != root and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent
