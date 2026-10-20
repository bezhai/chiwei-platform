"""world 的清单：world 的核心插件和三个知识来源插件（records、reality、told），所以 world 的进程
不加载 living 的任何代码。插件按这里的顺序起来，来源也按这个顺序登记：感知判断、NPC、应答的查询
工具和收件处理按这个顺序看到它们。

主 agent 的一轮按 ``world`` 组成（各段各步见 :mod:`app.world.main_agent`）：一轮开始前补发没跑完的
一轮的告知；状态段是记录目录，只进上下文清理的标记消息；三个输入段用一个换行拼；检查它定了下次
醒来；没有落地事务；之后存上下文、清空没跑完的一轮、定下次醒来，三步失败都让这一轮失败。

它的工具按这里列的顺序给模型（也是请求前缀缓存的一部分，改顺序就是改请求）：三个来源的查询工具
按来源排，再是只有它才有的四个动作。三个来源能被 Dynamic Config ``world_sources`` 关掉（开关在
一轮开始时读一次，关掉的来源的工具不进这一轮）。

agent 的定义（:class:`~app.agent.contracts.AgentSpec`）在它们原来的模块里：感知判断、NPC 是在主
agent 的工具里调的，调它们的代码要用同一份定义。上下文的键、裁剪的数和标记消息的文字也都引用各自
唯一的定义处（标记消息这些文字和 agent-service 共用，见 :mod:`app.manifests.history`）。
"""

from __future__ import annotations

from app.agent.contracts import CostSpec, HistorySpec, Profile, Switches
from app.host.manifest import App, Participant
from app.manifests.history import HISTORY_TEXTS
from app.world.agents import HISTORY_KEY
from app.world.main_agent import ROUND_AGENT, TRIM_POLICY
from app.world.npc import NPC_AGENT
from app.world.perception import PERCEPTION_AGENT
from app.world.sources import ENABLED_SOURCES_KEY

WORLD = Profile(
    agent="world_round",
    sub_agents=("world_perception", "world_npc"),
    tools=(
        "list_records",
        "read_record",
        "check_current_weather",
        "check_hourly_forecast",
        "check_daily_forecast",
        "search_web",
        "list_senders",
        "read_messages_from",
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
        "app.world.sources.records",
        "app.world.sources.reality",
        "app.world.sources.told",
    ),
    agents={
        "world_round": ROUND_AGENT,
        "world_perception": PERCEPTION_AGENT,
        "world_npc": NPC_AGENT,
    },
    profiles={"world": WORLD},
    participants=(Participant("world", "world"),),
)
