"""reading 插件：她读别人发到她手机上的文件 —— 没有"书"这个注册物，她能读的严格等于她看得见的。

  * 两只手（:class:`app.living.reading.files.Reader` 的方法）：``look_for_something_to_read`` 看看有人
    发过什么能读的、她读到哪了，``read_a_bit`` 拿起其中一个往下读一程。
  * 读书收件箱（:data:`app.living.services.READING`）：她拿起一个文件时发一条进来，处理函数
    :func:`app.living.reading.files.read_a_round` 读那一程（读一程的 agent 在
    :mod:`app.living.reading.agent`）。
  * ``POST /admin/reading/carry-over``：把旧读书队列里剩下的搬进读书收件箱
    （:mod:`app.living.reading.carry_over`），prod 切换完成、旧队列删掉之后连同路由一起删除。
  * 要 ``phone`` 服务（她能读的会话）、``residents`` 服务（拿起文件时那条消息署她的名字，搬运时认出是谁
    的那一程）和 ``persona`` 服务（读一程的 agent 的人设变量）。

**读书收件箱里的一条最多处理一次**（``max_attempts=1``）：读一程要调模型，越过读一程的错误让这条
进死信，要不要重放由人决定。处理时限五分钟，比读书 agent 自己的超时长；它限定的是读一程的进程死了
之后，那一条被占着多久才有别的进程接着读。从外面投进这个收件箱的东西能让读书 agent 读一程，碰不到
她的一轮（``tests/living/test_no_inbound.py``）。

**搬运那条路由**是唯一一条到得了 ``app.living`` 的路由。它要内网凭据、核对泳道，跟通信机制的人工入口
一样；它能做到的跟往读书收件箱里投一条一样，碰不到她的一轮（同一个测试文件守着）。
"""
from __future__ import annotations

from datetime import timedelta
from functools import partial

from app.agent.tooling import Tool
from app.host import Context, Plugin
from app.living.reading.carry_over import ReadingCarryOverRequest, carry_over_reading
from app.living.reading.files import Reader, read_a_round
from app.living.services import READING


def setup(ctx: Context) -> None:
    residents = ctx.service("residents")
    reader = Reader(phone=ctx.service("phone"), residents=residents)
    # 读东西的两只手留着，不当素材裁：``look_for_something_to_read`` 的返回里是 ``file=<attachment_id>``，
    # 被 ``read_a_bit(which=...)`` 吃；``read_a_bit`` 指代不明时抛的那句话里也逐个印着候选的 ``file=``。
    ctx.tool(Tool(reader.look_for_something_to_read), trim="kept")
    ctx.tool(Tool(reader.read_a_bit), trim="kept")
    ctx.inbox(
        READING,
        on_message=partial(read_a_round, persona=ctx.service("persona")),
        processing_timeout=timedelta(minutes=5),
        max_attempts=1,
    )
    ctx.route(
        "POST",
        "/admin/reading/carry-over",
        ReadingCarryOverRequest,
        partial(carry_over_reading, residents=residents),
        inner_secret=True,
        lane_match=True,
        answers_with_lane=True,
    )


PLUGIN = Plugin(
    name="reading",
    setup=setup,
    requires=("phone", "residents", "persona"),
)
