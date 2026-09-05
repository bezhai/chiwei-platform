"""Agent tool sets.

``BASE_TOOLS`` / ``ALL_TOOLS`` 两份工具清单随旧 chat 一起删除：装配它们的唯一
调用方是旧 chat 的渲染层（``Agent(cfg, tools=ALL_TOOLS)``）。living 引擎
不用成套的清单——它在每个场景各自 ``@tool`` 声明自己那一把（``app.living.moment``
/ ``mouth`` / ``phone`` / ``web`` ...），要哪个给哪个。

包本身留着，而且不只为下面这几个：``_common`` / ``outcome`` / ``search`` 三个模块
是 ``app.living`` 直接在用的（``tool_error``、``ToolOutcomeError``、``search_web``）。

下面这几个具名工具**目前没有任何生产调用方**——它们原先只经上面那两份清单进旧
chat 的主 agent。留着是因为它们是自成一体的能力（沙箱 / 转派 / 载入 skill /
不回复），不是旧引擎的一部分；接进 living 只需要在用它的场景里 import。
要是决定不接了，连同 ``app.capabilities`` 里对应的适配器一起删。

原先那三件图片工具（画一张 / 搜一批 / 看某几张）**已经删掉**，不是搬走了。它们靠的是
旧 chat 那套按出站消息编号图片的 Redis 结构，而构造它的旧 chat pipeline 删掉之后那套
结构恒为空——手工调到也只会回一句"图片生成失败"，而图其实画出来了、也传上对象存储了。
她那侧的图走 ``app.living.pictures``：存永久句柄、按句柄取回、跨缝找得回。真正画图
的那一层仍在 ``app.agent.image_gen``。
"""

from app.agent.tools.delegation import deep_research
from app.agent.tools.no_reply import no_reply
from app.agent.tools.sandbox import sandbox_bash
from app.agent.tools.search import search_web
from app.agent.tools.skill import load_skill

__all__ = [
    "search_web",
    "deep_research",
    "load_skill",
    "sandbox_bash",
    "no_reply",
]
