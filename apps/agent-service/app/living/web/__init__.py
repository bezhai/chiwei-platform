"""web 插件：上网 —— 手里有问题就查（``search_online``），没有就随便翻（``browse_online``）。

两只手和它们怎么把搜索的几种空返回如实告诉她，见 :mod:`app.living.web.online`。查回来的东西是她
读到的材料，裁剪时过了 ``material_minutes`` 换成一句占位（再查一次就有）。不交出也不要任何服务。
"""
from __future__ import annotations

from app.host import Context, Plugin
from app.living.web.online import WEB_TOOLS


def setup(ctx: Context) -> None:
    for spec in WEB_TOOLS:
        ctx.tool(spec.tool, trim=spec.trim, ends_round=spec.ends_round)


PLUGIN = Plugin(name="web", setup=setup)
