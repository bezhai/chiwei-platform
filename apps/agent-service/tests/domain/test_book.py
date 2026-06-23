"""Book 接入与分页存储契约 — 读小说 Task 1.

赤尾要真的读一本书：你在飞书私聊发个 txt/epub 文件给她，系统解析、按「页」（可按页号
取的有序单元）落库，并往她 life 信箱投一条「X 推荐你读《书名》」。本文件钉住 Task 1
那条接入路径在领域层的正确性故事：

  * **分页是按页号可稳定取回的有序单元**：txt / epub 都解析成 N 页正文，``read(page_num)``
    能按号取回那一页、``total_pages`` 与实际页数一致（Task 2 靠它判读到书尾）。
  * **同内容重传按内容判重、不建第二本**：book_id 由 ``(lane, persona_id, content_hash)``
    派生，同一份内容再发一次落同一个 book_id、页不重复落（durable 幂等）。
  * **lane 进 Key**：runtime 持久化不自动加 lane，不显式带就会覆盖 prod 的书（写脏线上）。
  * **解析失败 fail-fast 抛异常**（不静默吞）：坏 epub 抛 ``BookParseError``，接入层据此
    回真人一条提示。

集成测试（真实 Postgres）走 ingest → read → 重传判重 → lane 隔离的完整故事；纯解析 /
判重 / 分页是纯函数，不碰 DB、直接断言。
"""

from __future__ import annotations

import io
import zipfile

import pytest

from app.domain.book import (
    BookMeta,
    BookPage,
    BookParseError,
    derive_book_id,
    find_book_meta,
    find_books_by_title,
    ingest_book,
    paginate,
    parse_epub,
    parse_txt,
    read_page,
)
from tests.runtime.conftest import migrate


@pytest.fixture
async def book_db(test_db):
    """Build the BookMeta + BookPage tables on the test db."""
    await migrate(BookMeta, test_db)
    await migrate(BookPage, test_db)
    yield test_db


def _make_epub(*, title: str, chapters: list[str]) -> bytes:
    """造一个最小可解析 epub（zip + container.xml + content.opf + N 个 xhtml）。

    epub 就是 zip 包 + OPF spine 串起 XHTML 章节，这里手搓一个最小合法包，让 parse_epub
    走真实的解析路径（解 zip、读 spine、抽 XHTML 文字），而不是 mock 掉解析库。
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        # mimetype 必须是第一个、不压缩（epub 规范）
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?>'
            '<container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            "<rootfiles><rootfile full-path=\"OEBPS/content.opf\" "
            'media-type="application/oebps-package+xml"/></rootfiles>'
            "</container>",
        )
        manifest_items = []
        spine_items = []
        for i, _ in enumerate(chapters):
            manifest_items.append(
                f'<item id="ch{i}" href="ch{i}.xhtml" '
                f'media-type="application/xhtml+xml"/>'
            )
            spine_items.append(f'<itemref idref="ch{i}"/>')
        opf = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
            'unique-identifier="bookid">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f"<dc:title>{title}</dc:title>"
            '<dc:identifier id="bookid">urn:uuid:test-book</dc:identifier>'
            "</metadata>"
            f"<manifest>{''.join(manifest_items)}</manifest>"
            f"<spine>{''.join(spine_items)}</spine>"
            "</package>"
        )
        z.writestr("OEBPS/content.opf", opf)
        for i, ch in enumerate(chapters):
            z.writestr(
                f"OEBPS/ch{i}.xhtml",
                '<?xml version="1.0" encoding="utf-8"?>'
                '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
                f"<p>{ch}</p></body></html>",
            )
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 纯解析 / 分页 / 判重（不碰 DB）
# ---------------------------------------------------------------------------


def test_parse_txt_decodes_utf8():
    """txt 直接读：utf-8 字节解码成文本，书名取文件名（去扩展名）。"""
    raw = "他年轻时也曾如此。".encode()
    text, title = parse_txt("斜阳.txt", raw)
    assert "他年轻时也曾如此" in text
    assert title == "斜阳", "txt 书名取文件名（去扩展名）"


def test_parse_txt_falls_back_on_gbk():
    """txt 非 utf-8（GBK）也能解码出来，不直接炸（中文 txt 常见 GBK 编码）。"""
    raw = "测试中文".encode("gbk")
    text, _ = parse_txt("a.txt", raw)
    assert "测试中文" in text


def test_parse_epub_extracts_spine_text_and_title():
    """epub 解析：按 spine 顺序抽出每章正文 + 取标题。"""
    data = _make_epub(title="人间失格", chapters=["第一章正文", "第二章正文"])
    text, title = parse_epub(data)
    assert title == "人间失格"
    assert "第一章正文" in text
    assert "第二章正文" in text
    # spine 顺序：第一章在第二章前
    assert text.index("第一章正文") < text.index("第二章正文")


def test_parse_epub_bad_bytes_raises_parse_error():
    """坏 epub（不是 zip）→ 抛 BookParseError（接入层据此回真人失败提示，不静默吞）。"""
    with pytest.raises(BookParseError):
        parse_epub(b"this is not a zip file at all")


def test_paginate_splits_into_ordered_pages():
    """分页：长文本切成若干页，页是有序的、拼回去覆盖全文、每页不超长。"""
    text = "段落。\n\n" * 500  # 足够长，必然多页
    pages = paginate(text, page_size=200)
    assert len(pages) >= 2, "足够长的文本应切成多页"
    for p in pages:
        assert len(p) <= 200 or "\n" not in p[:200], "每页不超过 page_size（除非单段超长）"
    # 拼回去内容不丢（顺序保持）
    assert "".join(pages).replace("\n", "") == text.replace("\n", "")


def test_paginate_short_text_is_single_page():
    """短文本只一页（total_pages=1，read(0) 取回全文）。"""
    pages = paginate("就这么一句话。", page_size=200)
    assert len(pages) == 1
    assert pages[0] == "就这么一句话。"


def test_derive_book_id_is_content_addressed():
    """book_id 由 (lane, persona_id, content_hash) 派生：同内容同 id、不同内容不同 id。"""
    a = derive_book_id(lane="coe-t1", persona_id="akao", content_hash="hashA")
    b = derive_book_id(lane="coe-t1", persona_id="akao", content_hash="hashA")
    c = derive_book_id(lane="coe-t1", persona_id="akao", content_hash="hashB")
    assert a == b, "同内容 hash 派生同 book_id（判重的根）"
    assert a != c, "不同内容派生不同 book_id"
    # lane 也进派生，泳道隔离
    d = derive_book_id(lane="prod", persona_id="akao", content_hash="hashA")
    assert a != d


# ---------------------------------------------------------------------------
# ingest_book + read_page（真实 PG）
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_ingest_txt_then_read_pages(book_db):
    """发个 txt → 按页落库、total_pages 正确、read_page 按号取回那一页。"""
    raw = ("一段长正文。" * 100).encode("utf-8")
    book_id = await ingest_book(
        lane="coe-t1",
        persona_id="akao",
        filename="测试书.txt",
        raw=raw,
        page_size=200,
    )

    meta = await find_book_meta(lane="coe-t1", book_id=book_id)
    assert meta is not None
    assert meta.title == "测试书"
    assert meta.total_pages >= 2

    # 每一页都能按号取回，且页号不越界
    for n in range(meta.total_pages):
        page = await read_page(lane="coe-t1", book_id=book_id, page_num=n)
        assert page is not None, f"第 {n} 页应能取回"
    # 越界页取回 None
    assert await read_page(lane="coe-t1", book_id=book_id, page_num=meta.total_pages) is None


@pytest.mark.integration
async def test_ingest_epub_then_read_pages(book_db):
    """发个 epub → 解析入库、total_pages 正确、read_page 取回正文。"""
    data = _make_epub(title="夜航船", chapters=["甲章" * 80, "乙章" * 80])
    book_id = await ingest_book(
        lane="coe-t1",
        persona_id="akao",
        filename="ye.epub",
        raw=data,
        page_size=200,
    )
    meta = await find_book_meta(lane="coe-t1", book_id=book_id)
    assert meta is not None
    assert meta.title == "夜航船", "epub 书名取 OPF 标题（不是文件名）"
    assert meta.total_pages >= 1

    page0 = await read_page(lane="coe-t1", book_id=book_id, page_num=0)
    assert page0 is not None and ("甲章" in page0 or "乙章" in page0)


@pytest.mark.integration
async def test_same_content_reupload_no_duplicate_book(book_db):
    """同内容重传判重命门：同一份内容再发一次 → 落同一个 book_id、不建第二本、页不翻倍。"""
    raw = ("重复内容测试。" * 100).encode("utf-8")
    first = await ingest_book(
        lane="coe-t1", persona_id="akao", filename="dup.txt", raw=raw, page_size=200
    )
    second = await ingest_book(
        lane="coe-t1", persona_id="akao", filename="dup.txt", raw=raw, page_size=200
    )
    assert first == second, "同内容重传应返回同一个 book_id"

    meta = await find_book_meta(lane="coe-t1", book_id=first)
    assert meta is not None
    # 页数不因重传翻倍：read 到 total_pages-1 有、total_pages 处仍 None（没多塞）
    last = await read_page(lane="coe-t1", book_id=first, page_num=meta.total_pages - 1)
    assert last is not None
    over = await read_page(lane="coe-t1", book_id=first, page_num=meta.total_pages)
    assert over is None, "重传不该多塞页"


@pytest.mark.integration
async def test_book_lane_isolation(book_db):
    """lane 隔离：prod 与 coe 的同内容书 book_id 不同、互不覆盖。"""
    raw = ("同一份内容。" * 50).encode("utf-8")
    prod_id = await ingest_book(
        lane="prod", persona_id="akao", filename="x.txt", raw=raw, page_size=200
    )
    coe_id = await ingest_book(
        lane="coe-t1", persona_id="akao", filename="x.txt", raw=raw, page_size=200
    )
    assert prod_id != coe_id, "lane 进 book_id 派生，泳道隔离"
    # coe 读不到 prod 的书
    assert await find_book_meta(lane="coe-t1", book_id=prod_id) is None


@pytest.mark.integration
async def test_find_books_by_title_fuzzy_matches_within_persona(book_db):
    """按 (lane, persona) 下书名模糊找书：子串匹配、返回候选给工具判，不自动选。"""
    a = await ingest_book(
        lane="coe-t1", persona_id="akao", filename="人间失格.txt",
        raw=("正文一" * 100).encode(), page_size=200,
    )
    await ingest_book(
        lane="coe-t1", persona_id="akao", filename="斜阳.txt",
        raw=("正文二" * 100).encode(), page_size=200,
    )
    # 模糊「失格」→ 命中《人间失格》一本
    hits = await find_books_by_title(lane="coe-t1", persona_id="akao", title="失格")
    assert len(hits) == 1
    assert hits[0].book_id == a
    assert hits[0].title == "人间失格"


@pytest.mark.integration
async def test_find_books_by_title_no_match_returns_empty(book_db):
    """书名对不上任何一本 → 返回空列表（工具据此回问让她重报，不乱选）。"""
    await ingest_book(
        lane="coe-t1", persona_id="akao", filename="斜阳.txt",
        raw=("正文" * 100).encode(), page_size=200,
    )
    hits = await find_books_by_title(lane="coe-t1", persona_id="akao", title="罪与罚")
    assert hits == []


@pytest.mark.integration
async def test_find_books_by_title_isolates_persona_and_lane(book_db):
    """模糊找书按 (lane, persona) 隔离：别的 persona / lane 的同名书不串。"""
    await ingest_book(
        lane="coe-t1", persona_id="akao", filename="共同的书.txt",
        raw=("akao 的" * 100).encode(), page_size=200,
    )
    await ingest_book(
        lane="coe-t1", persona_id="chinagi", filename="共同的书.txt",
        raw=("chinagi 的" * 100).encode(), page_size=200,
    )
    # akao 报书名只命中 akao 自己的那本（不串到千凪的同名书）
    hits = await find_books_by_title(lane="coe-t1", persona_id="akao", title="共同的书")
    assert len(hits) == 1
    assert hits[0].persona_id == "akao"


@pytest.mark.integration
async def test_find_books_by_title_returns_multiple_when_ambiguous(book_db):
    """书名模糊命中多本 → 全返回（工具据此回问让她说清是哪本，绝不替她选一本）。"""
    await ingest_book(
        lane="coe-t1", persona_id="akao", filename="夏天的故事 上.txt",
        raw=("上册" * 100).encode(), page_size=200,
    )
    await ingest_book(
        lane="coe-t1", persona_id="akao", filename="夏天的故事 下.txt",
        raw=("下册" * 100).encode(), page_size=200,
    )
    hits = await find_books_by_title(lane="coe-t1", persona_id="akao", title="夏天的故事")
    assert len(hits) == 2, "模糊命中多本全返回，由工具回问、不替她选"


@pytest.mark.integration
async def test_ingest_bad_epub_raises(book_db):
    """坏 epub → ingest_book 抛 BookParseError（接入层回真人失败提示，不静默吞）。"""
    with pytest.raises(BookParseError):
        await ingest_book(
            lane="coe-t1",
            persona_id="akao",
            filename="broken.epub",
            raw=b"not a real epub",
            page_size=200,
        )
