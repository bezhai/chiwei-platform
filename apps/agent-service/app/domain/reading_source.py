"""读时取字节 + 现解码现分页 + 附件实例身份 — 赤尾读一个文件（读小说 Task 2）.

没有"书"这个注册物。赤尾读的是她在飞书收到过的**一个文件**（common_message.content 里
一条普通文件项，和图片走同一条媒体轨）。"读"只发生在她单独发起的一程动作里：阅读 agent
读的时候才从对象存储取这个附件实例的字节、**现解码现分页**、一页页往后读。

本模块承载读时这层（不碰任何持久化 —— 没有书表）：

  * **附件实例身份**（决策 3）：``derive_attachment_id`` 由「收到该文件那次」派生 ——
    ``(common_message_id, file_key)``。**不是** ``tos_file``、**不是**内容 hash：对象存储可能
    按内容去重把多次重发指向同一份字节，拿它当身份会把本该独立的多份印象合并，破坏
    「重发两次 = 两个文件、两份印象」。派生出的 id 是 opaque 自然键，任何地方不反解它。

  * **读时取字节**（决策 2）：``fetch_attachment_bytes`` 走 ``tos_file``（``files/<file_key>``，
    = 对象存储 file_name）→ tool-service get-url 拿 presigned URL → httpx 取字节。取不到
    （未缓存 / 预签失败 / GET 非 2xx / 超时）→ ``None``，调用方据此整程 fail-soft（印象不动）。
    预签成功 ≠ 对象存在，所以 GET 仍可能 404 —— 那也归 fail-soft。

  * **现解码现分页**：``decode_pages`` 按**原始 file_name** 后缀分流（txt 解码 / epub 抽），
    分页定长 + 段落对齐。分流靠 file_name 不靠 file_key —— file_key 不保证带 ``.txt/.epub``。
    ``paginate`` 确定性（同内容同分页），跨轮稳定，连续前沿不错位。解析失败抛
    :class:`BookParseError`，阅读 agent 据此整程 fail-soft。

分页是定长切分（段落边界对齐），不是按章 —— 一程读多少由阅读 agent 的机制安全阀收口，
分页只把正文切成稳定可按号取的有序单元。**不建结构化理解**（章节梗概 / 人物表 / 索引）——
那是 spec 明禁的「书本读取 agent」。这里只把"一个文件"切成可按页号取的有序正文。
"""

from __future__ import annotations

import io
import logging
import zipfile

import httpx

from app.infra.image import image_client

logger = logging.getLogger(__name__)


class BookParseError(Exception):
    """文件解析失败（坏 epub / 解不出正文 / 空文件）。

    阅读 agent 现解码时捕获它 → 整程 fail-soft（印象 / 页号都不动）。不静默吞、也不替
    她说话（系统在这条链上永不向真人说话，spec 决策 4）。
    """


# ---------------------------------------------------------------------------
# 附件实例身份（决策 3）
# ---------------------------------------------------------------------------


def derive_attachment_id(*, common_message_id: str, file_key: str) -> str:
    """由「收到该文件那次」派生附件实例身份 = ``(common_message_id, file_key)``。

    opaque 自然键（任何地方不反解）：同一附件实例再读一程派生同一 id（印象覆盖重写）；
    重发（新 common_message_id）/ 同消息多文件（不同 file_key）各自唯一 id。绝不取自
    ``tos_file`` / 内容 hash（对象存储内容去重会把重发合并 → 多份印象并一份）。
    """
    return f"{common_message_id}:{file_key}"


# 与 tool-service file-pipeline 的**存储命名 wire 契约**：tool-service 收到文件时把字节
# 原样存进对象存储、file_name = ``files/<file_key>``（见 tool-service
# app/services/attachment_pipeline.py 的 _file_storage_name）。这是**确定性**的——file_key
# 本就在 content 文件项的 value 里，所以 agent-service 直接从 file_key 派生这个引用、
# **不依赖任何回填**（那条 image_key→file 回填机制是 image-only、对文件根本不跑，文件项的
# tos_file 恒空）。读时拿这个引用走 get-url 取字节。命名改动须与 tool-service 同步。
_TOS_FILE_PREFIX = "files/"


def derive_tos_file(file_key: str) -> str:
    """从 file_key 确定性派生对象存储引用 ``files/<file_key>``（与 tool-service 存储命名契约）。

    file-pipeline 回填机制是 image-only、对文件不跑 → 文件项 content 里恒无 tos_file。但
    TOS 命名确定（``files/<file_key>``），file_key 本就在文件项里，所以直接派生、不等回填。
    """
    return f"{_TOS_FILE_PREFIX}{file_key}"


# ---------------------------------------------------------------------------
# 读时取字节（决策 2）：tos_file → presigned URL → httpx 取字节，fail-soft
# ---------------------------------------------------------------------------

# 取字节的硬超时（presigned URL GET）：远小于阅读 agent 整程超时，取不到就 fail-soft。
_FETCH_TIMEOUT_SECONDS = 30.0


async def fetch_attachment_bytes(*, tos_file: str) -> bytes | None:
    """取这个附件实例的字节：``tos_file`` → get-url 拿 presigned URL → httpx 取字节。

    ``tos_file`` 是对象存储引用（``files/<file_key>``，由 channel-server 收到文件时 best-effort
    缓存进对象存储派生），也就是 tool-service get-url 认的 file_name。链路：

      1. ``tos_file`` 空（从没回填进对象存储）→ 直接 ``None``（不空打 get-url）。
      2. ``image_client.get_url(file_name=tos_file)`` 拿 presigned URL；拿不到（未缓存 /
         预签失败）→ ``None``。
      3. httpx GET 取字节；非 2xx（对象其实不在 / 404）/ 超时 / 抛错 → ``None``。

    任一步取不到都返回 ``None``（fail-soft），阅读 agent 据此整程不动印象 / 页号（她可重读）。
    **预签成功 ≠ 对象存在**，所以第 3 步仍要兜 GET 失败（codex 建议 3）。
    """
    if not tos_file:
        return None

    url = await image_client.get_url(tos_file)
    if not url:
        logger.warning("[reading_source] get-url returned no url for %s", tos_file)
        return None

    try:
        async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT_SECONDS) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content
    except httpx.TimeoutException:
        logger.warning("[reading_source] fetch timeout for %s", tos_file)
        return None
    except httpx.HTTPStatusError as e:
        logger.warning(
            "[reading_source] fetch HTTP %s for %s",
            e.response.status_code if e.response else "?",
            tos_file,
        )
        return None
    except Exception as e:  # noqa: BLE001 — fail-soft：取不到字节就当数据缺损、不穿透
        logger.warning("[reading_source] fetch failed for %s: %s", tos_file, e)
        return None


# ---------------------------------------------------------------------------
# 解码：txt 直接读、epub 解 zip + 抽 spine XHTML 文字
# ---------------------------------------------------------------------------

# txt 中文常见单 / 多字节编码，按序尝试 strict 解码（utf-8 优先，失败退 GBK 等）。**不
# errors=replace 兜底**——把 PDF / 视频 / 任意二进制 errors=replace 读成 ��� 乱码硬塞进
# 印象，比整本读不了更糟（spec：读不了就 fail-soft、印象不动；系统永不替她把垃圾当书）。
# **不含 UTF-16**：UTF-16 几乎能把任意偶数长字节流"解码成"貌似 CJK 的乱码（mp4 / pdf 二进制
# 被它强解成看着像中文的字），所以 UTF-16 只在 BOM 存在时单独尝试（见 parse_txt）。
_TXT_ENCODINGS = ("utf-8", "gb18030", "gbk", "big5")

# UTF-16 BOM（LE / BE）：真 UTF-16 文本文件带 BOM；只在见到 BOM 时才用 UTF-16 解码，
# 不拿它兜底猜二进制（否则二进制被 UTF-16 强解成貌似 CJK 的乱码、漏过校验）。
_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")

# "看起来不像文本"判定阈值：解码后控制 / 非文本字符占比超过它即判不是文本（多半是被宽容
# 编码强解的二进制）。正常 txt（含中文）这些字符极少。
_NON_TEXT_RATIO_CAP = 0.30


def _looks_like_text(text: str) -> bool:
    """启发式判一段解码结果像不像正文（挡住被宽容编码强解的二进制乱码）。

    两道：
      1. **Unicode 非字符**（``\\ufffe`` / ``\\uffff`` / 每平面末两个码位 / ``\\ufdd0``–
         ``\\ufdef``）：真文本永不含它们，出现一个就判不是文本（UTF-16 强解二进制常产出）。
      2. **控制 / 非文本字符占比**：C0 控制符（除 ``\\t\\n\\r``）、C1（``\\x80``–``\\x9f``）、
         替换符 ``\\ufffd``、私用区——占比超 :data:`_NON_TEXT_RATIO_CAP` 即判不是文本。
    """
    if not text:
        return False
    non_text = 0
    for ch in text:
        o = ord(ch)
        # Unicode 非字符：真文本绝不出现，命中即判二进制乱码。
        if o in (0xFFFE, 0xFFFF) or (o & 0xFFFF) in (0xFFFE, 0xFFFF) or 0xFDD0 <= o <= 0xFDEF:
            return False
        if ch in "\t\n\r":
            continue
        if o < 0x20 or 0x80 <= o <= 0x9F or ch == "�" or 0xE000 <= o <= 0xF8FF:
            non_text += 1
    return (non_text / len(text)) <= _NON_TEXT_RATIO_CAP


def parse_txt(raw: bytes) -> str:
    """txt 直接读：strict 解码、取第一个**像文本**的结果。

    候选编码 :data:`_TXT_ENCODINGS`（utf-8 / GBK 家族）按序 strict 解；**UTF-16 只在见到
    BOM 时**单独尝试（不拿它兜底猜二进制）。空文件（解出空白）抛 :class:`BookParseError`。
    所有候选 strict 解码都失败、或解出来的是二进制乱码（:func:`_looks_like_text` 不通过）→
    抛 :class:`BookParseError`（阅读 agent 据此整程 fail-soft、印象不动）。**绝不 errors=
    replace 把乱码当书**（codex T3 ③）。书名由调用方从原始 file_name 取，只返回正文文本。
    """
    encodings = list(_TXT_ENCODINGS)
    if raw[:2] in _UTF16_BOMS:
        encodings.append("utf-16")
    for enc in encodings:
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        if _looks_like_text(text):
            if not text.strip():
                raise BookParseError("txt 文件解析出空内容")
            return text
    raise BookParseError("文件不是可读文本（解码失败或像二进制）")


def parse_epub(raw: bytes) -> str:
    """epub 解析：按 spine 顺序抽出每章正文文本拼接成整篇正文。

    epub 是 zip 包 + OPF spine 串起 XHTML 章节。用 ``ebooklib`` 读 spine 顺序、逐个 XHTML
    用 ``BeautifulSoup`` 剥标签抽纯文字、按 spine 序拼接。不是 zip / 解不出正文 →
    :class:`BookParseError`。只抽「按 spine 顺序的纯正文」——不建章节结构 / 目录索引
    （spec 明禁结构化理解）。
    """
    import ebooklib
    from bs4 import BeautifulSoup
    from ebooklib import epub

    try:
        book = epub.read_epub(io.BytesIO(raw))
    except (zipfile.BadZipFile, Exception) as exc:  # noqa: BLE001
        raise BookParseError(f"epub 解析失败：{exc}") from exc

    parts: list[str] = []
    for item_id, _ in book.spine:
        item = book.get_item_with_id(item_id)
        if item is None or item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue
        soup = BeautifulSoup(item.get_content(), "html.parser")
        chunk = soup.get_text(separator="\n").strip()
        if chunk:
            parts.append(chunk)

    text = "\n\n".join(parts)
    if not text.strip():
        raise BookParseError("epub 解析出空正文")
    return text


# 一页默认字符数。定长切分 + 段落边界对齐，让每页是稳定可按号取的有序单元。具体数值是
# 形态选择、可调，不是语义阈值。
DEFAULT_PAGE_SIZE = 1800


def paginate(text: str, *, page_size: int = DEFAULT_PAGE_SIZE) -> list[str]:
    """把整篇正文切成有序的若干页（定长、段落边界对齐）。确定性 → 同内容同分页。

    页是「可按页号稳定取回的有序单元」：按 ``page_size`` 字符攒页，在段落边界（``\\n``）处
    断开让每页是完整段落（除非单段超长才硬切）。返回列表索引即页号，拼回去内容不丢。
    短文本就一页。**确定性切分**（无随机 / 无时间依赖）→ 同内容跨轮分页稳定，连续阅读
    前沿不错位。
    """
    paragraphs = text.split("\n")
    pages: list[str] = []
    cur: list[str] = []
    cur_len = 0

    def flush() -> None:
        nonlocal cur, cur_len
        if cur:
            pages.append("\n".join(cur))
            cur = []
            cur_len = 0

    for para in paragraphs:
        if len(para) > page_size:
            flush()
            for i in range(0, len(para), page_size):
                pages.append(para[i : i + page_size])
            continue
        if cur_len + len(para) + 1 > page_size and cur:
            flush()
        cur.append(para)
        cur_len += len(para) + 1
    flush()

    if not pages:
        pages = [text]
    return pages


def decode_pages(
    file_name: str, raw: bytes, *, page_size: int = DEFAULT_PAGE_SIZE
) -> list[str]:
    """现解码现分页：按**原始 file_name** 后缀分流（txt / epub）→ 分页成有序页列表。

    分流靠 ``file_name`` 后缀、不靠 ``tos_file`` / file_key —— Task 1 契约只保证原始文件名在
    ``meta.file_name`` 里，file_key 不保证带 ``.txt/.epub`` 后缀（codex 必改 3）。``.epub``
    走 epub 解析，其余（含 ``.txt`` / 无后缀）按 txt 直接读。返回的页列表索引即页号、
    ``len()`` 即 total_pages（阅读 agent 读时现算）。解析失败抛 :class:`BookParseError`
    （阅读 agent 整程 fail-soft）。
    """
    if file_name.lower().endswith(".epub"):
        text = parse_epub(raw)
    else:
        text = parse_txt(raw)
    return paginate(text, page_size=page_size)
