"""把一张图各打标器的输出合并成一行结果，并提供输入 id 去重。

合并按打标器 `name` 命名空间（避免 wd14/eva02 都有 `tags` 时互相覆盖）；某打标器失败时
其 `error` 抽到行级 `errors`、子对象字段仍以 null 在位，做到单打标器失败不污染整行。
"""
from __future__ import annotations

from typing import Any

# 字段集（打标器组合）变化时递增，下游据此识别版本、避免 silent mismatch。
SCHEMA_VERSION = 1


def merge_row(image_id: str, results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    row: dict[str, Any] = {"id": image_id, "schema_version": SCHEMA_VERSION}
    errors: dict[str, str] = {}
    for name, result in results.items():
        row[name] = {k: v for k, v in result.items() if k != "error"}
        if "error" in result:
            errors[name] = result["error"]
    if errors:
        row["errors"] = errors
    return row


def dedup_ids(items: list[tuple[str, Any]]) -> tuple[list[tuple[str, Any]], list[str]]:
    """按 id 去重，保序保留首次出现；返回 (去重后的 items, 被丢弃的重复 id 列表)。"""
    seen: set[str] = set()
    kept: list[tuple[str, Any]] = []
    dups: list[str] = []
    for image_id, image in items:
        if image_id in seen:
            dups.append(image_id)
            continue
        seen.add(image_id)
        kept.append((image_id, image))
    return kept, dups
