"""world：一个独立部署的引擎，自己醒来、看现实、让世界变化，变化写进它自己的记录；谁会察觉
到一个变化由它判断并告知当事人，NPC 由临时 agent 扮演，别人问它某处什么样、谁在哪时它回答。

它和三姐妹的 life 互不 import（CI 规则 ``scripts/check_world_life_imports.py``），运行时
也不加载 life 的代码：它是同一个镜像上的另一个 App，进程只起 world 的插件（清单是
:mod:`app.manifests.world`）。它和别的参与者之间唯一的连接是通信机制（:mod:`app.messaging`）。

**每个插件一个目录**，目录之间不互相 import（``tests/world/test_plugin_boundaries.py``）：

* :mod:`app.world.core` —— 插件 ``world``，world 的核心：主 agent 的一轮（``main_agent``）和它自己
  才有的动作（``actions``）、收件箱的处理（``rounds``、``pending``）、自己定的醒来（``wake``），以及
  ``world_intake`` 服务（``intake``）：收件处理和提问交给哪些插件；
* :mod:`app.world.records` —— 记录：私有卷上的一棵自然语言文档树，和它的两个插件（读的查询工具
  ``records``；写记录、记录目录和人工读写接口 ``record_keeping``）；
* :mod:`app.world.reality` —— 知识来源"现实"的插件：天气、网页搜索；
* :mod:`app.world.told` —— 知识来源"别人告诉世界的事"的插件，收件处理登记在 ``world_intake`` 上；
* :mod:`app.world.perception` —— 感知判断的插件：谁会察觉、察觉到什么，以及告知他们（提供
  ``perception`` 服务）；没跑完的一轮里已经发生、收不回来的事（:mod:`app.world.perception.unfinished`）；
* :mod:`app.world.npc` —— NPC 的插件：让 NPC 出场，一个临时 agent 扮演他完成一次互动；
* :mod:`app.world.answer` —— 应答的插件：回答问 world 的问题，只读，登记在 ``world_intake`` 上。

**目录外只有插件之间共享的东西**：

* :mod:`app.world.services` —— 插件之间的服务契约（``world_intake``、``perception``）；
* :mod:`app.world.agents` —— world 的几类 agent 怎么调模型（模型、trace、成本），world 的名字、
  自己排的醒来怎么认、摆到 agent 眼前的时刻；
* :mod:`app.world.volume` —— 私有卷上按泳道分的那个目录、单写者锁和原子写。
"""
