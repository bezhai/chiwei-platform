"""阅读 agent — 读一程她收到的一个文件、揉出她对它的新印象（读小说 Task 2）.

一个简单 ReAct agent（deepseek-v4-flash，**唯一工具** ``read(page_num)``）：外壳喂它
[这本书当前印象 + 从第几页接着读]，它在循环里往后 ``read`` 一程、最终输出 = 一篇揉好
旧印象的第一人称新印象正文。照 :mod:`app.agent.sediment` 的范式（任务指令 inline、系统
人设走 langfuse prompt、硬超时、独立成本入账、空产出拦截），但它**有工具、要往后读**。

读的是**她收到的一个文件**（不是注册的书）：没有书注册表，读的时候才从对象存储取这个
附件实例的字节、**现解码现分页**（见 :mod:`app.domain.reading_source`）。换的只是"读什么"
（一个读时取的文件）和"身份"（附件实例），下面的机制硬约束**原样**：

  * **读时取字节 + 现解码现分页**（决策 2）：进 :func:`run_reading_round` 先按 ``tos_file``
    取这个附件实例的字节（:func:`fetch_attachment_bytes`），用**原始 file_name** 现解码现
    分页（:func:`decode_pages`）。``total_pages`` = 解码出的页数（读时现算）。取不到字节
    （未缓存进对象存储 / 预签失败 / GET 失败）/ 解码失败 → 整程 fail-soft 返回 None
    （印象 / 页号都不动）。**不依赖任何书注册表**。

  * **进度从 read() 调用机制派生、且是「连续阅读前缀」模型**（项目红线 + 修复 A）：一程
    内只能从本程起始页往后连续读。``read`` 工具只接受页号等于「连续前沿」（下一页该读的
    那页）的调用；非连续 / 跳页 / 往回的 read 被挡（喂回引导）、不静默接受、不让进度跳过
    中间没读的内容。每成功喂出连续的一页就把前沿 +1。本程「读到第几页」= 连续前沿
    （= 起始页 + 连续真读到的页数），绝不解析 agent 文字里自报的页号。**前沿没推进过 →
    不提交**（印象 / 页号都不动，fail-soft）。

  * **EOF vs 数据缺损（修复 B）**：现切某页取到 None 时分两种——页号已达 / 超过
    ``total_pages`` 才是真书尾（``finished=True``、页号夹到 total 不越界）；页号还在范围内
    取到 None（解码出的页含缺损占位等）是数据缺损，**不置 finished、不当书尾**，前沿停在
    缺页前（fail-soft、下次从缺页重试）。绝不把一本书因为中间缺页误判成「读完了」。

  * **一程靠机制安全阀收口、不设语义页数预算**（用户明确「不规定读多少」）：硬 timeout
    （:data:`READING_TIMEOUT_SECONDS`）+ recursion_limit + 最多 read() 次数上限
    （:data:`MAX_READ_CALLS`）+ 读到书尾。任一触发即收口这一程。

  * **fail-soft**（spec）：取不到字节 / 解码失败 / 超时 / 抛错 / 空产出都返回 ``None``，
    外壳据此本程不算（不写半截脏印象、她可重读）。

  * **durable 重试安全**：``read`` 是只读工具（无 durable mutation），整轮重放只重读、
    不写脏。真正的 durable mutation（写印象 + 推进页号）在 @node 外壳里、本函数返回之后
    做，走版本 CAS（见 app/nodes/reading.py）。

  * **成本独立入账**：``collect_usage`` 包住 run、``record_round_cost(actor=
    f"{persona}:reading")``——阅读的 token 绝不混进 life 本体账。

模型 **deepseek-v4-flash**：``_READING_CFG`` 的 model_id 指向它（解析见 app/agent/models.py）。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from app.agent.context import AgentContext
from app.agent.core import Agent, AgentConfig
from app.agent.neutral import Message, Role
from app.agent.tooling import Tool
from app.agent.tools._common import tool_error
from app.agent.trace import collect_usage, make_session_id
from app.domain.reading_source import (  # module-level so tests can monkeypatch
    BookParseError,
    decode_pages,
    fetch_attachment_bytes,
)
from app.domain.thinking_cost import record_round_cost
from app.infra import cst_time
from app.memory._persona import load_persona  # module-level so tests can monkeypatch

logger = logging.getLogger(__name__)

# 阅读 agent 的硬超时（一程读多页 + 揉印象的工具循环）：机制安全阀。超时抛 TimeoutError
# → fail-soft 返回 None。
READING_TIMEOUT_SECONDS = 180

# 一程最多 read() 次数上限（机制安全阀，非语义页数预算）：到顶后 read 工具不再喂正文、
# 只回一句停读提示。**不规定她读多少**（用户明确）——这只是防失控的上界。
MAX_READ_CALLS = 40

# AgentConfig：deepseek-v4-flash + langfuse prompt id "book_reading_impression" +
# recursion_limit 给够（让它在一程里连续 read 多页再产出，不被默认 6 卡住）。
_READING_CFG = AgentConfig(
    "book_reading_impression",
    "deepseek-v4-flash",
    "book-reading",
    recursion_limit=MAX_READ_CALLS + 4,
)


@dataclass(slots=True)
class ReadingResult:
    """一程阅读的产出（外壳据它走 CAS 提交：整篇覆盖写印象 + 推进页号）。

    ``impression`` 揉好的新印象正文（非空——空产出在 :func:`run_reading_round` 已拦成
    None）。``pages_read`` 本程读完后「读到第几页」（= 连续阅读前沿 = 起始页 + 连续真读到
    的页数，机制派生）。``finished`` 是否读到了真书尾（现切到 None 且页号已达 total_pages）。
    """

    impression: str
    pages_read: int
    finished: bool


def reading_instruction() -> str:
    """阅读 + 印象生成的任务指令（代码侧只承载任务语义与输出形态；零剧情事实——宪法）。"""
    return (
        "你在读一本书。下面给你两样东西：你此前读这本书读到现在的印象（可能还没有，"
        "那就是你刚翻开它），和你现在该从第几页接着往下读。用 read(page_num) 这只手"
        "一页一页往后读——从给你的那一页开始，读你想读的一程，读到你这一程想停下的地方"
        "就停（读不读得完、读多少，你自己定；读到没有下一页了就是读到结尾了）。\n\n"
        "读完这一程，把你**此前的印象**和**这一程新读到的**揉成一篇新的印象，整篇重写、"
        "取代旧的那篇。写的是这本书在你心里激起了什么、你怎么看里头的人和事、哪一段留在"
        "你心上——是你的反应和感受，不是剧情梗概、不是复述情节。读得多了，早先的会变糊、"
        "近的会清楚，记岔了、忘了些也没关系，本就如此。只写你**真读到过**的，绝不编你没"
        "读到的情节。\n\n"
        "直接输出重写后的印象正文——第一人称、你自己的话，不要标题、不要列表、不要解释"
        "说明、不要报你读到第几页（页数不归你管）、不要任何机器标记。"
    )


def build_reading_tools(
    *, pages: list[str | None], total_pages: int, progress: dict
) -> list[Tool]:
    """造阅读 agent 的唯一工具 ``read(page_num)``，把现切页机制绑定 capture 进闭包。

    ``pages`` —— 读时现解码现分页得到的页列表（内存）。``read`` 从这里按页号现切。元素为
        ``None`` 表示该页是数据缺损占位（罕见，解码出含缺损时）——按数据缺损处理。
    ``total_pages`` —— 这本书的总页数（= ``len(pages)``，EOF vs 数据缺损判定）：现切到 None
        且页号已达 / 超过它才是真书尾。
    ``progress`` —— round-scoped 进度容器（外壳新建）。**进度从这里的机制事实派生**（连续
        阅读前沿），不从 agent 文字抠。容器键 ``frontier`` / ``reached_end`` / ``calls``。

    **连续前缀模型（修复 A）**：只接受页号等于 ``frontier`` 的 read（从起始页起一页页连续
    往后）。非连续 / 跳页 / 往回的调用被挡——回引导让模型读正确的下一页。

    ``read`` 叠 ``@tool_error``：自身抛错吞成结构化 outcome 喂回模型。它是**只读**工具
    （只从内存页列表现切、只更新进度容器，无任何 durable mutation），整轮重放只重读、
    不写脏——重试安全的根。
    """

    @tool_error("读这一页失败")
    async def read(page_num: int) -> str:
        """从你正在读的那一页往后读一页：给页号，返回这一页的正文。

        从外壳告诉你的那一页开始，**一页一页连续往后读**（page_num 严格 +1 递增、不跳页、
        不往回翻）。返回这一页的正文你就接着读；返回「没有下一页了」就是读到这本书的结尾
        了，别再往后读、该停下来揉你的印象了。

        Args:
            page_num: 要读的页号（= 你该读的下一页：从起始页起一页页 +1）。

        Returns:
            这一页的正文；到结尾时一句「没有下一页了」；翻乱了时一句提示你该读的下一页。
        """
        # 机制安全阀：超过本程最多 read 次数上限 → 不再喂正文，回停读提示（非语义页数
        # 预算）。进度不因被拦的调用推进。
        if progress["calls"] >= MAX_READ_CALLS:
            return "（这一程读得够多了，停下来，把读到的揉成你的印象吧。）"

        # 连续前缀护栏（修复 A）：只接受连续前沿那一页，跳页 / 往回 / 非连续都挡回引导。
        if page_num != progress["frontier"]:
            return (
                f"（一页一页连续往后读：你现在该读第 {progress['frontier']} 页"
                f"（用 read({progress['frontier']})），别跳页也别往回翻。）"
            )

        content = pages[page_num] if 0 <= page_num < len(pages) else None
        if content is None:
            if page_num >= total_pages:
                # 真书尾（越界页且页号已达 total_pages）：标到书尾、回提示停下揉印象。
                # 不推进前沿（这一页没有正文、不算读过）。
                progress["reached_end"] = True
                return "（没有下一页了——这本书你读到结尾了，停下来揉你的印象吧。）"
            # 数据缺损（修复 B）：页号还在 total_pages 范围内却没正文。不当书尾、不置
            # reached_end、不推进前沿——回提示让模型停下（外壳按 fail-soft 处理、下次重试）。
            return "（这一页暂时读不到——先停下来，把读到的揉成你的印象吧。）"

        # 真实喂出了连续的一页正文：前沿 +1，计一次喂出。
        progress["calls"] += 1
        progress["frontier"] += 1
        return content

    return [Tool(read)]


async def run_reading_round(
    *,
    lane: str,
    persona_id: str,
    attachment_id: str,
    book_title: str,
    tos_file: str,
    file_name: str,
    prior_impression: str | None,
    start_page: int,
    round_id: str,
) -> ReadingResult | None:
    """跑一程阅读：读时取字节 + 现解码现分页 + 硬超时 + 独立成本 + 空产出拦截。

    失败返回 ``None``（fail-soft）。外壳（app/nodes/reading.py 的 @node）调它，传
    [当前印象 + 从第几页接着读]。返回 :class:`ReadingResult` 供外壳走 CAS 提交；返回
    ``None`` = 本程失败（取不到字节 / 解码失败 / 超时 / 抛错 / 空产出），外壳据此**不动
    印象 / 页号**（她可重读）。

    ``attachment_id`` 这个附件实例的身份（外壳 / trace 用，本函数不反解它）。
    ``tos_file`` 对象存储引用（``files/<file_key>``），读时按它取字节。``file_name``
    原始文件名（解码分流靠它）。``round_id`` 触发这一程的 life 轮派生标识（成本 round_id）。
    """
    # 读时取字节（决策 2）：按 tos_file 从对象存储取这个附件实例的字节。取不到（未缓存 /
    # 预签失败 / GET 失败）→ fail-soft 返回 None：读不了一个还没准备好的附件，本程不算。
    raw = await fetch_attachment_bytes(tos_file=tos_file)
    if raw is None:
        logger.warning(
            "[reading] %s/%s attachment=%s round=%s bytes unavailable, fail-soft "
            "(impression/progress untouched)",
            lane, persona_id, attachment_id, round_id,
        )
        return None

    # 现解码现分页（按原始 file_name 分流）。解码失败（坏 epub / 空文件）→ fail-soft。
    try:
        pages = decode_pages(file_name, raw)
    except BookParseError as exc:
        logger.warning(
            "[reading] %s/%s attachment=%s round=%s decode failed, fail-soft: %s",
            lane, persona_id, attachment_id, round_id, exc,
        )
        return None
    total_pages = len(pages)

    # round-scoped 进度容器：read 工具按连续阅读前沿更新它，本函数据它派生 pages_read /
    # finished（机制事实，不靠 agent 文字）。frontier 初值 = 起始页。
    progress = {"frontier": start_page, "reached_end": False, "calls": 0}
    tools = build_reading_tools(pages=pages, total_pages=total_pages, progress=progress)

    pc = await load_persona(persona_id)
    now = cst_time.now_cst()
    prior_text = (
        prior_impression
        if prior_impression is not None and prior_impression.strip()
        else "（你还没读过这本书——这是你第一次翻开它。）"
    )
    user_content = (
        f"{reading_instruction()}\n\n"
        f"【你在读的书】《{book_title}》\n\n"
        f"【你此前读这本书的印象】\n{prior_text}\n\n"
        f"【从第几页接着读】第 {start_page} 页（用 read({start_page}) 开始往后读）"
    )
    # langfuse 归组：把这一程读书的 LLM 调用归进她当天的 session（trace 标签，不续接——
    # 读书是一次性整篇产出，run 不传 session_id）。
    session_id = make_session_id(lane, persona_id, now.strftime("%Y-%m-%d"))
    context = AgentContext(persona_id=persona_id, session_id=session_id)

    try:
        with collect_usage() as usage:
            result = await asyncio.wait_for(
                Agent(_READING_CFG, tools=tools).run(
                    [Message(role=Role.USER, content=user_content)],
                    prompt_vars={
                        "persona_name": pc.display_name,
                        "persona_lite": pc.persona_lite,
                    },
                    context=context,
                    max_retries=1,
                ),
                timeout=READING_TIMEOUT_SECONDS,
            )
    except Exception as exc:  # noqa: BLE001 — fail-soft：超时 / 抛错都印象 / 页号不动
        logger.warning(
            "[reading] %s/%s attachment=%s round=%s failed (impression/progress "
            "untouched, she can reread): %s",
            lane, persona_id, attachment_id, round_id, exc, exc_info=True,
        )
        return None

    # run 正常返回 → token 真烧了，成本照记（独立 actor，不混 life 本体账）。
    await record_round_cost(
        lane=lane,
        actor=f"{persona_id}:reading",
        round_id=round_id,
        usage=usage,
        observed_at=now.isoformat(),
    )

    impression = result.text().strip()
    if not impression:
        # 空产出拦截（机制安全阀）：空 / 半截印象覆盖整卷 = 真失忆。fail-soft 返回 None。
        logger.warning(
            "[reading] %s/%s attachment=%s round=%s empty impression, fail-soft "
            "(impression/progress untouched)",
            lane, persona_id, attachment_id, round_id,
        )
        return None

    # 进度从机制派生（spec 红线 + 修复 A）：本程「读到第几页」= 连续阅读前沿。
    #   * 读到过真书尾（reached_end）→ 提交 finished，**即便本程前沿没推进**（修复 ②，
    #     EOF 不卡死）：上一程正好读完最后一页、frontier 停在 total、状态仍 reading；下一程
    #     从书尾起只调越界页 read(total) → reached_end=True 但 frontier 没新推进。若沿用
    #     "前沿没推进就 drop"，这一程被丢弃、状态永远停在 reading、永不 finished。读到真书尾
    #     是真实终点（不是空转），即便没新前沿也提交 finished（印象非空已上面校验）。
    #   * 没读到书尾、且前沿没推进过（frontier == start_page）= 一页没真读到（纯空转：模型
    #     没 read 就产出 / 全是被挡的跳页 / 起始页就缺损）→ fail-soft 返回 None，不写脏印象。
    if not progress["reached_end"] and progress["frontier"] == start_page:
        logger.warning(
            "[reading] %s/%s attachment=%s round=%s no page truly read and not at EOF "
            "(frontier did not advance), fail-soft (impression/progress untouched)",
            lane, persona_id, attachment_id, round_id,
        )
        return None
    return ReadingResult(
        impression=impression,
        pages_read=progress["frontier"],
        finished=progress["reached_end"],
    )
