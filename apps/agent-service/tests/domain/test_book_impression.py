"""赤尾对一个文件的滚动印象 Data 契约 — 读小说 Task 3.

一个文件一份「滚动印象」（第一人称印象正文 + 读到第几页 + 开读后状态）。这是模拟「会
读书的人」的记忆：单条、覆盖式重写（as_latest + Version，每次 append 新版、读取
select_latest 取最新一版）。印象挂在**附件实例身份**上（决策 3：收到该文件那次，不是
注册的 book_id、不是对象存储 key），书名由印象自带（不查任何书注册表）。本文件钉住印象
Data 在领域层的正确性故事：

  * **Key = (lane, persona_id, attachment_id)**：泳道隔离 + 每人每个附件实例一条版本链。
  * **书名自带**：``book_title`` 列随印象存（注入渲染不再 find_book_meta）。
  * **进度从机制派生、提交走版本 CAS**：``save_impression`` 用 expected_ver CAS append。
  * **三态 status**：在读 / 读完 / 放下。
  * **当前在读那本**：``find_current_book_impression`` 取她最近读过一程、状态仍「在读」那本。
  * **重发不合并**（决策 3 命门）：同一份内容分两条消息重发 → 两个附件实例身份 → 两份
    独立印象、不合并。
"""

from __future__ import annotations

import pytest

from app.domain.book_impression import (
    STATUS_ABANDONED,
    STATUS_FINISHED,
    STATUS_READING,
    BookImpression,
    find_book_impression,
    find_current_book_impression,
    render_reading_impression,
    save_impression,
)
from tests.runtime.conftest import migrate

_LANE = "coe-t2"
_PERSONA = "akao"
_ATTACHMENT = "msg-1:file-k"


@pytest.fixture
async def impression_db(test_db):
    """Build the BookImpression table on the test db."""
    await migrate(BookImpression, test_db)
    yield test_db


# ---------------------------------------------------------------------------
# 状态常量 + 形态
# ---------------------------------------------------------------------------


def test_status_constants_are_three_distinct_values():
    assert STATUS_READING == "reading"
    assert STATUS_FINISHED == "finished"
    assert STATUS_ABANDONED == "abandoned"
    assert len({STATUS_READING, STATUS_FINISHED, STATUS_ABANDONED}) == 3


def test_impression_key_is_lane_persona_attachment():
    """自然键 = (lane, persona_id, attachment_id)，带 Version（as_latest）。"""
    from app.runtime.data import key_fields, version_field

    assert list(key_fields(BookImpression)) == ["lane", "persona_id", "attachment_id"]
    assert version_field(BookImpression) == "ver"


def test_impression_carries_book_title():
    """印象自带书名（book_title 列）—— 注入渲染不再查任何书注册表。"""
    assert "book_title" in BookImpression.model_fields


@pytest.mark.integration
async def test_save_records_request_id_for_turn_idempotency(impression_db):
    """印象行记着「这一版是哪次阅读任务提交的」（last_request_id）—— turn 幂等查重靠它。"""
    await save_impression(
        lane=_LANE, persona_id=_PERSONA, attachment_id=_ATTACHMENT,
        book_title="斜阳.txt", impression="一程。", pages_read=3,
        status=STATUS_READING, observed_at="t", expected_ver=0, request_id="req-abc",
    )
    imp = await find_book_impression(
        lane=_LANE, persona_id=_PERSONA, attachment_id=_ATTACHMENT
    )
    assert imp.last_request_id == "req-abc"


# ---------------------------------------------------------------------------
# save / find（真实 PG）
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_first_save_creates_reading_impression(impression_db):
    """首次保存印象（expected_ver=0）→ 落一条在读印象，find 取回（书名自带）。"""
    ok = await save_impression(
        lane=_LANE, persona_id=_PERSONA, attachment_id=_ATTACHMENT,
        book_title="夏天的书.txt", impression="开头几页让我想起夏天。",
        pages_read=3, status=STATUS_READING,
        observed_at="2026-06-23T12:00:00+08:00", expected_ver=0,
    )
    assert ok is True
    imp = await find_book_impression(
        lane=_LANE, persona_id=_PERSONA, attachment_id=_ATTACHMENT
    )
    assert imp is not None
    assert imp.impression == "开头几页让我想起夏天。"
    assert imp.book_title == "夏天的书.txt"
    assert imp.pages_read == 3
    assert imp.status == STATUS_READING
    assert imp.ver == 1


@pytest.mark.integration
async def test_find_missing_returns_none(impression_db):
    imp = await find_book_impression(
        lane=_LANE, persona_id=_PERSONA, attachment_id="never-read:k"
    )
    assert imp is None


@pytest.mark.integration
async def test_second_save_overwrites_and_advances_page(impression_db):
    """读第二程：在最新 ver 上 CAS append 一版，印象覆盖重写、页号前进。"""
    await save_impression(
        lane=_LANE, persona_id=_PERSONA, attachment_id=_ATTACHMENT,
        book_title="t", impression="第一程。", pages_read=3, status=STATUS_READING,
        observed_at="2026-06-23T12:00:00+08:00", expected_ver=0,
    )
    first = await find_book_impression(
        lane=_LANE, persona_id=_PERSONA, attachment_id=_ATTACHMENT
    )
    ok = await save_impression(
        lane=_LANE, persona_id=_PERSONA, attachment_id=_ATTACHMENT,
        book_title="t", impression="读到第二程，揉进了旧印象。", pages_read=7,
        status=STATUS_READING, observed_at="2026-06-23T13:00:00+08:00",
        expected_ver=first.ver,
    )
    assert ok is True
    latest = await find_book_impression(
        lane=_LANE, persona_id=_PERSONA, attachment_id=_ATTACHMENT
    )
    assert latest.impression == "读到第二程，揉进了旧印象。"
    assert latest.pages_read == 7
    assert latest.ver == 2


@pytest.mark.integration
async def test_stale_expected_ver_is_rejected(impression_db):
    """CAS 命门：拿过时 ver 写入被拒（不覆盖更新的印象、不双推进页号）。"""
    await save_impression(
        lane=_LANE, persona_id=_PERSONA, attachment_id=_ATTACHMENT,
        book_title="t", impression="ver1。", pages_read=3, status=STATUS_READING,
        observed_at="t1", expected_ver=0,
    )
    await save_impression(
        lane=_LANE, persona_id=_PERSONA, attachment_id=_ATTACHMENT,
        book_title="t", impression="ver2 by B。", pages_read=9, status=STATUS_READING,
        observed_at="t2", expected_ver=1,
    )
    ok = await save_impression(
        lane=_LANE, persona_id=_PERSONA, attachment_id=_ATTACHMENT,
        book_title="t", impression="stale by A。", pages_read=5, status=STATUS_READING,
        observed_at="t3", expected_ver=1,
    )
    assert ok is False
    latest = await find_book_impression(
        lane=_LANE, persona_id=_PERSONA, attachment_id=_ATTACHMENT
    )
    assert latest.impression == "ver2 by B。"
    assert latest.pages_read == 9


@pytest.mark.integration
async def test_lane_isolation(impression_db):
    """lane 隔离：coe 与 prod 同一 persona + 附件实例的印象互不覆盖。"""
    await save_impression(
        lane="coe-t2", persona_id=_PERSONA, attachment_id=_ATTACHMENT,
        book_title="t", impression="coe 的印象。", pages_read=2, status=STATUS_READING,
        observed_at="t", expected_ver=0,
    )
    await save_impression(
        lane="prod", persona_id=_PERSONA, attachment_id=_ATTACHMENT,
        book_title="t", impression="prod 的印象。", pages_read=8, status=STATUS_READING,
        observed_at="t", expected_ver=0,
    )
    coe = await find_book_impression(
        lane="coe-t2", persona_id=_PERSONA, attachment_id=_ATTACHMENT
    )
    prod = await find_book_impression(
        lane="prod", persona_id=_PERSONA, attachment_id=_ATTACHMENT
    )
    assert coe.impression == "coe 的印象。"
    assert prod.impression == "prod 的印象。"


@pytest.mark.integration
async def test_resend_two_instances_two_impressions_not_merged(impression_db):
    """决策 3 命门：同一份内容分两条消息重发 → 两个附件实例身份 → 两份独立印象、不合并。

    两个 attachment_id（不同 common_message_id 派生）各自落各自的印象版本链，互不覆盖。
    """
    inst_a = "msg-A:samefile"
    inst_b = "msg-B:samefile"
    await save_impression(
        lane=_LANE, persona_id=_PERSONA, attachment_id=inst_a,
        book_title="斜阳.txt", impression="第一次发的那份，读到一半。", pages_read=5,
        status=STATUS_READING, observed_at="2026-06-23T10:00:00+08:00", expected_ver=0,
    )
    await save_impression(
        lane=_LANE, persona_id=_PERSONA, attachment_id=inst_b,
        book_title="斜阳.txt", impression="第二次发的那份，刚翻开。", pages_read=1,
        status=STATUS_READING, observed_at="2026-06-23T11:00:00+08:00", expected_ver=0,
    )
    a = await find_book_impression(lane=_LANE, persona_id=_PERSONA, attachment_id=inst_a)
    b = await find_book_impression(lane=_LANE, persona_id=_PERSONA, attachment_id=inst_b)
    assert a.impression == "第一次发的那份，读到一半。"
    assert a.pages_read == 5
    assert b.impression == "第二次发的那份，刚翻开。"
    assert b.pages_read == 1, "两份独立印象互不合并、互不覆盖"


# ---------------------------------------------------------------------------
# 当前在读那本（Task 3 注入只渲染这一本）
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_current_reading_is_most_recent_reading_book(impression_db):
    """当前在读那本 = 她最近读过一程、状态仍「在读」那一本（开读另一本旧的淡出）。"""
    await save_impression(
        lane=_LANE, persona_id=_PERSONA, attachment_id="inst-A",
        book_title="A.txt", impression="A 的印象。", pages_read=5, status=STATUS_READING,
        observed_at="2026-06-23T10:00:00+08:00", expected_ver=0,
    )
    await save_impression(
        lane=_LANE, persona_id=_PERSONA, attachment_id="inst-B",
        book_title="B.txt", impression="B 的印象。", pages_read=2, status=STATUS_READING,
        observed_at="2026-06-23T15:00:00+08:00", expected_ver=0,
    )
    cur = await find_current_book_impression(lane=_LANE, persona_id=_PERSONA)
    assert cur is not None
    assert cur.attachment_id == "inst-B", "最近读过一程那本是当前在读"
    assert cur.book_title == "B.txt"


@pytest.mark.integration
async def test_finished_or_abandoned_not_current(impression_db):
    await save_impression(
        lane=_LANE, persona_id=_PERSONA, attachment_id="inst-done",
        book_title="d.txt", impression="读完了。", pages_read=20, status=STATUS_FINISHED,
        observed_at="2026-06-23T16:00:00+08:00", expected_ver=0,
    )
    await save_impression(
        lane=_LANE, persona_id=_PERSONA, attachment_id="inst-drop",
        book_title="x.txt", impression="弃了。", pages_read=4, status=STATUS_ABANDONED,
        observed_at="2026-06-23T17:00:00+08:00", expected_ver=0,
    )
    cur = await find_current_book_impression(lane=_LANE, persona_id=_PERSONA)
    assert cur is None


@pytest.mark.integration
async def test_current_reading_ignores_other_persona(impression_db):
    await save_impression(
        lane=_LANE, persona_id="chinagi", attachment_id="inst-X",
        book_title="x.txt", impression="千凪在读。", pages_read=3, status=STATUS_READING,
        observed_at="2026-06-23T15:00:00+08:00", expected_ver=0,
    )
    cur = await find_current_book_impression(lane=_LANE, persona_id=_PERSONA)
    assert cur is None


# ---------------------------------------------------------------------------
# render_reading_impression — 当前在读那本书的印象渲染（书名自带，单一定义处，纯函数）
# ---------------------------------------------------------------------------


def _impression(impression_text, *, book_title="挪威的森林.txt", pages_read=3,
                status=STATUS_READING):
    return BookImpression(
        lane=_LANE, persona_id=_PERSONA, attachment_id=_ATTACHMENT,
        book_title=book_title, impression=impression_text,
        pages_read=pages_read, status=status,
        observed_at="2026-06-23T15:00:00+08:00",
    )


def test_render_includes_title_and_impression_body():
    """渲染把书名（印象自带）+ 印象正文都渲出来。"""
    out = render_reading_impression(
        _impression("那个少年总让我想起小时候的自己。", book_title="挪威的森林")
    )
    assert "挪威的森林" in out, "书名（印象自带）必须渲出来"
    assert "那个少年总让我想起小时候的自己。" in out


def test_render_returns_nonempty_text():
    out = render_reading_impression(_impression("读得有点慢，但放不下。", book_title="百年孤独"))
    assert out.strip()
