"""persona 插件：「她是谁」—— 那条会变的版本链，和喂进各条 prompt 的人设变量。

交出 ``persona`` 服务（:class:`app.living.services.Persona`，实现是
:class:`app.living.persona.chain.PersonaChain`）：写日记、每周回看、读书都经它取「她是谁」，
没有第二个组装点。

登记她一轮 prompt 的「她是谁」那一段（``living_life_moment/identity``）：两个变量
``persona_name``、``persona_core`` 在一轮开始取齐 prompt 时由同一个对象算出
（:meth:`~app.living.persona.chain.PersonaChain.identity_vars`），清单 ``resident`` 的主 agent
把它排在第一段。
"""
from __future__ import annotations

from app.agent.contracts import Segment
from app.host import Context, Plugin
from app.living.persona.chain import PersonaChain

# 她一轮 prompt 的第一段：段名和 Langfuse id 相同。
IDENTITY = "living_life_moment/identity"


def identity_segment(chain: PersonaChain) -> Segment:
    """她一轮 prompt 的「她是谁」那一段：两个变量由 ``chain`` 在一轮开始取齐 prompt 时算。"""
    return Segment(
        IDENTITY,
        IDENTITY,
        variables=("persona_name", "persona_core"),
        values=chain.identity_vars,
    )


def setup(ctx: Context) -> None:
    chain = PersonaChain()
    ctx.provide("persona", chain)
    ctx.segment(identity_segment(chain))


PLUGIN = Plugin(name="persona", setup=setup, provides=("persona",))
