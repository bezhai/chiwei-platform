"""world 的记录：私有卷上按泳道分开的一棵自然语言文档树。

钉的是：位置（``$WORLD_DATA_DIR/<泳道>/records/``）、两个写者之间不会有人的写入被
静默盖掉（改一份已有的必须带上它现在的指纹）、路径走不出这棵树、写到一半不会留下
半截文件。
"""
from __future__ import annotations

import os

import pytest

from app.world import records
from app.world.volume import VolumeUnavailable

from .conftest import LANE


def test_a_new_record_is_written_under_this_lanes_records_directory(volume):
    written = records.write("地方/甲.md", "朝北的窗。", expected=None)

    on_disk = volume / LANE / "records" / "地方" / "甲.md"
    assert on_disk.read_text(encoding="utf-8") == "朝北的窗。"
    back = records.read("地方/甲.md")
    assert (back.path, back.text, back.fingerprint) == (
        "地方/甲.md",
        "朝北的窗。",
        written.fingerprint,
    )
    assert written.fingerprint == records.fingerprint_of("朝北的窗。")


def test_lanes_do_not_see_each_others_records(volume, monkeypatch):
    records.write("甲.md", "这条泳道的。", expected=None)

    monkeypatch.setenv("LANE", "coe-other")
    assert records.listing() == []
    with pytest.raises(records.RecordNotFound):
        records.read("甲.md")

    monkeypatch.delenv("LANE")
    records.write("甲.md", "prod 的。", expected=None)
    assert (volume / "prod" / "records" / "甲.md").exists()


def test_rewriting_an_existing_record_needs_its_current_fingerprint(volume):
    first = records.write("人/乙.md", "第一版。", expected=None)

    with pytest.raises(records.RecordConflict):
        records.write("人/乙.md", "没看过就写。", expected=None)
    with pytest.raises(records.RecordConflict):
        records.write("人/乙.md", "拿着旧指纹写。", expected="0" * 16)
    assert records.read("人/乙.md").text == "第一版。"

    second = records.write("人/乙.md", "第二版。", expected=first.fingerprint)

    assert records.read("人/乙.md").text == "第二版。"
    with pytest.raises(records.RecordConflict):
        records.write("人/乙.md", "拿着第一版的指纹。", expected=first.fingerprint)
    assert second.fingerprint != first.fingerprint


def test_a_fingerprint_for_a_record_that_does_not_exist_is_refused(volume):
    with pytest.raises(records.RecordConflict):
        records.write("丙.md", "它以为这里原来有一份。", expected="0" * 16)
    assert records.listing() == []


def test_deleting_needs_the_current_fingerprint_and_tidies_empty_directories(volume):
    kept = records.write("地方/家/丁.md", "留着。", expected=None)
    gone = records.write("地方/街/戊.md", "要删的。", expected=None)

    with pytest.raises(records.RecordConflict):
        records.delete("地方/街/戊.md", expected=kept.fingerprint)
    records.delete("地方/街/戊.md", expected=gone.fingerprint)

    assert [e.path for e in records.listing()] == ["地方/家/丁.md"]
    assert not (volume / LANE / "records" / "地方" / "街").exists()
    with pytest.raises(records.RecordNotFound):
        records.delete("地方/街/戊.md", expected=gone.fingerprint)


def test_the_listing_has_every_record_in_path_order_and_nothing_else(volume):
    records.write("地方/乙.md", "二。", expected=None)
    records.write("地方/甲.md", "一二三。", expected=None)
    records.write("设定.md", "底子。", expected=None)
    root = volume / LANE / "records"
    (root / "地方" / ".乙.md.tmp").write_text("半截", encoding="utf-8")
    (root / "说明.txt").write_text("不是记录", encoding="utf-8")

    entries = records.listing()

    assert [e.path for e in entries] == ["地方/乙.md", "地方/甲.md", "设定.md"]
    assert entries[1].chars == 4
    assert entries[1].fingerprint == records.fingerprint_of("一二三。")
    assert entries[1].updated_at.tzinfo is not None


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/甲.md",
        "../甲.md",
        "地方/../../甲.md",
        "地方/./甲.md",
        "地方//甲.md",
        ".甲.md",
        "地方/.隐藏/甲.md",
        "甲.txt",
        ".md",
        "地方\\甲.md",
        "甲\x00.md",
        "甲\n.md",
        "地" * 300 + ".md",
        "../next_wake.json",
    ],
)
def test_a_path_that_is_not_a_record_inside_the_tree_is_refused(volume, path):
    with pytest.raises(records.InvalidRecordPath):
        records.write(path, "不该落盘。", expected=None)
    with pytest.raises(records.InvalidRecordPath):
        records.read(path)
    assert not any(volume.rglob("*.md"))


def test_a_symlink_cannot_lead_a_write_out_of_the_tree(volume, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = volume / LANE / "records"
    root.mkdir(parents=True)
    os.symlink(outside, root / "出口")

    with pytest.raises(records.InvalidRecordPath):
        records.write("出口/甲.md", "越界。", expected=None)
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("text", ["", "   \n", "字" * (records.MAX_RECORD_CHARS + 1)])
def test_empty_or_oversized_text_is_refused(volume, text):
    with pytest.raises(records.InvalidRecordText):
        records.write("甲.md", text, expected=None)
    assert records.listing() == []


def test_a_write_leaves_no_temporary_file_behind(volume):
    first = records.write("甲.md", "一。", expected=None)
    records.write("甲.md", "二。", expected=first.fingerprint)

    names = [p.name for p in (volume / LANE / "records").iterdir()]
    assert names == ["甲.md"]


def test_a_directory_is_not_a_record(volume):
    records.write("地方/甲.md", "一。", expected=None)
    (volume / LANE / "records" / "地方" / "乙.md").mkdir()

    with pytest.raises(records.InvalidRecordPath):
        records.write("地方/乙.md", "二。", expected=None)
    with pytest.raises(records.InvalidRecordPath):
        records.write("地方/甲.md/丙.md", "三。", expected=None)


def test_without_a_volume_nothing_is_read_or_written(monkeypatch):
    monkeypatch.delenv("WORLD_DATA_DIR", raising=False)

    with pytest.raises(VolumeUnavailable, match="WORLD_DATA_DIR"):
        records.listing()
    with pytest.raises(VolumeUnavailable):
        records.write("甲.md", "一。", expected=None)
