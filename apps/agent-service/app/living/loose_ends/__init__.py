"""loose_ends 插件：她心里挂着、还没了结的事 —— 她自己维护的那份清单。

  * 工具 ``keep_in_mind``：整份重写那份清单（:meth:`~app.living.loose_ends.mind.OnHerMind.keep_in_mind`；
    记下的是她自己的话，裁剪时和她说的话一样留着）。
  * 状态段 ``loose_ends.open``：心里挂着没了结的那几件；输入段 ``loose_ends.just_due``：这期间到点的
    那几件（:mod:`app.living.loose_ends.mind`）。
  * 交出 ``loose_ends`` 服务（:class:`app.living.services.LooseEnds`）：她这一轮落地时还挂着几件。

线头本身怎么存、怎么开关、时刻怎么写在条目里，见 :mod:`app.living.loose_ends.threads`。
"""
from __future__ import annotations

from app.agent.tooling import Tool
from app.host import Context, Plugin
from app.living.loose_ends.mind import OnHerMind


def setup(ctx: Context) -> None:
    mind = OnHerMind()
    ctx.provide("loose_ends", mind)
    ctx.tool(Tool(mind.keep_in_mind), trim="kept")
    ctx.section("loose_ends.open", mind.open_section)
    ctx.section("loose_ends.just_due", mind.just_due_section)


PLUGIN = Plugin(name="loose_ends", setup=setup, provides=("loose_ends",))
