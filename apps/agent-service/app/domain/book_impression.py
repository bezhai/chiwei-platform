"""BookImpression — 赤尾对一本书的滚动印象（读小说 Task 2 的记忆底子）.

模拟一个**会读书的人**对一本书的记忆，不是做一个会读书的 agent：她对一本书的记忆是
**单条、滚动、覆盖式重写**的第一人称有损印象（靠 reactions、不靠 plot summary），读得
越多旧的越糊、近的越清。选它而不是「章节梗概 + 人物表」那种结构化档案——后者会把她变成
读取机器，丢掉「她是个读书的人」（spec key decision 1）。

一份印象 = 第一人称印象正文 + 读到第几页 + 开读后状态（在读 / 读完 / 放下）。印象 Data
只承载**开读后**的生命周期；「推荐未开读」不落任何状态（就是信箱里那条推荐消息 + 她
爱记不记的本子，见 book_ingest）。

设计上钉死的几条：

  * **as_latest + Version，Key 带 lane**：每读一程 ``insert_append`` 一版（整篇覆盖
    重写印象 + 推进页号），对外读永远 ``select_latest`` 取最新一版（旧版留作历史，不删）。
    Key 含 ``lane`` —— runtime 持久化不自动加 lane，不显式带就会覆盖 prod 的印象（写脏
    线上她的私人记忆），同其它 durable Data（NotebookEntry / LifeState）。

  * **提交走幂等 + 版本 CAS**（spec key decision 2，最承重）：``save_impression`` 带
    ``expected_ver`` 走 :func:`insert_append` 的 CAS（仿 ``replace_session(expected_ver=)``）
    —— 并发 / 过期任务 / durable 重投用过时 ver 写入会被拒，不覆盖更新的印象、不双推进
    页号。读小说一程是异步任务，重投 / 部署中断重放都不双推进，靠这条 CAS 守住。

  * **进度从机制派生、不从 LLM 文字抠**（项目红线）：``pages_read`` = 本次实际调用过
    ``read(page_num)`` 的最大页号 +1（= 下次从第几页接着读），由阅读任务的外壳 / 工具
    机制记录，**绝不靠阅读 agent 的文字自报页号**。读到 ``read_page`` 返回 None 即到
    书尾，状态置「读完」、页号不越界。

  * **一次一本「当前书」**（spec key decision「印象靠每轮注入 context」）：Task 3 注入只
    渲染**一本当前书** = 她最近读过一程、状态仍「在读」那一本（:func:`find_current_book_
    impression`）。她开读另一本，旧的自然不再最近、淡出注入，**不靠「N 天没读就丢」这种
    阈值替她遗忘**。

``observed_at`` 而非 ``updated_at``：``updated_at`` / ``created_at`` 是 migrator 自动加
的保留列，不能拿来当业务字段名（同 LifeState.observed_at / NotebookEntry.noted_at 教训）。
``pages_read`` 而非 ``page`` / ``cursor``：直白说「读到第几页（下次从这接着读）」。
"""

from __future__ import annotations

from typing import Annotated

from app.runtime.data import Data, Key, Version
from app.runtime.persist import insert_append, select_latest

# 三态协议常量（机制层硬定，不是让 LLM 猜的字符串）。开读后生命周期的三个取值。
# 单一定义处（宪法「禁止重复定义」）：写入处 / Task 3 渲染处都从这里取。
STATUS_READING = "reading"      # 在读（还会接着往下读 / 注入她的 context）
STATUS_FINISHED = "finished"    # 读完（读到书尾派生，不再是当前书）
STATUS_ABANDONED = "abandoned"  # 放下 / 弃书（她自己决定不读了，不再是当前书）

# 「还算当前在读」的态集合：Task 3 注入只渲染状态在此集合里、且最近读过一程那本。
# 读完 / 放下的不在其中（淡出注入）。单一定义处。
CURRENT_STATUSES = frozenset({STATUS_READING})


class BookImpression(Data):
    """赤尾对一本书的滚动印象。as_latest（带 Version），Key = (lane, persona_id, book_id)。

    一份印象 = 第一人称印象正文 + 读到第几页（下次从这接着读）+ 开读后状态。每读一程
    整篇覆盖重写（append 新版），读取取最新一版。Key 三键 = 泳道隔离 + 每人 + 每本书一条
    版本链。
    """

    lane: Annotated[str, Key]
    persona_id: Annotated[str, Key]
    book_id: Annotated[str, Key]
    ver: Annotated[int, Version] = 0
    impression: str       # 第一人称滚动印象正文（reactions、有损、揉进旧印象）
    pages_read: int       # 读到第几页：下次从这一页接着读（= 实际 read 过的最大页号 +1）
    status: str           # 开读后生命周期：reading / finished / abandoned
    observed_at: str      # 这版印象写下的现实时刻 (ISO8601)
    # 提交这一版的那次异步阅读任务的 request_id（从 life 触发轮派生）。**@node turn 幂等
    # 查重的载荷**：阅读 @node 在跑昂贵的阅读 agent 之前，先读当前印象、若 last_request_id
    # 已等于本次触发的 request_id 说明这一程已提交过 → 跳过、不重复跑（仿 life_wake 的
    # round marker 查重，但 marker 落在 durable 印象行上）。首版默认空串。
    last_request_id: str = ""


async def save_impression(
    *,
    lane: str,
    persona_id: str,
    book_id: str,
    impression: str,
    pages_read: int,
    status: str,
    observed_at: str,
    expected_ver: int,
    request_id: str = "",
) -> bool:
    """读完一程、整篇覆盖写回印象 + 推进页号 → CAS append 一版。返回是否写入成功。

    **版本 CAS 命门（spec key decision 2）**：``expected_ver`` 是调用方读到的最新一版
    的 ``ver``（首次开读为 0，对齐 :func:`insert_append` 的 ``COALESCE(MAX(ver), 0)``
    base）。写入只在库里当前 ``MAX(ver)`` 仍等于 ``expected_ver`` 时落地（校验与写入是
    同一条 SQL、无 TOCTOU，见 :func:`insert_append`）。返回 ``True`` = 写入了，``False``
    = 期间有人 append（并发 / 过期任务 / 部署中断重放抢先），**本次放弃、不覆盖更新的
    印象、不双推进页号**——调用方据此走 fail-soft（这一程不算、她可重读）。

    选 CAS 而不是无条件 append：读一程是异步任务，durable 重投 / 部署中断重放都可能让
    一个拿着过时印象的任务在更新的印象之后再写一次——无条件 append 会用旧印象盖掉新的、
    把页号回退或双推进。CAS 让「基于过时读」的写入被拒，是这一程提交幂等的根。

    ``request_id`` 落进 ``last_request_id`` 列，是阅读 @node turn 幂等查重的载荷（@node
    跑昂贵的阅读 agent 之前先比对它，已提交过就跳过、不重复跑）。
    """
    written = await insert_append(
        BookImpression(
            lane=lane,
            persona_id=persona_id,
            book_id=book_id,
            impression=impression,
            pages_read=pages_read,
            status=status,
            observed_at=observed_at,
            last_request_id=request_id,
        ),
        expected_current_ver=expected_ver,
    )
    return written == 1


async def find_book_impression(
    *, lane: str, persona_id: str, book_id: str
) -> BookImpression | None:
    """读某 persona 对某本书的最新一版印象，没有则 ``None``（她还没开读这本）。

    照 notebook ``find_notebook_entry`` 的姿势薄封 ``select_latest`` 取最新一版。阅读任务
    的外壳读它拿「当前印象 + 从第几页接着读」喂给阅读 agent；CAS 提交也读它拿
    ``expected_ver``。lane 进 Key → 泳道隔离。
    """
    return await select_latest(
        BookImpression,
        {"lane": lane, "persona_id": persona_id, "book_id": book_id},
    )


async def find_current_book_impression(
    *, lane: str, persona_id: str
) -> BookImpression | None:
    """取这个 persona「当前在读那一本」的印象 = 最近读过一程、状态仍「在读」那本。

    Task 3 注入只渲染**一本当前书**（spec key decision）：她开读另一本，旧的自然不再
    最近、淡出注入，**不靠阈值替她遗忘**。「当前在读」= 每本书取最新一版后，筛出状态在
    :data:`CURRENT_STATUSES`（在读）的，按 ``observed_at`` 取最近写过的那一本。读完 /
    放下的不在其中（不再是当前书）。无在读书 → ``None``（整段缺席、Task 3 不补占位）。

    照 ``list_notebook_entries`` 的先例在 framework 持久化写好的真实表上做只读 SELECT
    （DISTINCT ON 每本书取最新一版）；写入仍走 :func:`save_impression`，不绕开 framework
    持久化原语。``observed_at`` 比较的是 ISO8601 字符串——同一 persona 的印象都由阅读任务
    用 CST aware ISO 写入（同格式可字典序比较即时序），取字典序最大那条 = 最近读过一程。
    """
    from sqlalchemy import text

    from app.data.session import get_session
    from app.runtime.migrator import _table_name

    # DISTINCT ON 每本书取最新一版（ver 最大），再在 Python 侧筛在读 + 取 observed_at
    # 最近那本。只读、不改任何状态。
    sql = (
        f"SELECT DISTINCT ON (book_id) * FROM {_table_name(BookImpression)} "
        f"WHERE lane = :lane AND persona_id = :persona_id "
        f"ORDER BY book_id ASC, ver DESC"
    )
    async with get_session() as s:
        r = await s.execute(text(sql), {"lane": lane, "persona_id": persona_id})
        rows = [
            BookImpression(**{k: row[k] for k in BookImpression.model_fields})
            for row in r.mappings()
        ]
    reading = [imp for imp in rows if imp.status in CURRENT_STATUSES]
    if not reading:
        return None
    return max(reading, key=lambda imp: imp.observed_at)


# 注入她 context 的「在读的书」段标头（平直第一人称框架文案，零剧情事实——书名 / 印象
# 正文都是参数传进来的真实内容，不写进模板；宪法同 notebook / arc 透传）。
_READING_HEADER = "【你正在读的那本书】"


def render_reading_impression(impression: BookImpression, *, title: str) -> str:
    """把当前在读那本书渲成给模型看的一段文字 = 书名 + 这本书此刻在她心里的印象正文。

    **单一定义处**（宪法「禁止重复定义」）：Task 3 注入两处（life 唤醒 stimulus + chat
    inner_context）共用这一份渲染——在读的书是同一份内容，渲染只该有一处（照
    ``render_notebook`` 的先例）。

    渲染只管把有的东西如实渲出来：书名 + 她自己的第一人称滚动印象正文，措辞中性、不加
    框架腔的评判（她对这本书是什么感受由印象正文自己说，渲染不替她下结论）。**无书时
    的处理交给调用方**（返回空段、整段缺席），本函数只在有当前书时被调、必产非空文字。
    """
    return f"{_READING_HEADER}《{title}》\n{impression.impression}"
