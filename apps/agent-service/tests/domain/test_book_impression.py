"""赤尾对一本书的滚动印象 Data 契约 — 读小说 Task 2.

一本书一份「滚动印象」（第一人称印象正文 + 读到第几页 + 开读后状态）。这是模拟「会
读书的人」的记忆：单条、覆盖式重写（as_latest + Version，每次 append 新版、读取
select_latest 取最新一版）。本文件钉住印象 Data 在领域层的正确性故事：

  * **Key = (lane, persona_id, book_id)**：泳道隔离 + 每人每本书一条版本链。
  * **进度从机制派生、提交走版本 CAS**：``save_impression`` 用 expected_ver CAS append
    —— 并发 / 过期任务用过时 ver 写入会被拒（不覆盖更新的印象、不双推进页号）。
  * **三态 status**：在读 / 读完 / 放下，机制常量（不是让 LLM 猜的字符串）。
  * **当前在读那本**：``find_current_book_impression`` 取她最近读过一程、状态仍「在读」
    那一本（Task 3 注入只渲染这一本）。

集成测试（真实 Postgres）走 save → find → CAS 拒绝过期写 → 当前在读筛选的完整故事。
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
_BOOK = "book-abc"


@pytest.fixture
async def impression_db(test_db):
    """Build the BookImpression table on the test db."""
    await migrate(BookImpression, test_db)
    yield test_db


# ---------------------------------------------------------------------------
# 状态常量 + 形态
# ---------------------------------------------------------------------------


def test_status_constants_are_three_distinct_values():
    """三态常量互不相同、是机制层硬定的取值（不是让 LLM 猜的字符串）。"""
    assert STATUS_READING == "reading"
    assert STATUS_FINISHED == "finished"
    assert STATUS_ABANDONED == "abandoned"
    assert len({STATUS_READING, STATUS_FINISHED, STATUS_ABANDONED}) == 3


def test_impression_key_is_lane_persona_book():
    """自然键 = (lane, persona_id, book_id)，带 Version（as_latest）。"""
    from app.runtime.data import key_fields, version_field

    assert list(key_fields(BookImpression)) == ["lane", "persona_id", "book_id"]
    assert version_field(BookImpression) == "ver"


@pytest.mark.integration
async def test_save_records_request_id_for_turn_idempotency(impression_db):
    """印象行记着「这一版是哪次阅读任务提交的」（last_request_id）—— @node turn 幂等查重靠它。"""
    await save_impression(
        lane=_LANE, persona_id=_PERSONA, book_id=_BOOK,
        impression="一程。", pages_read=3, status=STATUS_READING,
        observed_at="t", expected_ver=0, request_id="req-abc",
    )
    imp = await find_book_impression(lane=_LANE, persona_id=_PERSONA, book_id=_BOOK)
    assert imp.last_request_id == "req-abc"


# ---------------------------------------------------------------------------
# save / find（真实 PG）
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_first_save_creates_reading_impression(impression_db):
    """首次保存印象（expected_ver=0）→ 落一条在读印象，find 取回。"""
    ok = await save_impression(
        lane=_LANE,
        persona_id=_PERSONA,
        book_id=_BOOK,
        impression="开头几页让我想起夏天。",
        pages_read=3,
        status=STATUS_READING,
        observed_at="2026-06-23T12:00:00+08:00",
        expected_ver=0,
    )
    assert ok is True
    imp = await find_book_impression(lane=_LANE, persona_id=_PERSONA, book_id=_BOOK)
    assert imp is not None
    assert imp.impression == "开头几页让我想起夏天。"
    assert imp.pages_read == 3
    assert imp.status == STATUS_READING
    assert imp.ver == 1


@pytest.mark.integration
async def test_find_missing_returns_none(impression_db):
    """没读过这本书 → find 返回 None（她还没开读）。"""
    imp = await find_book_impression(
        lane=_LANE, persona_id=_PERSONA, book_id="never-read"
    )
    assert imp is None


@pytest.mark.integration
async def test_second_save_overwrites_and_advances_page(impression_db):
    """读第二程：在最新 ver 上 CAS append 一版，印象覆盖重写、页号前进。"""
    await save_impression(
        lane=_LANE, persona_id=_PERSONA, book_id=_BOOK,
        impression="第一程。", pages_read=3, status=STATUS_READING,
        observed_at="2026-06-23T12:00:00+08:00", expected_ver=0,
    )
    first = await find_book_impression(lane=_LANE, persona_id=_PERSONA, book_id=_BOOK)
    ok = await save_impression(
        lane=_LANE, persona_id=_PERSONA, book_id=_BOOK,
        impression="读到第二程，揉进了旧印象。", pages_read=7, status=STATUS_READING,
        observed_at="2026-06-23T13:00:00+08:00", expected_ver=first.ver,
    )
    assert ok is True
    latest = await find_book_impression(lane=_LANE, persona_id=_PERSONA, book_id=_BOOK)
    assert latest.impression == "读到第二程，揉进了旧印象。"
    assert latest.pages_read == 7
    assert latest.ver == 2


@pytest.mark.integration
async def test_stale_expected_ver_is_rejected(impression_db):
    """CAS 命门：拿过时 ver 写入被拒（不覆盖更新的印象、不双推进页号）。

    模拟并发 / 部署中断重放：任务 A 读到 ver=1，期间任务 B 已 append 到 ver=2；
    A 再用 expected_ver=1 写入应被拒（False），库里仍是 B 的 ver=2。
    """
    await save_impression(
        lane=_LANE, persona_id=_PERSONA, book_id=_BOOK,
        impression="ver1。", pages_read=3, status=STATUS_READING,
        observed_at="t1", expected_ver=0,
    )
    # 任务 B 推进到 ver=2
    await save_impression(
        lane=_LANE, persona_id=_PERSONA, book_id=_BOOK,
        impression="ver2 by B。", pages_read=9, status=STATUS_READING,
        observed_at="t2", expected_ver=1,
    )
    # 任务 A 拿过时 ver=1 写入 → 被拒
    ok = await save_impression(
        lane=_LANE, persona_id=_PERSONA, book_id=_BOOK,
        impression="stale by A。", pages_read=5, status=STATUS_READING,
        observed_at="t3", expected_ver=1,
    )
    assert ok is False, "过时 expected_ver 必须被 CAS 拒"
    latest = await find_book_impression(lane=_LANE, persona_id=_PERSONA, book_id=_BOOK)
    assert latest.impression == "ver2 by B。", "更新的印象不被过期任务覆盖"
    assert latest.pages_read == 9, "页号不被过期任务回退 / 双推进"


@pytest.mark.integration
async def test_lane_isolation(impression_db):
    """lane 隔离：coe 与 prod 同一 persona + book 的印象互不覆盖。"""
    await save_impression(
        lane="coe-t2", persona_id=_PERSONA, book_id=_BOOK,
        impression="coe 的印象。", pages_read=2, status=STATUS_READING,
        observed_at="t", expected_ver=0,
    )
    await save_impression(
        lane="prod", persona_id=_PERSONA, book_id=_BOOK,
        impression="prod 的印象。", pages_read=8, status=STATUS_READING,
        observed_at="t", expected_ver=0,
    )
    coe = await find_book_impression(lane="coe-t2", persona_id=_PERSONA, book_id=_BOOK)
    prod = await find_book_impression(lane="prod", persona_id=_PERSONA, book_id=_BOOK)
    assert coe.impression == "coe 的印象。"
    assert prod.impression == "prod 的印象。"


# ---------------------------------------------------------------------------
# 当前在读那本（Task 3 注入只渲染这一本）
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_current_reading_is_most_recent_reading_book(impression_db):
    """当前在读那本 = 她最近读过一程、状态仍「在读」那一本（开读另一本旧的淡出）。"""
    # 先读书 A（较早）
    await save_impression(
        lane=_LANE, persona_id=_PERSONA, book_id="book-A",
        impression="A 的印象。", pages_read=5, status=STATUS_READING,
        observed_at="2026-06-23T10:00:00+08:00", expected_ver=0,
    )
    # 后读书 B（较晚）→ B 是当前在读
    await save_impression(
        lane=_LANE, persona_id=_PERSONA, book_id="book-B",
        impression="B 的印象。", pages_read=2, status=STATUS_READING,
        observed_at="2026-06-23T15:00:00+08:00", expected_ver=0,
    )
    cur = await find_current_book_impression(lane=_LANE, persona_id=_PERSONA)
    assert cur is not None
    assert cur.book_id == "book-B", "最近读过一程那本是当前在读"


@pytest.mark.integration
async def test_finished_or_abandoned_not_current(impression_db):
    """读完 / 放下的不再是当前在读（Task 3 注入只渲染在读那本）。"""
    await save_impression(
        lane=_LANE, persona_id=_PERSONA, book_id="book-done",
        impression="读完了。", pages_read=20, status=STATUS_FINISHED,
        observed_at="2026-06-23T16:00:00+08:00", expected_ver=0,
    )
    await save_impression(
        lane=_LANE, persona_id=_PERSONA, book_id="book-drop",
        impression="弃了。", pages_read=4, status=STATUS_ABANDONED,
        observed_at="2026-06-23T17:00:00+08:00", expected_ver=0,
    )
    cur = await find_current_book_impression(lane=_LANE, persona_id=_PERSONA)
    assert cur is None, "没有在读的书 → 无当前书"


@pytest.mark.integration
async def test_current_reading_ignores_other_persona(impression_db):
    """当前在读按 (lane, persona) 隔离：别的 persona 在读不算这个 persona 的。"""
    await save_impression(
        lane=_LANE, persona_id="chinagi", book_id="book-X",
        impression="千凪在读。", pages_read=3, status=STATUS_READING,
        observed_at="2026-06-23T15:00:00+08:00", expected_ver=0,
    )
    cur = await find_current_book_impression(lane=_LANE, persona_id=_PERSONA)
    assert cur is None


# ---------------------------------------------------------------------------
# render_reading_impression — 当前在读那本书的印象渲染（单一定义处，纯函数）.
#
# Task 3 注入两处（life 唤醒 stimulus + chat inner_context）共用这一份渲染：把当前
# 在读那本书渲成给模型看的一段文字 = 书名 + 这本书此刻在她心里的印象正文。无书时由
# 调用方处理（整段缺席），渲染函数本身只管把有的东西如实渲出来、不加框架腔评判。
# 纯函数（不碰 DB），所以不挂 integration mark。
# ---------------------------------------------------------------------------


def _impression(impression_text, *, pages_read=3, status=STATUS_READING):
    return BookImpression(
        lane=_LANE,
        persona_id=_PERSONA,
        book_id=_BOOK,
        impression=impression_text,
        pages_read=pages_read,
        status=status,
        observed_at="2026-06-23T15:00:00+08:00",
    )


def test_render_includes_title_and_impression_body():
    """渲染把书名 + 印象正文都渲出来（两处注入靠它把当前书呈现给模型）。"""
    out = render_reading_impression(
        _impression("那个少年总让我想起小时候的自己。"),
        title="挪威的森林",
    )
    assert "挪威的森林" in out, "书名必须渲出来"
    assert "那个少年总让我想起小时候的自己。" in out, "她的印象正文必须如实渲出来"


def test_render_returns_nonempty_text():
    """有书有印象时渲出的是一段非空文字（调用方据此接进 context）。"""
    out = render_reading_impression(
        _impression("读得有点慢，但放不下。"), title="百年孤独"
    )
    assert out.strip(), "有书时必须渲出非空文字"
