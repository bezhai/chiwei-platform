"""agent-service 的清单：运维接口、通信机制的人工入口、skills（手边那几份说明从它来），和她的插件
（``app/living/<插件>/``，一个功能一个插件，插件之间只经服务交互，见 :mod:`app.living`）；住在这儿的是
``participants`` 列的三个人，她们的一轮按 ``resident`` 组成。宿主先起被依赖的插件（一个插件要的服务由
哪个插件交出，各插件的 ``requires`` / ``provides`` 说了算），互不依赖的按这里列的顺序。

**给她的一轮加一只手或一段输入，只改那个插件和这里。** 插件在 setup 里登记它（``ctx.tool``、
``ctx.section``），这里把插件加进 ``plugins``、把名字写进 ``resident`` 对应的位置；别的插件、宿主、固定
循环都不用动（``tests/living/test_a_new_feature.py``）。登记了而 ``resident`` 没列的、列了而没有插件
登记的，宿主起来时就报错。

她的工具按这里列的顺序给模型（也是请求前缀缓存的一部分，改顺序就是改请求）：``keep_in_mind``
夹在 life 的几只手中间，所以顺序集中写在这里，不按插件拼。

她的一轮：一轮开始前把她之前做了还没发出去的发出去；状态段是她此刻的样子（清理那一下重铺），输入段是
这一轮新发生的，两组各按这里的顺序用空行拼；落地的三步在一个事务里；存上下文和往外发失败只记日志，
这一轮照样算数（各段各步见 :mod:`app.living.life.moment`）。

agent 的定义（:class:`~app.agent.contracts.AgentSpec`）在它们原来的模块里：安全检查是她发消息时
在那一轮里调的，调它的代码要用同一份定义。上下文的键、裁剪的数和标记消息的文字也都引用各自唯一的
定义处（标记消息这些文字和 world 共用，见 :mod:`app.manifests.history`）。
"""

from __future__ import annotations

from app.agent.contracts import CostSpec, HistorySpec, Profile
from app.host import App, Participant
from app.living.life.moment import HISTORY_KEY, LIFE_MOMENT_AGENT, MOMENT_TRIM_POLICY
from app.living.speaking.output_safety import GUARD_OUTPUT_AGENT
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
        "app.living.household",
        "app.living.speaking",
        "app.living.reading",
        "app.living.day_page",
        "app.living.persona_review",
        "app.living.web",
        "app.living.guides",
        "app.living.life",
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
