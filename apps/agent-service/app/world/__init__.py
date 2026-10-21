"""world：一个独立部署的引擎，自己醒来、看现实、让世界变化，变化写进它自己的记录；谁会察觉
到一个变化由它判断并告知当事人，NPC 由临时 agent 扮演，别人问它某处什么样、谁在哪时它回答。

它和三姐妹的 life 互不 import（CI 规则 ``scripts/check_world_life_imports.py``），运行时
也不加载 life 的代码：它是同一个镜像上的另一个 App，进程只起 world 的插件
（:mod:`app.plugins.world`、记录的两个插件、现实和 told 的插件，清单是 :mod:`app.manifests.world`）。它和别的参与者之间唯一的连接是通信机制
（:mod:`app.messaging`）。

* :mod:`app.world.volume` —— 私有卷上按泳道分的那个目录；
* :mod:`app.world.records` —— 记录：那个目录下的一棵自然语言文档树，和它的两个插件（读的查询
  工具 ``records``；写记录、记录目录和人工读写接口 ``record_keeping``）；
* :mod:`app.world.wake` —— 下次醒来的时刻（私有状态）和醒来规则；
* :mod:`app.world.reality` —— 知识来源"现实"的插件：天气、网页搜索；
* :mod:`app.world.told` —— 知识来源"别人告诉世界的事"的插件，收件处理登记在 ``world_intake`` 上；
* :mod:`app.world.intake` —— 收件箱的 ``world_intake`` 服务：收件处理和提问交给哪些插件；
* :mod:`app.world.agents` —— world 的几类 agent 怎么调模型（模型、trace、成本）；
* :mod:`app.world.rounds` —— 收件箱的处理：一轮处理所有还没经过一轮的消息，一次只跑一轮；
* :mod:`app.world.pending` —— 收件箱里的消息走到了哪一步：还没经过一轮、处理完了、放弃了；
* :mod:`app.world.main_agent` —— 主 agent 的一轮；
* :mod:`app.world.actions` —— 只有主 agent 才有的动作；
* :mod:`app.world.perception` —— 感知判断 agent：谁会察觉、察觉到什么，以及告知他们；
* :mod:`app.world.npc` —— NPC agent：扮演一个 NPC 完成一次互动；
* :mod:`app.world.answer` —— 应答 agent：回答问 world 的问题，只读；
* :mod:`app.world.unfinished` —— 没跑完的一轮里已经发生、收不回来的事。

主 agent 的一轮和收件箱由 world 的插件（:mod:`app.plugins.world`）登记，记录、来源和它们的
查询工具、人工接口由各自的插件登记。
"""
