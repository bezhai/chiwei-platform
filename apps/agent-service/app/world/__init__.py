"""world：一个独立部署的引擎，自己醒来、看现实、让世界变化，变化写进它自己的记录；谁会察觉
到一个变化由它判断并告知当事人，NPC 由临时 agent 扮演，别人问它某处什么样、谁在哪时它回答。

它和三姐妹的 life 互不 import（CI 规则 ``scripts/check_world_life_imports.py``），运行时
也不加载 life 的代码：它是同一个镜像上的另一个 App，进程只起 world 的插件
（:mod:`app.plugins.world`，清单在 ``app.deployment.APPS``）。它和别的参与者之间唯一的连接是通信机制
（:mod:`app.messaging`）。

* :mod:`app.world.volume` —— 私有卷上按泳道分的那个目录；
* :mod:`app.world.records` —— 记录：那个目录下的一棵自然语言文档树；
* :mod:`app.world.wake` —— 下次醒来的时刻（私有状态）和醒来规则；
* :mod:`app.world.sources` —— 知识来源：agent 能查到的东西从哪来，每个来源一个模块；
* :mod:`app.world.agents` —— world 的几类 agent 怎么调模型（模型、trace、成本）；
* :mod:`app.world.rounds` —— 收件箱的处理：一轮处理所有还没经过一轮的消息，一次只跑一轮；
* :mod:`app.world.pending` —— 收件箱里的消息走到了哪一步：还没经过一轮、处理完了、放弃了；
* :mod:`app.world.main_agent` —— 主 agent 的一轮；
* :mod:`app.world.actions` —— 只有主 agent 才有的动作；
* :mod:`app.world.perception` —— 感知判断 agent：谁会察觉、察觉到什么，以及告知他们；
* :mod:`app.world.npc` —— NPC agent：扮演一个 NPC 完成一次互动；
* :mod:`app.world.answer` —— 应答 agent：回答问 world 的问题，只读；
* :mod:`app.world.unfinished` —— 没跑完的一轮里已经发生、收不回来的事；
* :mod:`app.world.admin` —— 记录的人工读写接口。

来源登记、收件箱和人工接口由 world 的插件（:mod:`app.plugins.world`）登记。
"""
