"""上网 —— 她带着问题去查，或者没事逛一圈。

**替身站在 capability 那一层，不换掉 ``search_web``。** 上一版把整只 ``search_web``
换成替身，于是它自己那套（抓正文 → 分块重排 → 两种不一样的空返回）在测试里一次都没
跑过，两条真缺陷全被照不到：

  * 刷那只手要 10 条，``search_web`` 里 ``_rerank_chunks(query, list(enriched))``
    **没传 top_k**，永远是默认的 5，还带一道 ``score < MIN_RELEVANCE_SCORE`` 的相关性
    过滤 —— 她要的一批实际是"按跟检索词的相关性筛过、最多 5 条"。"掺进去的条目照样
    出现"那条用例当时绿，只是因为替身把摆好的那一批原样交了回来。
  * ``未搜索到相关结果`` 被当成了"网上没有这回事"，而它恰恰发生在**原始命中非空、
    重排之后一条没剩**的时候。

所以现在替身换在下面那一层（``_web_search_capability`` / ``_read_webpage_capability``
/ ``_rerank_capability``，以及刷那只手自己引到的 capability）：``search_web`` 的真实
代码照跑，重排真的会砍、两种空返回真的会分别出现。到 HTTP 那一跳为止都是真的。

五条硬边界，各有对应的用例：

  * **拿回来的是带出处的真材料，原样进她眼前。**
  * **她写的那句原样当检索词。**
  * **刷回来的一批一条不筛**，也不按相关性砍成 5 条。
  * **说不清的一趟绝不说成"网上没有这回事"** —— 那是替网络下一个我们并不知道的结论。
  * **一个字都不落库。**
"""
from __future__ import annotations

import pytest

from app.agent.tools import search as search_mod
from app.capabilities.web_search import SearchHit
from app.living import web as web_mod
from app.living.web import (
    BROWSE_COUNT,
    SEARCH_COUNT,
    WEB_TOOLS,
    browse_online,
    search_online,
)

# 一屏刷出来的东西：**故意**排成"不该她刷的那些在后面"（时政 / 灾害预警）。
# ``search_web`` 那条路的重排砍在第 5 条，所以只要刷还走它，后面这几条必然掉。
_FEED_TITLES = (
    "深夜食堂第四季定档",
    "某款独立游戏发售",
    "手冲咖啡入门器具怎么选",
    "这只猫把主人的键盘坐塌了",
    "本周新番补番指南",
    "台风蓝色预警生效",
    "两国元首会晤",
    "邻市化工厂起火",
    "某地明日停水通知",
    "年度游戏提名公布",
)
_FEED = tuple(
    SearchHit(
        title=title,
        url=f"https://feed.example/{i}",
        snippet=f"{title}的摘要。",
    )
    for i, title in enumerate(_FEED_TITLES, 1)
)

# 带着问题查回来的命中（真路径下还会被抓正文 + 重排）。
_HITS = (
    SearchHit(
        title="广州天气预报",
        url="https://weather.example/gz",
        snippet="明天有雨。",
    ),
    SearchHit(
        title="中国天气网",
        url="https://weather.example/cn",
        snippet="华南多云转雨。",
    ),
)


def _page_of(url: str) -> str:
    """抓回来的那一页正文 —— 每页各不相同，好在断言里指认是哪一页。"""
    return f"{url} 这一页写着：明天白天有雨，最高 31 度。"


class _Wired:
    """这一趟里下面每一层各被调了什么。

    "刷没有走重排"这条断言就靠它：走没走过那两步昂贵操作，看的是调用记录，不是看
    输出长什么样。
    """

    def __init__(self) -> None:
        self.searched: list[tuple[str, int]] = []  # search_web 底下那次搜索
        self.browsed: list[tuple[str, int]] = []   # 刷那只手自己发起的搜索
        self.read: list[str] = []                  # 抓正文
        self.reranked: list[tuple[str, int]] = []  # 重排（检索词, 文档数）


@pytest.fixture
def wire_web(monkeypatch):
    """把替身装在 capability 那一层。

    ``hits`` 给一个异常实例就是"这一跳自己炸了"。``score`` 是重排给每条打的分：
    低于 ``search_web`` 的 ``MIN_RELEVANCE_SCORE`` 时，它真实的代码会把结果全滤掉、
    走到 ``未搜索到相关结果`` 那条返回上。
    """

    def install(*, hits, score: float = 0.9) -> _Wired:
        rec = _Wired()

        def _hits_or_raise() -> list[SearchHit]:
            if isinstance(hits, Exception):
                raise hits
            return list(hits)

        async def search_capability(
            query, *, count=10, country="CN", language="ZH-HANS"
        ):
            rec.searched.append((query, count))
            return _hits_or_raise()

        async def browse_capability(
            query, *, count=10, country="CN", language="ZH-HANS"
        ):
            rec.browsed.append((query, count))
            return _hits_or_raise()

        async def read_capability(url):
            rec.read.append(url)
            return _page_of(url)

        async def rerank_capability(query, docs, *, top_k=5, model=""):
            rec.reranked.append((query, len(docs)))
            # 真的那个 API 认 top_n：只回前 top_k 条 —— 这正是那道砍。
            return [(i, score) for i in range(min(top_k, len(docs)))]

        monkeypatch.setattr(search_mod, "_web_search_capability", search_capability)
        monkeypatch.setattr(search_mod, "_read_webpage_capability", read_capability)
        monkeypatch.setattr(search_mod, "_rerank_capability", rerank_capability)
        # 刷那只手自己引到的那一层（它不该再从 search_web 走）。
        monkeypatch.setattr(web_mod, "web_search", browse_capability)
        return rec

    return install


# --------------------------------------------------------------------------
# 一 · 带着问题去查（真的走完 search_web：抓正文 → 重排 → 渲染）
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_question_comes_back_with_its_sources_attached(
    living_db, in_a_moment, wire_web
):
    """出处必须原样到她眼前 —— 没有出处的答案跟她自己编的分不开。"""
    rec = wire_web(hits=_HITS)

    async with in_a_moment("akao"):
        got = await search_online.invoke({"question": "广州明天下雨吗"})

    assert "https://weather.example/gz" in got, f"出处链接丢了。拿到：\n{got}"
    assert "明天白天有雨" in got, f"抓回来的正文没到她眼前。拿到：\n{got}"
    assert "广州明天下雨吗" in got, (
        f"这批材料是为哪个问题查的，得写在它前面。拿到：\n{got}"
    )
    assert rec.read == [h.url for h in _HITS], (
        f"带着问题查该把命中的页面抓回来读。抓的是：{rec.read}"
    )


@pytest.mark.integration
async def test_her_question_goes_out_word_for_word(living_db, in_a_moment, wire_web):
    """检索词就是她写的那句。替她改写 / 补关键词就是替她决定查什么。"""
    rec = wire_web(hits=_HITS)

    async with in_a_moment("akao"):
        await search_online.invoke({"question": "  广州明天下雨吗  "})

    assert rec.searched == [("广州明天下雨吗", SEARCH_COUNT)], (
        f"她的问题或条数被动过手脚：{rec.searched}"
    )


@pytest.mark.integration
async def test_results_that_do_not_line_up_are_not_called_nothing_out_there(
    living_db, in_a_moment, wire_web
):
    """``未搜索到相关结果`` **不是**「网上没有这回事」。

    它发生在**原始命中非空、重排之后一条没剩**的时候（``search_web`` 里那道
    ``score < MIN_RELEVANCE_SCORE``）—— 网上明明搜回来了东西，只是没有一条跟她这个
    问题对得上，甚至可能只是重排模型这一趟抽风。把它翻译成「网上没查到」，就是拿一次
    捞空替网络下了一个肯定的否定结论，她会照着这个假结论继续想下去。
    """
    rec = wire_web(hits=_HITS, score=0.02)  # 全在相关性阈值以下

    async with in_a_moment("akao"):
        got = await search_online.invoke({"question": "广州明天下雨吗"})

    assert rec.reranked, "前提没成立：这一趟该真的走到重排"
    assert isinstance(got, str), f"这一趟是跑完了的，不该报成没跑成。拿到：{got!r}"
    assert "不对路" in got, f"没说清是「捞回来的东西不对路」。拿到：\n{got}"
    assert "不等于网上没有" in got, (
        f"没有把「这不是网上没有这回事」说给她。拿到：\n{got}"
    )
    assert "网上没查到" not in got, f"把一次捞空说成了网上没有这回事。拿到：\n{got}"


@pytest.mark.integration
async def test_a_search_route_that_may_not_even_be_wired_is_not_a_verdict(
    living_db, in_a_moment, wire_web
):
    """一条命中都没有时 ``search_web`` 回的是「未配置**或**没搜到」—— 分不清是哪一种。"""
    rec = wire_web(hits=[])

    async with in_a_moment("akao"):
        outcome = await search_online.invoke({"question": "广州明天下雨吗"})

    assert rec.searched, "前提没成立：这一趟该真的打到搜索那一层"
    assert isinstance(outcome, dict), (
        f"说不清的一趟被当成「网上确实没有」交给她了。拿到：\n{outcome}"
    )


@pytest.mark.integration
async def test_a_search_that_broke_is_never_reported_as_nothing_out_there(
    living_db, in_a_moment, wire_web
):
    """搜索那一跳自己炸了 —— ``search_web`` 把它收成 ``网页搜索失败: {exc}``。

    那条串带着变长的异常文本，只能认前缀；认漏它就会把一份报错摆成「查到这些」。
    """
    wire_web(hits=RuntimeError("ConnectError: 502 from upstream"))

    async with in_a_moment("akao"):
        outcome = await search_online.invoke({"question": "广州明天下雨吗"})

    assert isinstance(outcome, dict), (
        f"搜索挂了却当成正常结果交给她了。拿到：\n{outcome}"
    )
    assert "502" in outcome["message"], f"实情没带上。拿到：{outcome}"
    assert "网上没查到" not in outcome["message"], (
        f"把「这趟没跑成」说成了「网上没有」。拿到：{outcome}"
    )


@pytest.mark.integration
async def test_a_structured_failure_is_not_dressed_up_as_material(
    living_db, in_a_moment, wire_web, monkeypatch
):
    """``search_web`` 自己叠着 ``@tool_error``：它内部炸掉时交回来的是 dict，不是字符串。

    重排那一步在它的 try 之外，所以从那儿炸就是它自己那道 ``@tool_error`` 兜住 ——
    这是这种形态在真实路径上唯一的来路。
    """
    wire_web(hits=_HITS)

    async def boom(*_a, **_kw):
        raise RuntimeError("重排这一步整个炸了")

    monkeypatch.setattr(search_mod, "_rerank_chunks", boom)

    async with in_a_moment("akao"):
        outcome = await search_online.invoke({"question": "广州明天下雨吗"})

    assert isinstance(outcome, dict) and outcome["kind"] == "tool_error", (
        f"一份结构化报错被当成她查到的东西了。拿到：\n{outcome}"
    )
    assert "重排这一步整个炸了" in outcome["message"], f"实情没带上。拿到：{outcome}"


def test_a_blank_answer_is_not_passed_off_as_material():
    """一片空白既不是材料，也不是任何关于网上有没有的结论。

    这一形态今天从下面那一层构造不出来（``search_web`` 每条返回都带内容），所以直接
    验判据本身：留着这一手，是因为漏掉它的表现是她收到一句「查到这些：」后面空无一物。
    """
    with pytest.raises(RuntimeError):
        web_mod._material("   \n  ")


@pytest.mark.integration
async def test_an_empty_question_is_refused_instead_of_searching_for_nothing(
    living_db, in_a_moment, wire_web
):
    rec = wire_web(hits=_HITS)

    async with in_a_moment("akao"):
        outcome = await search_online.invoke({"question": "   "})

    assert isinstance(outcome, dict), f"空问题被放过去了。拿到：{outcome!r}"
    assert rec.searched == [], "空问题还是打出去了一次搜索"


# --------------------------------------------------------------------------
# 二 · 没事刷一批
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_browsing_brings_back_the_whole_batch_not_a_reranked_five(
    living_db, in_a_moment, wire_web
):
    """刷回来的是**整整一批**，不是按相关性砍剩的 5 条。

    ``search_web`` 那条路走不通这件事：它最后一步
    ``_rerank_chunks(query, list(enriched))`` **没传 top_k**，永远默认 5，还带一道
    ``score < MIN_RELEVANCE_SCORE``。所以要 10 条实际最多回 5 条、而且是按「跟检索词
    有多相关」排过筛过的 —— 刷这件事根本没有那个「问题」可对齐，那道筛就是替她决定
    看什么。
    """
    wire_web(hits=_FEED)

    async with in_a_moment("akao"):
        got = await browse_online.invoke({"direction": "想看点搞笑的"})

    assert len(_FEED) == BROWSE_COUNT, "前提没成立：这一批就该是她要的条数"
    for title in _FEED_TITLES:
        assert title in got, (
            f"这一批少了「{title}」—— 她连它存在都不知道。拿到：\n{got}"
        )


@pytest.mark.integration
async def test_nothing_is_filtered_out_of_what_she_scrolled_past(
    living_db, in_a_moment, wire_web
):
    """刷到什么算什么。

    哪些东西不归她刷（时政、灾害预警这些世界自己会让她感知到的），只靠工具描述说给
    她听。在代码里筛一遍就是替她决定看什么 —— 而且被筛掉的那条她连存在都不知道，
    也就永远不会对它有反应。这几条**故意排在这一批的后半段**：走重排那条路，它们
    正好是被砍掉的那些。
    """
    wire_web(hits=_FEED)

    async with in_a_moment("akao"):
        got = await browse_online.invoke({"direction": "随便看看"})

    for title in ("台风蓝色预警生效", "两国元首会晤", "邻市化工厂起火"):
        assert title in got, f"有条目被替她筛掉了：「{title}」。拿到：\n{got}"


@pytest.mark.integration
async def test_browsing_never_pays_for_page_reading_or_reranking(
    living_db, in_a_moment, wire_web
):
    """刷不抓正文、不重排。

    这不是省钱的顺手优化，是语义：重排是「按跟**那个问题**的相关性排序、并筛掉不相关
    的」，而刷这一刻她手里根本没有问题。拿一个不存在的问题去筛她的一屏，筛掉什么全凭
    重排模型，她自己一票都没有。省下 10 次抓正文 + 一次重排只是顺带的。
    """
    rec = wire_web(hits=_FEED)

    async with in_a_moment("akao"):
        await browse_online.invoke({"direction": "想看点搞笑的"})

    assert rec.browsed, "前提没成立：刷该真的打到搜索那一层"
    assert rec.searched == [], f"刷绕回 search_web 那条路上去了：{rec.searched}"
    assert rec.read == [], f"刷不该去抓正文：{rec.read}"
    assert rec.reranked == [], f"刷不该走重排：{rec.reranked}"


@pytest.mark.integration
async def test_her_direction_goes_out_word_for_word(living_db, in_a_moment, wire_web):
    """她给的方向可以很泛（「想看点搞笑的」），原样就是检索词，不替她拧成关键词。"""
    rec = wire_web(hits=_FEED)

    async with in_a_moment("akao"):
        await browse_online.invoke({"direction": " 想看点搞笑的 "})

    assert rec.browsed == [("想看点搞笑的", BROWSE_COUNT)], (
        f"她的方向或条数被动过手脚：{rec.browsed}"
    )


@pytest.mark.integration
async def test_an_empty_feed_is_not_called_nothing_new_out_there(
    living_db, in_a_moment, wire_web
):
    """一条都没回来时分不清是「这条路没配」还是「真没有」—— 两种都不许说成结论。

    搜索那一层的契约把「没配置」也表达成空列表（见
    ``app/capabilities/web_search.py`` 的 ``web_search``），所以空这一手在这儿跟带着
    问题查那边是同一个处境：如实说这一趟没拿到东西、说不清为什么。
    """
    rec = wire_web(hits=[])

    async with in_a_moment("akao"):
        outcome = await browse_online.invoke({"direction": "想看点搞笑的"})

    assert rec.browsed, "前提没成立：这一趟该真的打到搜索那一层"
    assert isinstance(outcome, dict), (
        f"说不清的一趟被当成「这会儿真没什么可看」交给她了。拿到：\n{outcome}"
    )


@pytest.mark.integration
async def test_browsing_that_broke_is_never_reported_as_nothing_new(
    living_db, in_a_moment, wire_web
):
    wire_web(hits=RuntimeError("ConnectError: 502 from upstream"))

    async with in_a_moment("akao"):
        outcome = await browse_online.invoke({"direction": "想看点搞笑的"})

    assert isinstance(outcome, dict), f"刷挂了却当成刷到了。拿到：\n{outcome}"
    assert "502" in outcome["message"], f"实情没带上。拿到：{outcome}"


@pytest.mark.integration
async def test_an_empty_direction_is_refused_instead_of_browsing_for_nothing(
    living_db, in_a_moment, wire_web
):
    rec = wire_web(hits=_FEED)

    async with in_a_moment("akao"):
        outcome = await browse_online.invoke({"direction": ""})

    assert isinstance(outcome, dict), f"空方向被放过去了。拿到：{outcome!r}"
    assert rec.browsed == [], "空方向还是打出去了一次搜索"


# --------------------------------------------------------------------------
# 三 · 这两只手在她眼里是什么
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_neither_hand_works_outside_one_of_her_moments(wire_web):
    """这两只手只在她的一缝里用得了 —— 没绑上那一缝就当场报错，不是安静地照查。

    她上网看了什么，一个字都不落库（结果只活在本缝的上下文里），事后能查到的只有
    那行带着 lane / persona / 哪一缝的日志。工具体不去读这一缝的身份，那行日志就空
    了一半，而且没有任何东西会红。
    """
    rec = wire_web(hits=_HITS)

    for hand, args in (
        (search_online, {"question": "广州明天下雨吗"}),
        (browse_online, {"direction": "想看点搞笑的"}),
    ):
        outcome = await hand.invoke(args)
        assert isinstance(outcome, dict), (
            f"{hand.name} 没在一缝里也照跑了。拿到：{outcome!r}"
        )

    assert rec.searched == [] and rec.browsed == [], (
        "没在一缝里却已经把她的话打了出去"
    )


def test_neither_hand_ever_asks_her_how_long_something_takes():
    """真人对「多久」没有内感受。问她要一个时长就是把生活切成日程表。

    ``tests/living/test_moment.py`` 对 ``MOMENT_TOOLS`` 有同一条；这两只手自己也守
    一遍，改坏了在本文件就该红。
    """
    banned = ("minute", "duration", "how_long", "seconds", "until", "hour")
    for t in WEB_TOOLS:
        for pname in t.definition.parameters.get("properties", {}):
            assert not any(b in pname.lower() for b in banned), (
                f"{t.name} 的参数 {pname} 在问她一个时长 —— 她换的是一件事"
            )


def test_neither_hand_can_be_mistaken_for_looking_at_her_phone():
    """「刷手机」和「看手机」是两件事，名字和说明书上都必须分得开。

    她手里已经有一只 ``look_at_phone``（看她手机上某条会话说了什么）。刷网上的内容
    口语上也叫「刷手机」，两者一旦在她眼里糊成一件，她想看谁发来的消息时会去刷网页，
    或者反过来。名字里不带 phone，说明书里各自指回对方。
    """
    names = {t.name for t in WEB_TOOLS}

    assert all("phone" not in n for n in names), f"名字会跟看手机撞上：{names}"
    for t in WEB_TOOLS:
        assert "look_at_phone" in t.definition.description, (
            f"{t.name} 的说明书里没把「手机上谁给你发了什么」指回 look_at_phone"
        )


def test_both_hands_are_handed_over_as_one_set():
    """两只手一起交出去 —— 汇进 ``MOMENT_TOOLS`` 的是这一份，不是散着的两个。"""
    assert WEB_TOOLS == [search_online, browse_online]


def test_nothing_she_looks_up_survives_past_this_moment():
    """这两只手不落任何库。

    结果只进本缝的上下文、影响她这一缝的输出，下一缝就没了 —— 跟真人刷完就忘是一
    回事。这里长出一张表就意味着有个东西替她把「看过的东西」留了下来，而留什么该由
    她自己记（``keep_in_mind``）。
    """
    from app.runtime.data import Data

    tables = [
        name
        for name, obj in vars(web_mod).items()
        if isinstance(obj, type) and issubclass(obj, Data) and obj is not Data
    ]
    assert tables == [], f"上网这两只手长出了库：{tables}"


def test_both_hands_are_ones_she_actually_has():
    """这两只手要真在她那一缝的工具集里。

    没挂上去是**静默失败**：模块写好了、这个文件里的用例全绿，但她那一缝的工具列表
    里没有它们，于是永远不会调。同款用例见 ``test_phone.py`` 的
    ``test_finding_someone_is_one_of_the_hands_she_actually_has``。
    """
    from app.living.moment import MOMENT_TOOLS

    assert search_online in MOMENT_TOOLS, "她手里没有上网查这只手"
    assert browse_online in MOMENT_TOOLS, "她手里没有刷一批这只手"
