"""guides 插件：手边那几份写好的说明 —— 读一份（``read_a_guide``），照说明在沙箱里跑一条命令
（``run_a_script``）。她自己长什么样就写在其中一份里。

登记两只手（读回来的、跑出来的都是材料），和她一轮 prompt 里「手边能读的说明」那一段
（``living_life_moment/guides``，变量 :data:`~app.living.guides.at_hand.GUIDES_VAR`，一轮开始时现算）。
说明的正文在盘上，由 skills 插件扫目录、热加载（:mod:`app.plugins.skills`），所以这个插件要在它之后
起来、没有它就拒绝启动。细节见 :mod:`app.living.guides.at_hand`。
"""
from __future__ import annotations

from app.agent.contracts import Segment
from app.host import Context, Plugin
from app.living.guides.at_hand import GUIDE_TOOLS, GUIDES_VAR, guides_at_hand

# 她一轮 prompt 的最后一段：段名和 Langfuse id 相同。
GUIDES = "living_life_moment/guides"
GUIDES_SEGMENT = Segment(GUIDES, GUIDES, variables=(GUIDES_VAR,), values=guides_at_hand)


def setup(ctx: Context) -> None:
    for spec in GUIDE_TOOLS:
        ctx.tool(spec.tool, trim=spec.trim, ends_round=spec.ends_round)
    ctx.segment(GUIDES_SEGMENT)


PLUGIN = Plugin(name="guides", setup=setup, requires=("skills",))
