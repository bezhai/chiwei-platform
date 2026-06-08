from __future__ import annotations

from typing import Any

from app.pipeline.merge import SCHEMA_VERSION, merge_row

TAGGER_CAPABILITIES = ("wd14", "eva02", "anime_rating", "phash")


def error_row(image_id: str, capability: str, message: str) -> dict[str, Any]:
    return merge_row(image_id, {capability: {"error": message}})


def tagger_error_row(image_id: str, message: str) -> dict[str, Any]:
    return merge_row(
        image_id,
        {
            "wd14": {"tags": None, "rating": None, "error": message},
            "eva02": {"tags": None, "rating": None, "error": message},
            "anime_rating": {
                "safe": None,
                "r15": None,
                "r18": None,
                "nsfw_score": None,
                "error": message,
            },
            "phash": {"phash": None, "dhash": None, "error": message},
        },
    )


def row_to_capabilities(row: dict[str, Any]) -> dict[str, dict[str, Any]]:
    errors = row.get("errors", {})
    caps: dict[str, dict[str, Any]] = {}
    for key, value in row.items():
        if key in {"id", "schema_version", "errors"}:
            continue
        if not isinstance(value, dict):
            continue
        cap = dict(value)
        if key in errors:
            cap["error"] = errors[key]
        caps[key] = cap
    return caps


def merge_rows_for_paths(paths: list[str], *row_groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, dict[str, Any]]] = {path: {} for path in paths}
    for rows in row_groups:
        for row in rows:
            image_id = str(row["id"])
            by_id.setdefault(image_id, {}).update(row_to_capabilities(row))
    return [merge_row(path, by_id.get(path, {})) for path in paths]


def dedup_paths(paths: list[str]) -> tuple[list[str], list[str]]:
    seen: set[str] = set()
    kept: list[str] = []
    dups: list[str] = []
    for path in paths:
        if path in seen:
            dups.append(path)
            continue
        seen.add(path)
        kept.append(path)
    return kept, dups


def empty_row(image_id: str) -> dict[str, Any]:
    return {"id": image_id, "schema_version": SCHEMA_VERSION}
