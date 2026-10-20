"""agent-service 的清单：运维接口、通信机制的人工入口、guides、living 引擎；三姐妹的一轮按
``resident`` 组成。

她的工具按这里列的顺序给模型（也是请求前缀缓存的一部分，改顺序就是改请求）：``keep_in_mind``
夹在 life 的几只手中间，所以顺序集中写在这里，不按插件拼。

agent 的定义（:class:`~app.agent.contracts.AgentSpec`）在它们原来的模块里：安全检查是她发消息时
在那一轮里调的，调它的代码要用同一份定义。
"""

from __future__ import annotations

from app.agent.contracts import Profile
from app.capabilities.output_safety import GUARD_OUTPUT_AGENT
from app.host.manifest import App, Participant
from app.living.moment import LIFE_MOMENT_AGENT

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
)

MANIFEST = App(
    plugins=(
        "app.plugins.ops",
        "app.plugins.operator",
        "app.plugins.skills",
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
