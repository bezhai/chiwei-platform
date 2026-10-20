"""world 的清单：world 的核心插件，记录的两个插件（``records`` 是查询工具，``record_keeping`` 是写
记录、记录目录和人工读写接口），和另外两个知识来源插件（reality、told），所以 world 的进程不加载
living 的任何代码。插件按这里的顺序起来，来源也按这个顺序登记，收件处理按这个顺序看到它们。

主 agent 的一轮按 ``world`` 组成（各段各步见 :mod:`app.world.main_agent`）：一轮开始前补发没跑完的
一轮的告知；状态段是记录目录，只进上下文清理的标记消息；三个输入段用一个换行拼；检查它定了下次
醒来；没有落地事务；之后存上下文、清空没跑完的一轮、定下次醒来，三步失败都让这一轮失败。

它的工具按这里列的顺序给模型（也是请求前缀缓存的一部分，改顺序就是改请求）：三个来源的查询工具
按来源排（:data:`QUERY_TOOLS`），再是只有它才有的四个动作。三个来源能被 Dynamic Config
``world_sources`` 关掉（开关在一轮开始时读一次，关掉的来源的工具不进这一轮）。

感知判断、NPC、应答拿的也是 :data:`QUERY_TOOLS` 这一份，按同样的顺序（感知判断另有它记判断的
那只手）：加一个查询工具，就是在它的插件里登记、在 :data:`QUERY_TOOLS` 里加上名字。

agent 的定义（:class:`~app.agent.contracts.AgentSpec`）在它们原来的模块里，这里给三类按需调用的
agent 加上工具；调它们的代码从宿主拿到的（``ctx.agent``）就是这里列的这一份。上下文的键、裁剪的数和
标记消息的文字也都引用各自唯一的定义处（标记消息这些文字和 agent-service 共用，见
:mod:`app.manifests.history`）。
"""

from __future__ import annotations

from dataclasses import replace

from app.agent.contracts import CostSpec, HistorySpec, Profile, Switches
from app.host.manifest import App, Participant
from app.manifests.history import HISTORY_TEXTS
from app.world.agents import HISTORY_KEY
from app.world.answer import ANSWER_AGENT
from app.world.main_agent import ROUND_AGENT, TRIM_POLICY
from app.world.npc import NPC_AGENT
from app.world.perception import PERCEPTION_AGENT
from app.world.sources import ENABLED_SOURCES_KEY

# 三个知识来源的查询工具，按来源排：主 agent 和另外三类 agent 拿的都是这一份。
QUERY_TOOLS = (
    "list_records",
    "read_record",
    "check_current_weather",
    "check_hourly_forecast",
    "check_daily_forecast",
    "search_web",
    "list_senders",
    "read_messages_from",
)

WORLD = Profile(
    agent="world_round",
    sub_agents=("world_perception", "world_npc"),
    tools=(
        *QUERY_TOOLS,
        "write_record",
        "wake_me_at",
        "report_change",
        "let_npc_appear",
    ),
    state=("records.listing",),
    input=("wake.woken_by", "wake.planned", "unfinished.happenings"),
    join="\n",
    round_start=("unfinished.resend",),
    checks=("wake.chosen",),
    after_commit=("history", "unfinished.clear", "wake.next"),
    history=HistorySpec(key=HISTORY_KEY, policy=TRIM_POLICY, texts=HISTORY_TEXTS),
    cost=CostSpec(actor="world", round_id="{trace}:{round_id}"),
    switches=Switches(ENABLED_SOURCES_KEY, ("records", "reality", "told")),
)

MANIFEST = App(
    plugins=(
        "app.plugins.world",
        "app.world.records.reading",
        "app.world.records.keeping",
        "app.world.reality",
        "app.world.sources.told",
    ),
    agents={
        "world_round": ROUND_AGENT,
        "world_perception": replace(
            PERCEPTION_AGENT, tools=(*QUERY_TOOLS, "someone_notices")
        ),
        "world_npc": replace(NPC_AGENT, tools=QUERY_TOOLS),
        "world_answer": replace(ANSWER_AGENT, tools=QUERY_TOOLS),
    },
    profiles={"world": WORLD},
    participants=(Participant("world", "world"),),
)
