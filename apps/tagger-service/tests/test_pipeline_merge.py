from __future__ import annotations

from app.pipeline.merge import SCHEMA_VERSION, dedup_ids, merge_row


def test_merge_combines_taggers_by_name() -> None:
    results = {
        "phash": {"phash": "abc", "dhash": "def"},
        "anime_rating": {"safe": 0.9, "r15": 0.05, "r18": 0.05, "nsfw_score": 0.1},
    }
    row = merge_row("img1", results)
    assert row["id"] == "img1"
    assert row["phash"]["phash"] == "abc"
    assert row["anime_rating"]["nsfw_score"] == 0.1
    assert row["schema_version"] == SCHEMA_VERSION
    assert "errors" not in row  # 全成功不带 errors


def test_merge_isolates_error_per_tagger() -> None:
    # 某打标器失败 → 进 errors，其字段仍以 null 在位；不影响其他打标器
    results = {
        "phash": {"phash": None, "dhash": None, "error": "OSError: boom"},
        "anime_rating": {"safe": 0.9, "r15": 0.05, "r18": 0.05, "nsfw_score": 0.1},
    }
    row = merge_row("img1", results)
    assert row["errors"]["phash"] == "OSError: boom"
    assert "error" not in row["phash"]  # error 移到 errors、不留在子对象里
    assert row["phash"]["phash"] is None
    assert row["anime_rating"]["safe"] == 0.9  # 其他打标器不受影响


def test_merge_distinguishes_two_taggers_same_field() -> None:
    # wd14 和 eva02 都输出 tags，按 name 命名空间区分、不互相覆盖
    results = {
        "wd14": {"tags": ["1girl"], "rating": {"general": 0.8}},
        "eva02": {"tags": ["solo"], "rating": {"general": 0.7}},
    }
    row = merge_row("img1", results)
    assert row["wd14"]["tags"] == ["1girl"]
    assert row["eva02"]["tags"] == ["solo"]


def test_dedup_ids_drops_duplicates_and_reports() -> None:
    items = [("a", object()), ("b", object()), ("a", object()), ("c", object())]
    kept, dups = dedup_ids(items)
    assert [i for i, _ in kept] == ["a", "b", "c"]
    assert dups == ["a"]


def test_dedup_ids_empty() -> None:
    kept, dups = dedup_ids([])
    assert kept == []
    assert dups == []
