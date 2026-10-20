"""Agent tool sets.

``BASE_TOOLS`` / ``ALL_TOOLS`` 两份工具清单随旧 chat 一起删除：装配它们的唯一
调用方是旧 chat 的渲染层（``Agent(cfg, tools=ALL_TOOLS)``）。living 引擎
不用成套的清单——它在每个场景各自 ``@tool`` 声明自己那一把（``app.living.moment``
/ ``mouth`` / ``phone`` / ``web`` ...），要哪个给哪个。

包本身留着：``_common`` / ``outcome`` / ``search`` 三个模块是 ``app.living`` 和 world 直接
在用的（``tool_error``、``ToolOutcomeError``、``search_web``）。

原先那两个具名工具（转派给调研 agent / 不回复）**已经删掉**：它们只经上面那两份清单进旧 chat
的主 agent，旧 chat 删掉之后一直没有调用方。

原先另外那两个（``load_skill`` 载入 skill / ``sandbox_bash`` 跑沙箱）**已经删掉**，
不是搬走了：她那侧的两只手在 ``app.living.guides`` 里按她的口吻重写过一遍（读一份
说明 / 照说明跑一条命令，跑的时候带上是哪份说明教的——旧那个从来不传这个参数，那几
份说明教她的用法在它上面一步都走不通）。底层的 ``app.skills`` 和
``app.capabilities.sandbox`` 保留：那是机制不是工具，渲染器也在用。

原先那三件图片工具（画一张 / 搜一批 / 看某几张）**已经删掉**，不是搬走了。它们靠的是
旧 chat 那套按出站消息编号图片的 Redis 结构，而构造它的旧 chat pipeline 删掉之后那套
结构恒为空——手工调到也只会回一句"图片生成失败"，而图其实画出来了、也传上对象存储了。
她那侧的图走 ``app.living.pictures``：存永久句柄、按句柄取回、跨轮找得回。真正画图
的那一层仍在 ``app.agent.image_gen``。
"""

from app.agent.tools.search import search_web

__all__ = [
    "search_web",
]
