"""抓取 agent 的配置 + 工具集 + 抓取意图 stimulus（刀 3 Task2，纯 agent 主导版）。

抓取回到**纯 agent 主导**：通用抓取 agent 不"填一张表"、也不被动接收 node 预查好的
文本，而是跑一个工具循环——拿着三个**结构化**查询 skill（query_weather /
query_anime_calendar / query_holiday，各返回带 ``ok`` 的 dict）+ ``search_web`` 兜底，
自己去查今天的天气 / 在更新的番 / 节假日，自己看每个工具返回的 ``ok``，把**真实数据**
组织成一段给世界引擎看的「今天的客观底料」中文话。

  * **模型** ``offline-model``：异步后台思考用离线模型（对齐 world / life，不用主对话
    的 gemini）。
  * **工具集** = 三个结构化查询 skill（:func:`app.agent.tools.external_sources.query_weather`
    / ``query_anime_calendar`` / ``query_holiday``）+ ``search_web`` 兜底。
  * **system prompt** 走 langfuse 按 ``prompt_id="fetch_agent"`` 取（:func:`app.agent.prompts.get_prompt`）。
    prompt 文本由主会话发 langfuse；这里只引用 prompt_id。
  * **recursion_limit** 给够：让它在一轮里连续调多个查询工具 + 可能的 search_web 兜底，
    不被默认 6 卡住。

node 只给一条抓取意图 stimulus（:func:`build_fetch_stimulus`，只含「今天是哪天」），
其余由 agent 自己调工具拿数据、自己组织——这是「用 agent 智能、不用死代码」的体现。
"""

from __future__ import annotations

from app.agent.core import AgentConfig
from app.agent.tools.external_sources import (
    query_anime_calendar,
    query_holiday,
    query_lunar_term,
    query_sun_times,
    query_weather,
)
from app.agent.tools.search import search_web

# 抓取 agent 的 langfuse prompt id（system prompt 文本由主会话发布到 langfuse）。
FETCH_PROMPT_ID = "fetch_agent"

# recursion_limit 给够：一轮里连续调三个查询工具 + 可能的 search_web 兜底（10 足矣）。
# 设够而非无限，是"别失控空转"的安全阀，不进抓取内容决策。
FETCH_RECURSION_LIMIT = 10

# 抓取 agent 工具集：五个结构化查询 skill（天气 / 番剧 / 节假日 / 日出日落 /
# 节气农历）+ search_web 兜底。
FETCH_TOOLS = [
    query_weather,
    query_anime_calendar,
    query_holiday,
    query_sun_times,
    query_lunar_term,
    search_web,
]

# offline-model：异步后台思考用离线模型（对齐 world / life）。
FETCH_CFG = AgentConfig(
    FETCH_PROMPT_ID,
    "offline-model",
    "fetch-agent",
    recursion_limit=FETCH_RECURSION_LIMIT,
)


def build_fetch_stimulus(*, date: str) -> str:
    """构造喂给抓取 agent 的抓取意图 stimulus（只含「今天是哪天」）。

    纯 agent 主导：node 不预查，agent 自己拿着三个结构化查询 skill 去查、看每个返回的
    ``ok``、把真实数据组织成一段「今天的客观底料」中文话。这条 stimulus 引导它覆盖天气
    / 番剧 / 节假日，对某个工具返回 ``ok=false`` 的源**如实说那项今天没拿到、绝不编一
    个顶上**，有专门工具没覆盖、今天值得世界知道的客观信息才用 ``search_web`` 兜底。
    """
    return (
        f"今天是 {date}。请你用手上的查询工具，把今天的几样外部底料查清楚、组织成一段"
        f"给世界引擎看的「今天的客观底料」中文话——客观、简洁、只陈述事实，不要加主观"
        f"评论或情绪。\n\n"
        f"要覆盖到：今天的天气、今天在更新的番、今天是否节假日。每样都用对应的工具去查，"
        f"工具返回的是结构化数据（带 ok 标志）：ok=true 时按里面的真实字段写；某个工具"
        f"返回 ok=false（没查到）时，就如实说那一项今天没拿到，绝不编一个顶上。如果你"
        f"觉得还有今天值得世界知道、而这几样工具没覆盖的客观信息，可以用 search_web 兜底"
        f"查一下再一并写进这段话。\n\n"
        f"请输出组织好的「今天的客观底料」。"
    )
