"""world：一个独立部署的引擎，自己醒来、看现实、让世界变化，变化写进它自己的记录。

它和三姐妹的 life 互不 import（CI 规则 ``scripts/check_world_life_imports.py``），运行时
也不加载 life 的代码：它是同一个镜像上的另一个 App，进程只加载 :mod:`app.world.wiring`
（``app.deployment.APP_WIRING``）。它和别的参与者之间唯一的连接是通信机制
（:mod:`app.messaging`）。

* :mod:`app.world.volume` —— 私有卷上按泳道分的那个目录；
* :mod:`app.world.records` —— 记录：那个目录下的一棵自然语言文档树；
* :mod:`app.world.wake` —— 下次醒来的时刻（私有状态）和醒来规则；
* :mod:`app.world.sources` —— 知识来源：agent 能查到的东西从哪来，每个来源一个模块；
* :mod:`app.world.agents` —— world 的几类 agent 怎么调模型（模型、trace、成本）；
* :mod:`app.world.main_agent` —— 主 agent 的一轮；
* :mod:`app.world.actions` —— 只有主 agent 才有的动作；
* :mod:`app.world.admin` —— 记录的人工读写接口；
* :mod:`app.world.wiring` —— 这个 App 的接线：来源登记、收件箱和人工接口。
"""
