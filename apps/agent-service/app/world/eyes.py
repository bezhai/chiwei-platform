"""world 的眼睛 — 感官环节：每天带两层感知去看，带世界关切的当日叙述（眼睛 Task 3）.

旧 app/fetch/agent.py 是独立的采编台（每天按固定清单抓一份 briefing），它不知道
世界走到哪、world 也没法告诉它想看什么——反思对表发现「在等一件事的消息但不知道
哪天来」这种关注无处安放。所以眼睛重写为 world 的一个环节（同一份世界底色、同族
prompt、产出朝向世界），每天带着两层感知去看：

  * **本能扫视**：开发者定的环境量必看清单（天气、日出日落、农历节气、节假日）
    ——人人被罩着的环境量，清单本身是机制层唯一写死的一层（番剧不在其中：谁追
    什么番是世界里长出来的事实，归有意张望）。
  * **有意张望**：读最新世界长弧（知道世界走到哪、看的方向跟着长弧走）+ 读最新
    世界关注（反思留给眼睛的「想看哪」）。长弧 / 关注读不到时如实说空、绝不冒充
    （证据风格同 :mod:`app.world.reflection`：带时间标注、缺失如实说）。
  * **无会话**：``Agent.run`` 不传 session_id（同反思）——cron 白天每小时打点做
    同日重试，每个钟点的尝试从证据现看，不续接上一次失败尝试的 transcript。
    langfuse 归组仍按 (lane, world_eyes, 今天) 的 session id（只做 trace 标签）。
  * **产出朝向世界**：briefing 不是罗列，是带世界关切的当日叙述——关注里问到的
    要回应（看到 / 没看到都如实），看回来的东西先讲世界此刻关心的。
  * **眼睛不落库也不吞错**：落 DailyMaterials 是 node（钟与落脚处，
    :mod:`app.fetch.node`）的事；中途失败照实抛给 node、本钟点不落库，下一钟点
    cron 自动重看。

看什么、怎么叙述由眼睛推演自主判断（prompt 层约束），这里没有覆盖率校验器 /
内容检查器（赤尾宪法：不用确定性规则替 agent 决策）。世界底色由 langfuse 的
``world_eyes`` system prompt 自带（与 ``world_reflect`` 同族、同批维护）；本模块
的 stimulus 只承载两层感知的证据与工具边界——代码侧是工具语义的权威来源。
"""

from __future__ import annotations

import logging

from app.agent.context import AgentContext
from app.agent.core import Agent, AgentConfig
from app.agent.neutral import Message, Role
from app.agent.tools.external_sources import (
    query_anime_calendar,
    query_holiday,
    query_lunar_term,
    query_sun_times,
    query_weather,
)
from app.agent.tools.search import search_web
from app.agent.trace import make_session_id
from app.world.arc import (  # module-level so tests can monkeypatch
    WorldArc,
    read_world_arc,
)
from app.world.attention import (  # module-level so tests can monkeypatch
    WorldAttention,
    read_world_attention,
)

logger = logging.getLogger(__name__)

# 眼睛的独立 AgentConfig：prompt id 钉为 "world_eyes"（langfuse 上新建、世界底色与
# world_reflect 同族，prompt 文本由主会话发布，这里只引用 id）。recursion_limit 给
# 够：一轮里连续查四样环境量 + 可能的番剧 / search_web 张望——设够而非无限，是
# "别失控空转"的安全阀，不进眼睛看什么的决策。
_EYES_RECURSION_LIMIT = 10
_EYES_CFG = AgentConfig(
    "world_eyes",
    "offline-model",
    "world-eyes",
    recursion_limit=_EYES_RECURSION_LIMIT,
)

# 本能扫视清单：开发者定的环境量类别（人人被罩着的），机制层唯一写死的一层。
# 番剧不在这里——谁追什么番是世界里长出来的事实，只在长弧 / 关注交代到时才看。
INSTINCT_SCAN_ITEMS: tuple[str, ...] = ("天气", "日出日落", "农历节气", "节假日")

# 眼睛的工具箱：五个结构化查询 skill + search_web 兜底。番剧工具**保留在手**（工具
# 在手 ≠ 每天必用），它的使用边界在 stimulus 里说清：只在长弧 / 关注交代到时上手。
WORLD_EYES_TOOLS = [
    query_weather,
    query_sun_times,
    query_lunar_term,
    query_holiday,
    query_anime_calendar,
    search_web,
]


def _arc_evidence(arc: WorldArc | None) -> str:
    """长弧段：有长弧给全文 + turned_at 时间标注；空弧如实说空、绝不冒充。"""
    if arc is None:
        return "世界还没写下走到哪——长弧还是空白。方向上不必强求，看好环境量就够。"
    return f"（这版长弧写于 {arc.turned_at}）\n{arc.narrative}"


def _attention_evidence(attention: WorldAttention | None) -> str:
    """关注段：有关注给全文 + written_at 时间标注；没有如实说没人交代过。"""
    if attention is None:
        return "还没有人交代过眼睛要看什么。今天只做本能扫视就好。"
    return f"（这版关注写于 {attention.written_at}）\n{attention.narrative}"


def build_eyes_stimulus(
    *,
    date: str,
    arc: WorldArc | None,
    attention: WorldAttention | None,
) -> str:
    """拼眼睛的两层感知 stimulus：今天日期 + 本能扫视清单 + 有意张望（长弧 / 关注）。

    证据风格同反思：长弧 / 关注全文带时间标注（眼睛要知道手里这份是多久前写的）、
    缺失如实说空。工具边界也在这里说清：番剧工具只在长弧 / 关注交代到时才用；
    ok=false 如实说没拿到、绝不编一个顶上（兜底语义沿用旧采编、不变）。
    """
    instinct = "、".join(INSTINCT_SCAN_ITEMS)
    return (
        f"今天是 {date}。你是这个世界的眼睛：带着下面两层感知，用手上的查询工具去看"
        f"今天，再把看回来的东西组织成一段给世界看的「当日叙述」中文话。\n\n"
        f"【本能扫视——每天必看的环境量】{instinct}。每样都用对应的工具去查。\n\n"
        f"【有意张望——世界的长弧】（看的方向跟着它走）\n"
        f"{_arc_evidence(arc)}\n\n"
        f"【有意张望——世界交代要看的】\n"
        f"{_attention_evidence(attention)}\n"
        f"关注里交代的事要专门去看；看到了什么、还是没看到，都必须在叙述里如实回应，"
        f"不许沉默带过。\n\n"
        f"工具的边界：查询工具返回带 ok 标志的结构化数据——ok=true 按里面的真实字段"
        f"写；ok=false 就如实说那一项今天没拿到，绝不编一个顶上。番剧日历工具不在每天"
        f"必看的清单里，只在长弧或关注交代到追番、新番这类事时才用。专门工具没覆盖、"
        f"而长弧或关注让你看的事，用 search_web 兜底去查。\n\n"
        f"产出：一段中文的「当日叙述」——客观、不编造，但不是逐项罗列；看回来的东西"
        f"要朝向世界：这个世界此刻关心什么，就先讲什么。"
    )


async def run_world_eyes(*, lane: str, date: str) -> str:
    """跑一次眼睛：读长弧 / 关注 → 拼两层感知 stimulus → Agent 工具循环 → 返回叙述。

    眼睛自己不落库（落 DailyMaterials 是 node 的事——钟与落脚处职责不变），也不
    吞错：中途失败照实抛给 node、本钟点不落库，下一钟点 cron 自动重看（同日重试
    的钟在 wiring 层）。

    无会话（同反思）：``Agent.run`` 不传 session_id——每个钟点的尝试从证据现看，
    不续接上一次失败尝试的 transcript；session_id 只塞 ``AgentContext`` 做 langfuse
    归组标签（这一天眼睛的所有尝试归一条 session）。

    ``max_retries=1``：眼睛的工具都是只读查询，整轮重放不破坏数据，但重放烧 token
    且与「失败交给下一钟点」的重试语义重复——重试只留钟那一层。
    """
    arc = await read_world_arc(lane=lane)
    attention = await read_world_attention(lane=lane)
    stimulus = build_eyes_stimulus(date=date, arc=arc, attention=attention)
    context = AgentContext(session_id=make_session_id(lane, "world_eyes", date))
    result = await Agent(_EYES_CFG, tools=WORLD_EYES_TOOLS).run(
        [Message(role=Role.USER, content=stimulus)],
        context=context,
        max_retries=1,
    )
    return result.text()
