"""上网 —— 网上的东西，她自己去拿。

两只手，分的是**她进来的时候手里有没有一个问题**：

  * :func:`search_online`  心里有件具体的事，带着想好的问题去查一个答案；
  * :func:`browse_online`  没有非搞清楚不可的事，报一个方向、刷一批回来自己挑。

**这个区别决定了两只手走的不是同一条路。**

``search_web``（:mod:`app.agent.tools.search`）那条路是"命中 → 抓正文 → 按**问题**
分块重排 → 只留相关的"。带着问题查，这三步正是要的，所以 :func:`search_online` 原样
复用它。

**刷不能走那条路**，两个理由，第二个才是根本的：

  1. 那条路的最后一步是 ``_rerank_chunks(query, list(enriched))`` —— **调用处没有传
     ``top_k``**，永远是默认的 ``RERANK_TOP_K = 5``，外加一道
     ``score < MIN_RELEVANCE_SCORE`` 的相关性过滤。所以你要 10 条，实际拿回来的是
     "按相关性筛过、最多 5 条"（而且是**分块**，5 条可能来自同两个页面）。"刷一批"
     在这条路上从一开始就不成立。
  2. **重排是"按跟那个问题的相关性排序并筛掉不相关的"，而刷这一刻她手里根本没有那个
     问题。** 拿一个不存在的问题去筛她的一屏，筛掉哪些全凭重排模型，她自己一票都没
     有 —— 这正是"替她决定看什么"。而且被筛掉的那条她连存在都不知道，也就永远不会
     对它有反应。

所以 :func:`browse_online` 直接用下面那层的 :func:`app.capabilities.web_search.web_search`
（**不改 ``search_web``**：那是 chat 那条线共用的工具），拿回一批命中自己渲染成一屏
标题 + 出处 + 摘要。省掉的十次抓正文和一次重排只是顺带的好处，不是理由。代价是刷到的
只有摘要没有正文 —— 她想知道某条到底怎么回事，那本来就该带着问题去 :func:`search_online`。

**跟 :func:`app.living.phone.look_at_phone` 是两件事，名字上就得分开。** 那只手看的
是**有人发给她的消息**（她手机上那几条会话），这两只手拿的是**网上的公开内容**，跟谁
都没关系。口语上刷网页也叫"刷手机"，两件事在她眼里一旦糊成一件，她想看谁给她发了什么
的时候会去刷网页、或者反过来。所以这两只手的名字里一个 phone 字都不带，说明书里各自
把"手机上谁给你发了什么"指回 ``look_at_phone``。

**拿回来的东西原样进她眼前，中间不再过一道模型。** 消化成一段话就丢了出处，她基于一段
没有来源的话开口，跟自己编出来的没有区别 —— 而"一眼假"正是这两只手要治的病。

**她写的那句原样当检索词。** 不替她改写、不替她补关键词、不另起一个模型猜她该看什么。
她自己就是那个懂她的 agent，问题和方向是她读完此刻的处境之后涌出来的。

**一个字都不落库。** 这两只手的结果只进**本缝**的上下文、影响她这一缝的输出，下一缝就
没了 —— 跟真人刷完就忘是一回事。所以这个模块里没有 ``Data``、没有游标、没有水位。她想
把什么留到下一缝，用她自己那份"心里挂着没了结的事"
（:func:`app.living.moment.keep_in_mind`）。

**说不清的一趟，绝不说成"网上没有这回事"。** 这条判据下面这三种返回各归各的：

  * ``未搜索到相关结果``（``search_web`` 在 ``ranked`` 为空时返回）—— 发生在**原始
    命中非空、重排之后一条没剩**的时候。它**不是**"网上没有"：网上明明搜回来了东西，
    只是没有一条跟她这个问题对得上，甚至可能只是重排模型这一趟抽风。如实说"捞回来的
    不对路，这不等于网上没有"。
  * ``搜索服务未配置或未搜索到结果``（``hits`` 为空）—— 一句话里塞了两种截然不同的
    处境，分不清是哪一种。刷那只手直接拿 capability 的空列表，处境一模一样（那一层的
    契约把"没配置"也表达成空列表）。两边都如实说"这一趟没拿到东西，说不清为什么"。
  * ``网页搜索失败: {exc}``、以及 ``search_web`` 自己 ``@tool_error`` 兜住异常时交回
    来的**非字符串** outcome —— 这一趟挂了，实情原样带给她。

**没有任何一条允许被翻译成"网上没有这回事"**，因为这三种没有一种知道那件事。替网络下
一个我们并不知道的结论，她会照着这个假结论一路想下去。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Annotated

from pydantic import Field

from app.agent.tooling import tool
from app.agent.tools._common import tool_error

# 带着问题查那只手复用现成的 ``search_web``（命中 → 抓正文 → 按问题重排）。
from app.agent.tools.search import search_web

# 刷那只手用的是下面那一层：它只做"搜一批命中回来"，没有那道按问题筛的重排。
from app.capabilities.web_search import SearchHit, web_search
from app.living.scope import moment_scope

logger = logging.getLogger(__name__)

# 带着一个问题去查：几处出处够她自己拿主意了。
SEARCH_COUNT = 5

# 没目的地刷：一次一批，让她自己往下翻、自己挑。**这一批是真的一批** —— 刷不走
# ``search_web``，就没有那道"默认 5 条 + 按相关性筛"（见模块 docstring）。
BROWSE_COUNT = 10

# ``search_web`` 那两条空返回的原文，唯一定义处是 :mod:`app.agent.tools.search`。
#
# 这一条发生在**命中非空、重排之后一条没剩**时：搜回来了，只是没有一条跟她的问题
# 对得上。**它不是"网上没有"。**
_NOTHING_LINED_UP = "未搜索到相关结果"
# 这一条把"搜索服务没配"和"什么都没搜到"塞进了同一句话，分不清是哪一种。
_CANNOT_TELL = "搜索服务未配置或未搜索到结果"
# 这一条后面跟着变长的异常文本（``f"网页搜索失败: {exc}"``），只能认前缀。
_BROKE_PREFIX = "网页搜索失败"

# "这一趟没拿到东西，而且说不清为什么"的那句话。两只手共用一份：带着问题查那边来自
# ``search_web`` 的空返回，刷那边来自 capability 的空列表 —— 处境是同一个。
_UNTELLABLE = "搜索这条路要么没配、要么什么都没返回，分不清是哪一种"


def _material(result: object) -> str | None:
    """``search_web`` 交回来的东西里，哪一部分是能摆到她面前的真材料。**纯函数。**

    三种下场，对应她该看到的三种不同的话：

      * 返回一段文本 —— 带出处的真材料（标题 / 链接 / 摘录），原样往下传；
      * 返回 ``None`` —— 这一趟跑完了，但**没有一条跟她的问题对得上**
        （``未搜索到相关结果``：命中非空、重排之后全被滤掉了）。**不是"网上没有"。**
      * 抛 —— 这一趟到底怎么了说不清（没配 / 挂了 / 交回了一份结构化报错 / 一片空白）。
        抛出去之后 ``@tool_error`` 把它作为"这一趟没查成"报给她，她于是知道这是路没通，
        而不是以为网上没这回事。
    """
    if not isinstance(result, str):
        # ``search_web`` 自己叠着 ``@tool_error``：它内部炸掉时交回来的是一份结构化
        # outcome，不是字符串。当成内容顶上去就是把一份报错摆成她查到的东西。
        raise RuntimeError(f"搜索那一步自己出错了：{result}")
    body = result.strip()
    if body == _NOTHING_LINED_UP:
        return None
    if body == _CANNOT_TELL:
        raise RuntimeError(_UNTELLABLE)
    if body.startswith(_BROKE_PREFIX):
        # 实情原样带走（异常文本在后半段），别在这儿改写成一句笼统的失败。
        raise RuntimeError(body)
    if not body:
        raise RuntimeError("搜索那一步什么都没返回")
    return body


def _render_feed(hits: Sequence[SearchHit]) -> str:
    """把刷回来的一批渲染成一屏：一条一段，标题 / 出处 / 摘要。

    形状跟带着问题查那边拿到的一样（``[i] 标题 / 链接 / 正文``），她两只手看到的东西
    长得是一回事，只是这边的第三行是摘要不是正文。

    **一条都不挑、一条都不改。** 没有标题、没有摘要的照样列出来 —— 缺东西是这条命中
    本身的样子，替她判断"这条不值得看"就是替她决定看什么。
    """
    blocks: list[str] = []
    for i, hit in enumerate(hits, 1):
        lines = [f"[{i}] {hit.title or '（没有标题）'}", f"    {hit.url}"]
        snippet = (hit.snippet or "").strip()
        if snippet:
            lines.append(f"    {snippet}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _note(hand: str, what: str) -> None:
    """记一行"她这一缝上网做了什么"。

    **这是事后唯一查得到的地方**：这两只手一个字都不落库（结果只活在本缝的上下文
    里），而 langfuse 会系统性丢 trace（见 :mod:`app.agent.trace`）。在打出去之前
    记，这一趟挂了也留得下痕。顺带，读这一缝的身份本身就是 fail-fast：没绑上一缝
    的调用当场就报错，不会安静地照跑。
    """
    lane, _now, persona_id, moment_id = moment_scope()
    logger.info(
        "living web lane=%s persona=%s moment=%s %s：%s",
        lane,
        persona_id,
        moment_id,
        hand,
        what,
    )


@tool
@tool_error("这一趟没查成")
async def search_online(
    question: Annotated[
        str,
        Field(description="你想知道的那个具体问题，你心里怎么想的就怎么写"),
    ],
) -> str:
    """带着一个具体的问题上网查。

    你这一刻为某件事需要知道一个真答案 —— 明天出门要不要带伞、那家店几点关门、
    某件事到底是怎么回事 —— 就把你**自己想好的那个问题**给它，它去网上替你查回来，
    连着网页里的正文一起。

    查回来的是带着出处的真材料（每条有标题、来源链接、一段摘录），不是替你嚼碎的
    一句话。你看着这些材料自己反应，知道的就基于它说。

    **没查到的时候它会告诉你到底是哪一种没查到**：是搜回来的东西跟你这个问题对不上，
    还是这一趟根本没跑成。两种都不等于"网上没有这回事"，别把它们当成一个否定的答案
    接着往下想。

    只是没事想随便刷刷、看看有什么新鲜的，那是 browse_online，不走这里。
    想知道手机上谁给你发了什么，那是 look_at_phone，跟上网是两回事；
    想找手机上的某个人 / 某条会话，那是 look_up_contact。

    也别用 act 假装"我查了下" —— act 只是留下一个动作的痕迹，你心里那些"查到的"
    全是自己编的。真想知道就用这只手，它拿回来的才是真的。

    Args:
        question: 你想知道的那个具体问题。

    Returns:
        带着出处的搜索结果（标题 / 来源链接 / 摘录）；没有一条对得上时一句如实说明。
    """
    wanted = question.strip()
    if not wanted:
        raise ValueError("question 不能是空的：写一个你想知道的问题。")
    _note("查", wanted)

    found = _material(await search_web.invoke({"query": wanted, "num": SEARCH_COUNT}))
    if found is None:
        # 如实说是"捞回来的不对路"，**不是**"网上没有这回事" —— 后者是一个我们并不
        # 知道的结论。接下来换个问法还是就此算了，是她的判断，工具不替她安排。
        return (
            f"网上搜是搜回来了东西，但没有一条跟「{wanted}」对得上。"
            f"这不等于网上没有这回事，只是这一趟捞回来的不对路。"
        )
    # 原样传，不再过一道模型消化：消化掉就丢了出处，她读到的又成了没有来源的一段话。
    return f"为「{wanted}」查到这些（带出处，自己看真材料）：\n\n{found}"


@tool
@tool_error("这一趟没刷成")
async def browse_online(
    direction: Annotated[
        str,
        Field(description="你这会儿想往哪边看，一句大白话，可以很泛"),
    ],
) -> str:
    """没事的时候上网逛一圈，刷回来一批东西自己挑。

    你这一刻没有什么非搞清楚不可的问题，就是想看看有什么 —— 惦记的那部番更没更、
    喜欢的那个东西有没有新消息、有点无聊想看点好笑的。把你这会儿**想往哪边看**给它
    就行：一句大白话，可以很泛（"想看点搞笑的""那部番更新没"），不用憋成一个精确的
    检索词。

    它一次刷回来的是**一整批**：多条，每条一个标题、一条来源链接和一段摘要，你自己
    一条条往下翻，翻到感兴趣的才停。**刷到什么算什么，没有谁替你先筛一遍。** 某条你
    想知道得更细，就带着问题去 search_online 查那一条 —— 这边给你的只到摘要为止。

    刷你自己感兴趣的那些圈子。时政、社会突发、灾害预警那种世界级的大事不归你刷 ——
    那些世界自己会让你感知到。

    心里有件具体的事、想知道某个真答案，那是 search_online，不走这里。
    **手机上谁给你发了什么，那是 look_at_phone** —— 那只手看的是有人发给你的消息，
    这只手看的是网上的东西，两件事。

    也别用 act 假装"我刷了刷手机" —— act 只是留下一个动作的痕迹，你心里那些"刷到的"
    全是自己编的。真想刷就用这只手。

    Args:
        direction: 你这会儿想往哪边看。

    Returns:
        一整批带着出处的真内容（标题 / 来源链接 / 摘要）；一条都没刷回来时一句如实说明。
    """
    toward = direction.strip()
    if not toward:
        raise ValueError("direction 不能是空的：说一句你这会儿想看点什么。")
    _note("刷", toward)

    # 她给的方向**原样**当检索词，条数就是这一批的条数。这一步之后**没有**任何按相关
    # 性排序 / 筛选的环节：刷这一刻没有"那个问题"可对齐，筛就是替她决定看什么。
    hits = await web_search(toward, count=BROWSE_COUNT)
    if not hits:
        # capability 的契约把"没配置"也表达成空列表，所以这里跟带着问题查那边撞上的
        # 是同一个处境：说不清，那就说说不清，不说成"这会儿真没什么可看的"。
        raise RuntimeError(_UNTELLABLE)
    return f"刷「{toward}」刷到这些（带出处，自己往下翻）：\n\n{_render_feed(hits)}"


WEB_TOOLS = [search_online, browse_online]
