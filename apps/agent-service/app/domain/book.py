"""Book — 赤尾读的一本书：分页正文 + 元信息（读小说 Task 1 的存储底子）.

你在飞书私聊把一个 txt/epub 文件发给她，系统解析、按「页」落库。「页」是**可按页号
稳定取回的有序单元**——Task 2 的阅读 agent 用 ``read(page_num)`` 一页页往后读、用
``total_pages`` 判读到了书尾。

两张表（照 notebook 的 Data 范式：Key 必带 ``lane``）：

  * :class:`BookMeta` —— 一本书的元信息：书名、总页数、内容 hash。自然键 ``(lane,
    book_id)``。``book_id`` 由 ``(lane, persona_id, content_hash)`` 派生（内容寻址），
    所以**同一份内容重传 = 同一个 book_id = 落同一本书**（判重的根）。
  * :class:`BookPage` —— 一页正文，一页一行。自然键 ``(lane, book_id, page_num)``。
    页是不可变的（书的正文不会改），所以无 Version，``insert_idempotent`` 落库——同书
    重传 / durable 重投用同一 ``(lane, book_id, page_num)`` 再写一次 ON CONFLICT DO
    NOTHING、不翻倍。

设计上钉死的几条：

  * **不给书建结构化理解**（章节梗概 / 人物表 / 检索索引）——那是 spec 明禁的「书本
    读取 agent」。这里只存「按页可取的有序正文」+「总页数」，仅此而已。
  * **分页是定长切分（段落边界对齐）**，不是按章。一程读多少由 Task 2 的机制安全阀
    收口，分页只负责把正文切成稳定可按号取的有序单元、并算出准确的 ``total_pages``。
  * **lane 进 Key**：runtime 持久化不自动加 lane，不显式带就会覆盖 prod 的书（写脏
    线上正文）。同其它 durable Data（NotebookEntry / EventEnvelope）。
  * **解析失败 fail-fast 抛 BookParseError**（不静默吞）：坏 epub / 空文件抛异常，
    接入层（book_ingest_node）据此回真人一条失败提示。

``total_pages`` 而非 ``page_count``：只是命名选择，不撞 runtime 保留列（id /
created_at / updated_at / dedup_hash）。``ingested_at`` 是书入库的现实时刻（业务字段，
不用 created_at 那个 runtime 落库时刻当业务语义，同 notebook 的 noted_at 教训）。
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path
from typing import Annotated

from app.runtime.data import Data, Key
from app.runtime.persist import insert_idempotent, select_latest


class BookParseError(Exception):
    """书解析失败（坏 epub / 解不出正文 / 空文件）。

    接入层捕获它、回真人一条失败提示（spec 失败口径：不静默吞）。是领域层的显式失败
    信号，不是 framework 异常。
    """


class BookMeta(Data):
    """一本书的元信息：书名 + 总页数 + 内容 hash。自然键 ``(lane, book_id)``。

    ``book_id`` 内容寻址（:func:`derive_book_id`）：同一份内容在同一 ``(lane, persona)``
    下派生同一个 book_id，重传落同一本（判重）。``content_hash`` 也存一列，便于排查 /
    将来按内容查。``total_pages`` 是 read 到书尾的判定依据（Task 2）。
    """

    lane: Annotated[str, Key]
    book_id: Annotated[str, Key]
    persona_id: str       # 这本书是发给哪个 persona 读的（推荐投谁的信箱）
    title: str            # 书名（epub 取 OPF 标题，txt 取文件名去扩展名）
    total_pages: int      # 总页数（read 到书尾判定：page_num >= total_pages 即越界）
    content_hash: str     # 正文内容 sha256（判重依据，book_id 由它派生）
    ingested_at: str      # 入库现实时刻 (ISO8601)


class BookPage(Data):
    """一页正文，一页一行。自然键 ``(lane, book_id, page_num)``。

    页不可变（书正文不会改），无 Version；``insert_idempotent`` 落库 —— 同书重传 /
    durable 重投用同一 ``(lane, book_id, page_num)`` 再写无害（ON CONFLICT DO NOTHING）。
    """

    lane: Annotated[str, Key]
    book_id: Annotated[str, Key]
    page_num: Annotated[int, Key]   # 0-based 页号
    content: str                    # 这一页的正文文本


# ---------------------------------------------------------------------------
# 解析：txt 直接读、epub 解 zip + 抽 spine XHTML 文字
# ---------------------------------------------------------------------------

# txt 中文常见编码，按序尝试解码（utf-8 优先，失败退 GBK 等）。最后兜底用 utf-8
# errors=replace（不静默炸，把不可解码字节替成占位符——总比解析整本失败好）。
_TXT_ENCODINGS = ("utf-8", "gb18030", "gbk", "big5", "utf-16")


def parse_txt(filename: str, raw: bytes) -> tuple[str, str]:
    """txt 直接读：解码字节成文本，书名取文件名（去扩展名）。返回 ``(text, title)``。

    按 :data:`_TXT_ENCODINGS` 顺序尝试解码（中文 txt 常见 GBK / GB18030），都不行
    退 utf-8 errors=replace 兜底。空文件（解出空白）抛 :class:`BookParseError`。
    """
    text: str | None = None
    for enc in _TXT_ENCODINGS:
        try:
            text = raw.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")
    if not text.strip():
        raise BookParseError("txt 文件解析出空内容")
    title = Path(filename).stem or "未命名"
    return text, title


def parse_epub(raw: bytes) -> tuple[str, str]:
    """epub 解析：按 spine 顺序抽出每章正文文本 + 取 OPF 标题。返回 ``(text, title)``。

    epub 是 zip 包 + OPF spine 串起 XHTML 章节。用 ``ebooklib`` 读 spine 顺序，逐个
    XHTML 文档用 ``BeautifulSoup``（项目已有依赖）剥标签抽纯文字、按 spine 序拼接。
    不是 zip / 解不出正文 → 抛 :class:`BookParseError`（接入层回真人失败提示）。

    只抽「按 spine 顺序的纯正文」——**不建章节结构 / 目录索引**（spec 明禁结构化理解，
    避免变成「书本读取 agent」）。分页交给 :func:`paginate`。
    """
    import ebooklib
    from bs4 import BeautifulSoup
    from ebooklib import epub

    try:
        book = epub.read_epub(io.BytesIO(raw))
    except (zipfile.BadZipFile, Exception) as exc:  # noqa: BLE001
        # ebooklib 对坏包抛各种异常（BadZipFile / KeyError / ...）；统一转 BookParseError
        # 让接入层只认一种失败信号。
        raise BookParseError(f"epub 解析失败：{exc}") from exc

    # spine 顺序：book.spine 是 [(item_id, linear), ...]，按它取 document 保证阅读顺序
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

    title = "未命名"
    meta_title = book.get_metadata("DC", "title")
    if meta_title and meta_title[0] and meta_title[0][0]:
        title = meta_title[0][0]
    return text, title


# 一页默认字符数。书不长（spec：不处理超长小说），定长切分 + 段落边界对齐即可，让每页
# 是稳定可按号取的有序单元。具体数值是形态选择、可调，不是语义阈值。
DEFAULT_PAGE_SIZE = 1800


def paginate(text: str, *, page_size: int = DEFAULT_PAGE_SIZE) -> list[str]:
    """把整篇正文切成有序的若干页（定长、段落边界对齐）。

    页是「可按页号稳定取回的有序单元」：按 ``page_size`` 字符攒页，**在段落边界
    （``\\n``）处断开**让每页是完整段落（除非单段超长才硬切）。返回的列表索引即页号，
    拼回去内容不丢。短文本就一页。
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
        # 单段就超过一页：先把当前页冲掉，再把超长段按 page_size 硬切成多页。
        if len(para) > page_size:
            flush()
            for i in range(0, len(para), page_size):
                pages.append(para[i : i + page_size])
            continue
        # 加上这段会超页 → 先冲掉当前页，这段开新页。
        if cur_len + len(para) + 1 > page_size and cur:
            flush()
        cur.append(para)
        cur_len += len(para) + 1
    flush()

    if not pages:
        # 全空白文本：归一成一页空串（调用方在 ingest 前已对空内容抛错，这里只兜底）。
        pages = [text]
    return pages


# ---------------------------------------------------------------------------
# 内容寻址 book_id + 入库 + 按页号取回
# ---------------------------------------------------------------------------


def content_hash(text: str) -> str:
    """正文文本的 sha256（判重依据）。同一份正文产同一个 hash。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def derive_book_id(*, lane: str, persona_id: str, content_hash: str) -> str:
    """由 ``(lane, persona_id, content_hash)`` 派生内容寻址的 book_id。

    同一份内容、同一 ``(lane, persona)`` → 同一个 book_id（重传判重的根）；lane 进派生
    保证泳道隔离。取 sha256 前 32 hex（够避撞、又不太长当 Key）。
    """
    h = hashlib.sha256(f"{lane}\x00{persona_id}\x00{content_hash}".encode()).hexdigest()
    return h[:32]


async def find_book_meta(*, lane: str, book_id: str) -> BookMeta | None:
    """读一本书的元信息（书名 / 总页数 / hash），不存在返回 ``None``。

    照 notebook ``find_notebook_entry`` 的姿势薄封 ``select_latest``（BookMeta 无
    Version，select_latest 退化成按 Key 取那一行）。lane 进 Key → 泳道隔离。
    """
    return await select_latest(BookMeta, {"lane": lane, "book_id": book_id})


async def find_books_by_title(
    *, lane: str, persona_id: str, title: str
) -> list[BookMeta]:
    """按 ``(lane, persona)`` 下书名模糊找书，返回所有命中候选（Task 2 读书工具用）。

    她在读书工具里**报书名**（自然语言、可能不精确），工具用它在**她自己**的书里
    （``(lane, persona_id)`` 隔离）做大小写无关的子串匹配，把命中的 BookMeta 全返回——
    **工具据此判**：命中恰一本就开读、命中零本 / 多本就回问让她说清，**绝不替她选一本**
    （代码替她决策违宪）。所以这里只负责「按书名捞候选」、不做任何排序 / 取第一个 /
    取最新（那是替她决策）。

    照 ``list_notebook_entries`` / ``list_relationship_pages`` 的先例在 framework 持久化
    写好的真实表上做只读 SELECT（DISTINCT ON 每本书取那一行——BookMeta 无 Version，每本
    一行）；写入仍走 ``insert_idempotent``，不绕开 framework 持久化原语。``title`` 空白
    （她没给书名）→ 返回空（工具回问）。
    """
    needle = title.strip()
    if not needle:
        return []

    from sqlalchemy import text

    from app.data.session import get_session
    from app.runtime.migrator import _table_name

    # 大小写无关子串匹配（ILIKE）；按 (lane, persona) 隔离。每本书一行（BookMeta 无
    # Version）。参数化 LIKE pattern，转义 needle 里的 LIKE 元字符（% / _ / \）防它们被
    # 当通配符（她报的书名是字面文字、不是模式）。
    escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    sql = (
        f"SELECT * FROM {_table_name(BookMeta)} "
        f"WHERE lane = :lane AND persona_id = :persona_id "
        f"AND title ILIKE :pattern ESCAPE '\\' "
        f"ORDER BY title ASC"
    )
    async with get_session() as s:
        r = await s.execute(
            text(sql),
            {
                "lane": lane,
                "persona_id": persona_id,
                "pattern": f"%{escaped}%",
            },
        )
        return [
            BookMeta(**{k: row[k] for k in BookMeta.model_fields})
            for row in r.mappings()
        ]


async def read_page(*, lane: str, book_id: str, page_num: int) -> str | None:
    """按页号取回一页正文（Task 2 阅读 agent 的 ``read(page_num)`` 底层）。

    越界页号（>= total_pages 或负）取回 ``None``——阅读 agent 据此知道读到了书尾。
    """
    page = await select_latest(
        BookPage, {"lane": lane, "book_id": book_id, "page_num": page_num}
    )
    return page.content if page is not None else None


def _parse(filename: str, raw: bytes) -> tuple[str, str]:
    """按文件名后缀分流到 txt / epub 解析。返回 ``(text, title)``。

    .epub 走 epub 解析，其余（含 .txt / 无后缀）按 txt 直接读——来源只有真人发的
    txt/epub 文件（spec：不自己上网搜书），后缀不是 epub 就当纯文本读。
    """
    if filename.lower().endswith(".epub"):
        return parse_epub(raw)
    return parse_txt(filename, raw)


async def ingest_book(
    *,
    lane: str,
    persona_id: str,
    filename: str,
    raw: bytes,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> str:
    """解析一个 txt/epub 文件、按页落库、返回内容寻址的 ``book_id``。

    流程：解析（按后缀分流，失败抛 :class:`BookParseError`）→ 算内容 hash → 派生
    book_id → 分页 → 逐页 + 元信息 ``insert_idempotent`` 落库。

    **同内容重传幂等**：book_id 由内容 hash 派生，重传走同一 book_id；BookMeta /
    BookPage 都 ``insert_idempotent``（ON CONFLICT DO NOTHING），重传不建第二本、页不
    翻倍、durable 重投也安全。
    """
    text, title = _parse(filename, raw)
    chash = content_hash(text)
    book_id = derive_book_id(lane=lane, persona_id=persona_id, content_hash=chash)
    pages = paginate(text, page_size=page_size)

    from app.infra import cst_time

    await insert_idempotent(
        BookMeta(
            lane=lane,
            book_id=book_id,
            persona_id=persona_id,
            title=title,
            total_pages=len(pages),
            content_hash=chash,
            ingested_at=cst_time.now_cst_iso(),
        )
    )
    for page_num, content in enumerate(pages):
        await insert_idempotent(
            BookPage(
                lane=lane,
                book_id=book_id,
                page_num=page_num,
                content=content,
            )
        )
    return book_id
