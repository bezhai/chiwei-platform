"""residents 插件：谁住在这儿 —— 三姐妹，和她们在世界里的名字。

住在这儿的是清单里按 ``resident`` 跑的那几个参与者（``ctx.participants``），代码里不另写一份。
交出 ``residents`` 服务（:class:`app.living.services.WhoLivesHere`，实现是
:class:`app.living.residents.names.PersonaNames`）：名字取自人设表，开收件箱时读一次、检查一次
（开收件箱的是收件那一侧，它要用这份名字开），之后查地址、认出她对谁说话都用同一份。
"""
from __future__ import annotations

from app.host import Context, Plugin
from app.living.residents.names import PersonaNames


def setup(ctx: Context) -> None:
    ctx.provide("residents", PersonaNames(ctx.participants(profile="resident")))


PLUGIN = Plugin(name="residents", setup=setup, provides=("residents",))
