"""阅读 agent — 读一程书、揉出她对这本书的新印象（读小说 Task 2）.

一个简单 ReAct agent（deepseek-v4-flash，**唯一工具** ``read(page_num)``）：外壳喂它
[这本书当前印象 + 从第几页接着读]，它在循环里往后 ``read`` 一程、最终输出 = 一篇揉好
旧印象的第一人称新印象正文。照 :mod:`app.agent.sediment` 的范式（任务指令 inline、系统
人设走 langfuse prompt、硬超时、独立成本入账、空产出拦截），但它**有工具、要往后读**。

机制硬约束（spec key decision 2「读一程靠机制收口、进度从 read() 机制派生」）：

  * **进度从 read() 调用机制派生、且是「连续阅读前缀」模型**（项目红线 + 修复 A）：一程
    内只能**从本程起始页往后连续读**（起始页、起始页+1、……）。``read`` 工具只接受页号
    等于「连续前沿」（下一页该读的那页）的调用；非连续 / 跳页 / 往回的 read 被挡（喂回
    一句引导让模型读正确的下一页，不静默接受、不让进度跳过中间没读的内容）。每成功喂出
    连续的一页就把前沿 +1。本程「读到第几页」= 连续前沿（= 起始页 + 连续真读到的页数），
    绝不解析 agent 文字里自报的页号。**前沿没推进过（= 起始页、即一页没真读到）→ 不提交**
    （印象 / 页号都不动，fail-soft，见 :func:`run_reading_round`）。

  * **EOF vs 数据缺损（修复 B）**：``read_page`` 返 None 时分两种——页号已达 / 超过
    ``total_pages`` 才是真书尾（``finished=True``、页号夹到 total 不越界）；页号还在
    ``total_pages`` 范围内是数据缺损（书被删 / 部分入库失败），**不置 finished、不当书尾**，
    前沿停在缺页前（按 fail-soft 处理，下次从缺页重试），绝不把一本书因为中间缺页误判成
    「读完了」。``total_pages`` 由 :func:`find_book_meta` 查；meta 查不到 → 整程 fail-soft
    返回 None（没法安全区分 EOF 与缺页、也读不了一本不存在的书）。

  * **一程靠机制安全阀收口、不设语义页数预算**（用户明确「不规定读多少」）：硬 timeout
    （:data:`READING_TIMEOUT_SECONDS` 的 asyncio.wait_for 包住 run）+ recursion_limit
    （AgentConfig）+ 最多 read() 次数上限（:data:`MAX_READ_CALLS`）+ 读到书尾。任一触发
    即收口这一程。

  * **fail-soft**（spec：阅读 agent 失败 → 印象 / 页号都不动）：超时 / 抛错 / 空产出都
    返回 ``None``，外壳据此本程不算（不写半截脏印象、她可重读）。**绝不**写空 / 半截
    印象——空印象覆盖整卷 = 真失忆（同 sediment 空产出拦截）。

  * **durable 重试安全**：``read`` 是**只读**工具（无 durable mutation），所以阅读 agent
    本身可安全 retry（整轮重放只重读、不写脏）。真正的 durable mutation（写印象 + 推进
    页号）在 @node 外壳里、在本函数返回**之后**做，走版本 CAS（见 app/nodes/reading.py）。

  * **成本独立入账**：``collect_usage`` 包住 run、``record_round_cost(actor=
    f"{persona}:reading")``——阅读的 token 绝不混进 life 本体账（同 sediment 的
    ``{persona}:sediment`` 独立 actor）。

模型 **deepseek-v4-flash**：``_READING_CFG`` 的 model_id 指向它。它是 ModelMapping 别名
/ ``provider:model`` 直引（解析见 app/agent/models.py）——provider 凭证 / 适配器是 ops
预置的前置，不是代码能搞定的（不硬编凭证，项目禁令）。
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
from app.domain.book import (  # module-level so tests can monkeypatch
    find_book_meta,
    read_page,
)
from app.domain.thinking_cost import record_round_cost
from app.infra import cst_time
from app.memory._persona import load_persona  # module-level so tests can monkeypatch

logger = logging.getLogger(__name__)

# 阅读 agent 的硬超时（一程读多页 + 揉印象的工具循环）：机制安全阀，远小于 life 单飞锁
# TTL（600s）——读书任务是 life 触发的独立异步任务、不占 life 锁，但取同量级保守上界，
# 让一程绝不无限挂。超时抛 TimeoutError → fail-soft 返回 None。
READING_TIMEOUT_SECONDS = 180

# 一程最多 read() 次数上限（机制安全阀，非语义页数预算）：到顶后 read 工具不再喂正文、
# 只回一句「读够了、该停下揉印象」提示，把模型从「一直往后读读不停」收口回来。**不规定
# 她读多少**（用户明确）——这只是防失控的上界，正常一程远够不着。具体数值是形态选择、
# 可调，不是语义阈值（不替她决定读多少，只防机制失控）。
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
    的页数，机制派生）。``finished`` 是否读到了真书尾（read 到 None 且页号已达 total_pages）
    ——外壳据它把状态置「读完」。
    """

    impression: str
    pages_read: int
    finished: bool


def reading_instruction() -> str:
    """阅读 + 印象生成的任务指令（代码侧只承载任务语义与输出形态；零剧情事实——宪法）。

    写作纪律（第一人称、reactions 不是 plot summary、有损滚动、揉进旧印象、绝不编没读到
    的）由 langfuse prompt（系统人设）+ 本指令共同约束；本指令承载任务语义与输出形态。
    """
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
    *, lane: str, book_id: str, total_pages: int, progress: dict
) -> list[Tool]:
    """造阅读 agent 的唯一工具 ``read(page_num)``，把机制绑定 capture 进闭包（连续前缀模型）。

    ``lane`` / ``book_id`` —— 读哪本书（capture 进闭包，模型只填 page_num）。
    ``total_pages`` —— 这本书的总页数（EOF vs 数据缺损判定，修复 B）：read 到 None 且页号
        已达 / 超过它才是真书尾。
    ``progress`` —— round-scoped 进度容器（外壳新建）。**进度从这里的机制事实派生**（连续
        阅读前沿），不从 agent 文字抠（项目红线）。容器键：
          * ``frontier``：连续阅读前沿 = 下一页该读的页号（初值 = 本程起始页）。每成功喂出
            连续的一页就 +1。本程「读到第几页」= 它（= 起始页 + 连续真读到的页数）。
          * ``reached_end``：是否读到过真书尾（read_page 返 None 且页号 >= total_pages）。
          * ``calls``：已喂出正文的 read 次数（机制安全阀 :data:`MAX_READ_CALLS` 用）。

    **连续前缀模型（修复 A）**：只接受页号等于 ``frontier`` 的 read（从起始页起一页页连续
    往后）。非连续 / 跳页 / 往回的调用被挡——回一句引导让模型读正确的下一页，不静默接受、
    不让进度跳过中间没读的内容。这样「碰过一个高页号」绝不会让进度跳过中间未读的页。

    ``read`` 叠 ``@tool_error``：自身抛错吞成结构化 outcome 喂回模型（不炸整轮）。它是
    **只读**工具（调 :func:`read_page`、只更新进度容器，无任何 durable mutation），所以
    阅读 agent 整轮重放只会重读、不写脏——重试安全的根。
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
        # 机制安全阀：超过本程最多 read 次数上限 → 不再喂正文，回一句停读提示把模型
        # 收口回来揉印象（非语义页数预算，只防「读不停」失控）。进度不因被拦的调用推进。
        if progress["calls"] >= MAX_READ_CALLS:
            return "（这一程读得够多了，停下来，把读到的揉成你的印象吧。）"

        # 连续前缀护栏（修复 A）：只接受连续前沿那一页，跳页 / 往回 / 非连续都挡回引导，
        # 不喂正文、不推进前沿——绝不让进度跳过中间没读的内容。
        if page_num != progress["frontier"]:
            return (
                f"（一页一页连续往后读：你现在该读第 {progress['frontier']} 页"
                f"（用 read({progress['frontier']})），别跳页也别往回翻。）"
            )

        content = await read_page(lane=lane, book_id=book_id, page_num=page_num)
        if content is None:
            if page_num >= total_pages:
                # 真书尾（越界页且页号已达 total_pages）：标到书尾、回提示停下揉印象。
                # 不推进前沿（这一页没有正文、不算读过）。
                progress["reached_end"] = True
                return "（没有下一页了——这本书你读到结尾了，停下来揉你的印象吧。）"
            # 数据缺损（修复 B）：页号还在 total_pages 范围内却没正文（书被删 / 部分入库
            # 失败）。不当书尾、不置 reached_end、不推进前沿——回提示让模型停下（这一程
            # 读到这里读不下去了，外壳会按 fail-soft 处理、不写脏进度，下次从这页重试）。
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
    book_id: str,
    book_title: str,
    prior_impression: str | None,
    start_page: int,
    round_id: str,
) -> ReadingResult | None:
    """跑一程阅读：硬超时 + 独立成本 + 进度机制派生 + 空产出拦截。失败返回 ``None``（fail-soft）。

    外壳（app/nodes/reading.py 的 @node）调它，传 [当前印象 + 从第几页接着读]。返回
    :class:`ReadingResult`（印象正文 + 派生页号 + 是否到书尾）供外壳走 CAS 提交；返回
    ``None`` = 本程失败（超时 / 抛错 / 空产出），外壳据此**不动印象 / 页号**（她可重读）。

    ``round_id`` 是触发这一程的 life 轮派生标识（成本 round_id 用它幂等）。

    成本口径（同 sediment）：run 正常返回（含空产出）→ token 真烧了、照记；run 抛错 /
    超时被掐 → 不记（usage 不完整）。
    """
    # 修复 B/orphan：先查书 meta 拿 total_pages（EOF vs 数据缺损判定的依据）。meta 查不到
    # （书被删 / 入库回滚）→ fail-soft 返回 None：没法安全区分 EOF 与缺页、也读不了一本
    # 不存在的书，本程不算（外壳据此不动印象 / 页号）。
    meta = await find_book_meta(lane=lane, book_id=book_id)
    if meta is None:
        logger.warning(
            "[reading] %s/%s book=%s round=%s meta not found, fail-soft "
            "(impression/progress untouched)",
            lane, persona_id, book_id, round_id,
        )
        return None

    # round-scoped 进度容器：read 工具按连续阅读前沿更新它，本函数据它派生 pages_read /
    # finished（机制事实，不靠 agent 文字）。frontier 初值 = 起始页（还没连续读到任何页）。
    progress = {"frontier": start_page, "reached_end": False, "calls": 0}
    tools = build_reading_tools(
        lane=lane, book_id=book_id, total_pages=meta.total_pages, progress=progress
    )

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
        # 超时（asyncio.wait_for 抛 TimeoutError）/ LLM 抛错都收在这里 fail-soft 返回
        # None，外壳据此本程不算（她可重读）。CancelledError 等 BaseException 不在
        # Exception 范围内，照常向上传（让 runtime 干净 unwind，不被当成阅读失败吞掉）。
        logger.warning(
            "[reading] %s/%s book=%s round=%s failed (impression/progress untouched, "
            "she can reread): %s",
            lane, persona_id, book_id, round_id, exc, exc_info=True,
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
        # 空产出拦截（机制安全阀，不是内容检测器）：空 / 半截印象覆盖整卷 = 真失忆。
        # fail-soft 返回 None，外壳不动印象 / 页号（同 sediment 空产出拦截）。
        logger.warning(
            "[reading] %s/%s book=%s round=%s empty impression, fail-soft "
            "(impression/progress untouched)",
            lane, persona_id, book_id, round_id,
        )
        return None

    # 进度从机制派生（spec 红线 + 修复 A）：本程「读到第几页」= 连续阅读前沿。
    #   * 前沿没推进过（frontier == start_page）= 一页没真读到（模型没 read 就产出 /
    #     全是被挡的跳页 / 全被上限拦 / 起始页就缺损）→ fail-soft 返回 None，外壳据此不
    #     提交（印象 / 页号都不动、不写脏印象，她可重读）。
    #   * 读到过真书尾（reached_end）→ frontier 已停在最后有效页 +1（= total_pages）、不越界。
    if progress["frontier"] == start_page:
        logger.warning(
            "[reading] %s/%s book=%s round=%s no page truly read (frontier did not "
            "advance), fail-soft (impression/progress untouched)",
            lane, persona_id, book_id, round_id,
        )
        return None
    return ReadingResult(
        impression=impression,
        pages_read=progress["frontier"],
        finished=progress["reached_end"],
    )
