"""agent-service 的清单：运维接口、通信机制的人工入口、guides、「她是谁」（persona）、living
引擎；三姐妹的一轮按 ``resident`` 组成。

她的工具按这里列的顺序给模型（也是请求前缀缓存的一部分，改顺序就是改请求）：``keep_in_mind``
夹在 life 的几只手中间，所以顺序集中写在这里，不按插件拼。

她的一轮：一轮开始前把她之前做了还没发出去的发出去；状态段是她此刻的样子（清理那一下重铺），输入段是
这一轮新发生的，两组各按这里的顺序用空行拼；落地的三步在一个事务里；存上下文和往外发失败只记日志，
这一轮照样算数（各段各步见 :mod:`app.living.moment`）。

agent 的定义（:class:`~app.agent.contracts.AgentSpec`）在它们原来的模块里：安全检查是她发消息时
在那一轮里调的，调它的代码要用同一份定义。上下文的键、裁剪的数和标记消息的文字也都引用各自唯一的
定义处（标记消息这些文字和 world 共用，见 :mod:`app.manifests.history`）。
"""

from __future__ import annotations

from app.agent.contracts import CostSpec, HistorySpec, Profile
from app.host.manifest import App, Participant
from app.living.moment import HISTORY_KEY, LIFE_MOMENT_AGENT, MOMENT_TRIM_POLICY
from app.living.output_safety import GUARD_OUTPUT_AGENT
from app.manifests.history import HISTORY_TEXTS

RESIDENT = Profile(
    agent="living_life_moment",
    sub_agents=("guard_output_safety",),
    tools=(
        "switch_to",
        "move_to",
        "keep_in_mind",
        "say",
        "act",
        "look_around",
        "stop_for_now",
        "look_at_phone",
        "look_through_your_phone",
        "look_up_contact",
        "send_message",
        "take_back_message",
        "search_online",
        "browse_online",
        "look_for_something_to_read",
        "read_a_bit",
        "draw_a_picture",
        "find_a_picture_online",
        "look_through_your_pictures",
        "look_at_a_picture",
        "read_a_guide",
        "run_a_script",
    ),
    state=(
        "day_page.last_written",
        "loose_ends.open",
        "her_records.recent",
        "phone.unread",
    ),
    input=(
        "life.now",
        "her_records.whereabouts",
        "loose_ends.just_due",
        "household.received",
        "phone.arrived",
    ),
    round_start=("outgoing.before",),
    commit=("life.moment", "phone.glances", "household.read"),
    after_commit=("history", "outgoing.after"),
    history=HistorySpec(
        key=HISTORY_KEY, policy=MOMENT_TRIM_POLICY, texts=HISTORY_TEXTS
    ),
    round_log="life.moments",
    cost=CostSpec(actor="{participant}", round_id="{round_id}"),
)

MANIFEST = App(
    plugins=(
        "app.plugins.ops",
        "app.plugins.operator",
        "app.plugins.skills",
        "app.living.persona",
        "app.living.residents",
        "app.living.her_records",
        "app.living.loose_ends",
        "app.living.pictures",
        "app.living.phone",
        "app.living.web",
        "app.living.guides",
        "app.plugins.living",
    ),
    agents={
        "living_life_moment": LIFE_MOMENT_AGENT,
        "guard_output_safety": GUARD_OUTPUT_AGENT,
    },
    profiles={"resident": RESIDENT},
    participants=(
        Participant("akao", "resident"),
        Participant("ayana", "resident"),
        Participant("chinagi", "resident"),
    ),
)
