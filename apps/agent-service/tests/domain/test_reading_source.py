"""读时取字节 + 现解码现分页 + 附件实例身份 — 读小说 Task 2 (reading_source).

阅读能力从"读一本注册的书"改写成"读她收到的一个文件"。没有书注册表：读的时候才
从对象存储取这个附件实例的字节、现解码现分页。本文件钉住读时这层在领域层的正确性：

  * **附件实例身份 = 收到该文件那次**（common_message_id + file_key），不是 tos_file、
    不是内容 hash（对象存储可能内容去重把重发合并 → 拿它当身份会把多份印象并一份）。
  * **读时取字节**：tos_file（files/<file_key>，= TOS file_name）→ presigned URL →
    httpx 取字节。取不到（未缓存 / 预签失败 / GET 非 2xx / 超时）→ None（fail-soft）。
  * **现解码现分页**：txt 解码 / epub 抽，按原始 file_name 分流（file_key 不保证带后缀）。
    分页定长 + 段落对齐，同内容跨轮稳定（连续前沿不错位）。

纯函数（派生 id / 解码 / 分页）直接断言；取字节那只手 mock 掉 image_client + httpx，
测的是 fail-soft 链路（绝不真打 tool-service / 真发网络）。
"""

from __future__ import annotations

import io
import zipfile

import pytest

import app.domain.reading_source as rs
from app.domain.reading_source import (
    BookParseError,
    decode_pages,
    derive_attachment_id,
    fetch_attachment_bytes,
    paginate,
    parse_epub,
    parse_txt,
)


def _make_epub(*, title: str, chapters: list[str]) -> bytes:
    """造一个最小可解析 epub（zip + container.xml + content.opf + N 个 xhtml）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
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
# 附件实例身份（决策 3）
# ---------------------------------------------------------------------------


def test_attachment_id_is_message_plus_file_key():
    """附件实例身份 = 收到该文件那次（common_message_id + file_key），稳定可复算。"""
    a = derive_attachment_id(common_message_id="msg-1", file_key="file-k")
    b = derive_attachment_id(common_message_id="msg-1", file_key="file-k")
    assert a == b, "同一附件实例派生同一 id（再读一程覆盖重写、不新增第二条）"


def test_attachment_id_distinguishes_resend():
    """同一份内容分两条消息重发 → 两个 common_message_id → 两个不同附件实例身份。

    决策 3 命门：身份绝不取自 tos_file / 内容 hash（对象存储可能内容去重把重发指向同
    一份字节）；重发=新消息=新身份，两份独立印象不合并。
    """
    first = derive_attachment_id(common_message_id="msg-1", file_key="same-file-key")
    second = derive_attachment_id(common_message_id="msg-2", file_key="same-file-key")
    assert first != second, "重发（不同 common_message_id）= 不同附件实例 = 不合并印象"


def test_attachment_id_distinguishes_file_key():
    """同一条消息里多个文件项（不同 file_key）→ 各自独立身份。"""
    a = derive_attachment_id(common_message_id="msg-1", file_key="k1")
    b = derive_attachment_id(common_message_id="msg-1", file_key="k2")
    assert a != b


# ---------------------------------------------------------------------------
# 现解码现分页（按原始 file_name 分流，不靠 file_key 推后缀 — codex 必改 3）
# ---------------------------------------------------------------------------


def test_decode_pages_txt_by_filename():
    """txt 按原始 file_name 分流：解码字节 → 分页成有序页。"""
    raw = ("一段长正文。" * 200).encode("utf-8")
    pages = decode_pages("斜阳.txt", raw, page_size=200)
    assert len(pages) >= 2, "足够长的 txt 切成多页"
    # 拼回去内容不丢
    assert "".join(pages).replace("\n", "") == raw.decode("utf-8").replace("\n", "")


def test_decode_pages_epub_by_filename():
    """epub 按原始 file_name 分流：解 zip + 抽 spine 文字 → 分页。"""
    data = _make_epub(title="人间失格", chapters=["第一章正文" * 50, "第二章正文" * 50])
    pages = decode_pages("ningen.epub", data, page_size=200)
    assert len(pages) >= 1
    body = "".join(pages)
    assert "第一章正文" in body and "第二章正文" in body


def test_decode_pages_filename_decides_format_not_file_key():
    """分流靠 file_name 后缀，不靠 tos_file / file_key（file_key 不保证带后缀）。

    一份 epub 字节，file_name 写 .epub → 必须走 epub 解析（不是把二进制 zip 当 txt 读）。
    """
    data = _make_epub(title="夏", chapters=["甲" * 60])
    pages = decode_pages("夏.epub", data, page_size=200)
    body = "".join(pages)
    assert "甲" in body, "epub 后缀走 epub 解析、抽出真正文（不是把 zip 字节当文本）"


def test_decode_pages_stable_across_calls():
    """同内容跨轮分页必须稳定（连续前沿不错位）。"""
    raw = ("稳定测试。" * 300).encode("utf-8")
    p1 = decode_pages("a.txt", raw, page_size=200)
    p2 = decode_pages("a.txt", raw, page_size=200)
    assert p1 == p2, "同内容同分页（确定性切分），跨轮稳定"


def test_decode_pages_bad_epub_raises():
    """坏 epub（不是 zip）→ 抛 BookParseError（阅读 agent 据此整程 fail-soft）。"""
    with pytest.raises(BookParseError):
        decode_pages("broken.epub", b"not a real epub", page_size=200)


def test_decode_pages_empty_txt_raises():
    """空 txt → 抛 BookParseError（解不出内容，不当一页空串读）。"""
    with pytest.raises(BookParseError):
        decode_pages("empty.txt", b"   \n  ", page_size=200)


def test_decode_pages_binary_garbage_raises_not_replace():
    """codex T3 ③：二进制（非 epub、所有编码 strict 解码都失败）→ fail-soft 抛错。

    现在非 .epub 一律当 txt、最后 errors=replace 兜底会把 PDF / 视频 / 任意二进制读成
    ��� 乱码硬塞进印象。改成：strict 解码全失败、又不是合法 epub → 抛 BookParseError
    （阅读 agent 据此整程 fail-soft、印象不动），绝不 errors=replace 把乱码当书。
    """
    pdf_like = b"%PDF-1.7\n\xff\xfe\x00\x80\x81\x82\x9f\xc0\xc1\xf5\xff" * 20
    with pytest.raises(BookParseError):
        decode_pages("某视频.mp4", pdf_like, page_size=200)


def test_decode_pages_pdf_filename_binary_raises():
    """.pdf 后缀 + 二进制内容（不是 txt 也不是 epub）→ fail-soft 抛错，不读成乱码。"""
    pdf_bytes = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n\xff\xfe\x80\x90\xa0\xb0" * 30
    with pytest.raises(BookParseError):
        decode_pages("说明书.pdf", pdf_bytes, page_size=200)


def test_parse_txt_binary_raises_not_replace():
    """parse_txt 对纯二进制（strict 解码全失败）抛 BookParseError，不 errors=replace 硬塞。"""
    garbage = b"\xff\xfe\x00\x80\x81\x9f\xc0\xc1\xf5\xfe\xff" * 50
    with pytest.raises(BookParseError):
        parse_txt(garbage)


def test_paginate_short_text_single_page():
    """短文本一页。"""
    pages = paginate("就这么一句话。", page_size=200)
    assert len(pages) == 1
    assert pages[0] == "就这么一句话。"


def test_parse_txt_gbk_fallback():
    """中文 txt 常见 GBK，解码兜底（不直接炸）。"""
    raw = "测试中文".encode("gbk")
    text = parse_txt(raw)
    assert "测试中文" in text


def test_parse_epub_extracts_spine_order():
    """epub 按 spine 顺序抽正文。"""
    data = _make_epub(title="t", chapters=["第一章", "第二章"])
    text = parse_epub(data)
    assert text.index("第一章") < text.index("第二章")


# ---------------------------------------------------------------------------
# 读时取字节（get-url + fetch），fail-soft 链路（codex 建议 3：覆盖预签成功但 GET 失败）
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_image_client(monkeypatch):
    """image_client.get_url 打桩（取 presigned URL）。"""
    state: dict = {"url": "https://tos.example/files/k?sig=x", "url_calls": []}

    async def fake_get_url(file_name):
        state["url_calls"].append(file_name)
        return state["url"]

    monkeypatch.setattr(rs.image_client, "get_url", fake_get_url)
    return state


@pytest.fixture
def stub_httpx(monkeypatch):
    """httpx GET 打桩：可控返回字节 / 抛错 / 非 2xx。"""
    state: dict = {"content": b"file bytes", "status": 200, "exc": None}

    class _FakeResp:
        def __init__(self):
            self.content = state["content"]
            self.status_code = state["status"]

        def raise_for_status(self):
            if self.status_code >= 400:
                import httpx

                raise httpx.HTTPStatusError(
                    "bad", request=None, response=_RespStub(self.status_code)
                )

    class _RespStub:
        def __init__(self, code):
            self.status_code = code
            self.text = "err"

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            if state["exc"] is not None:
                raise state["exc"]
            return _FakeResp()

    monkeypatch.setattr(rs.httpx, "AsyncClient", _FakeClient)
    return state


async def test_fetch_bytes_happy_path(stub_image_client, stub_httpx):
    """tos_file → get-url → httpx 取字节，成功返回原始字节。"""
    stub_httpx["content"] = b"the novel bytes"
    raw = await fetch_attachment_bytes(tos_file="files/file-k")
    assert raw == b"the novel bytes"
    assert stub_image_client["url_calls"] == ["files/file-k"], "get-url 只认 file_name"


async def test_fetch_bytes_no_url_returns_none(stub_image_client, stub_httpx):
    """get-url 拿不到 URL（对象还没缓存好 / 预签失败）→ None（fail-soft）。"""
    stub_image_client["url"] = None
    raw = await fetch_attachment_bytes(tos_file="files/file-k")
    assert raw is None, "拿不到 presigned URL → fail-soft None"


async def test_fetch_bytes_http_error_returns_none(stub_image_client, stub_httpx):
    """预签成功但 GET 非 2xx（对象其实不在 / 404）→ None（codex 建议 3：预签≠对象存在）。"""
    stub_httpx["status"] = 404
    raw = await fetch_attachment_bytes(tos_file="files/file-k")
    assert raw is None


async def test_fetch_bytes_timeout_returns_none(stub_image_client, stub_httpx):
    """GET 超时 / 抛错 → None（fail-soft）。"""
    import httpx

    stub_httpx["exc"] = httpx.TimeoutException("slow")
    raw = await fetch_attachment_bytes(tos_file="files/file-k")
    assert raw is None


async def test_fetch_bytes_empty_tos_file_returns_none(stub_image_client, stub_httpx):
    """tos_file 为空（从没回填进对象存储）→ 直接 None，不空打 get-url。"""
    raw = await fetch_attachment_bytes(tos_file="")
    assert raw is None
    assert stub_image_client["url_calls"] == [], "空引用不发起 get-url"
