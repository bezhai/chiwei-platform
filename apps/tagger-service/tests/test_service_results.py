from __future__ import annotations

from app.service.results import dedup_paths, merge_rows_for_paths, tagger_error_row


def test_dedup_paths_preserves_first_seen_order() -> None:
    kept, dups = dedup_paths(["a", "b", "a", "c", "b"])
    assert kept == ["a", "b", "c"]
    assert dups == ["a", "b"]


def test_merge_rows_for_paths_combines_capabilities_and_errors() -> None:
    qwen_rows = [{"id": "a", "schema_version": 1, "ocr": {"ocr_text": "hi"}}]
    tagger_rows = [tagger_error_row("a", "remote down")]

    rows = merge_rows_for_paths(["a"], tagger_rows, qwen_rows)

    assert rows[0]["id"] == "a"
    assert rows[0]["ocr"]["ocr_text"] == "hi"
    assert rows[0]["wd14"]["tags"] is None
    assert rows[0]["errors"]["wd14"] == "remote down"
