"""living —— 三姐妹的 life 引擎（chiwei-life-engine-minimal）。

world 不在这个包里。world 与 life 是两个独立的引擎，互不 import、不共读共写存储，
唯一的连接是通信机制（:mod:`app.messaging`）。

她的功能分成插件，每个插件一个目录 ``app/living/<插件>/``。插件之间不互相 import，
只经服务交互（``tests/living/test_plugin_boundaries.py`` 守着）：

  * :mod:`app.living.services`    插件之间的服务：接口（Protocol）和接口里出现的数据类型
  * :mod:`app.living.common`      各插件共用的纯函数：转义、时刻必须带时区、旧行的 NULL、周界

插件：

  * :mod:`app.living.persona`     她是谁：「她是谁」那份正文的版本链（她自己每周改的那条），和喂进
    各条 prompt 的人设变量（``persona`` 服务）；她一轮 prompt 的「她是谁」那一段
  * :mod:`app.living.residents`   谁住在这儿：三姐妹在世界里的名字，取自人设表、开收件箱时检查
    （``residents`` 服务）；住的是哪几个人由清单定。名字就是地址
  * :mod:`app.living.her_records` 她自己的经历（对谁、通过什么渠道、做了什么说了什么）和她此刻在
    哪、在做什么，都只是她自己的；她一轮里"刚做过说过""手上"那两段（``her_records`` 服务）
  * :mod:`app.living.loose_ends`  她自己挂着没了结的事：``keep_in_mind`` 这只手、她一轮里挂念和
    刚到点的那两段（``loose_ends`` 服务）
  * :mod:`app.living.pictures`    她自己做过的图：画 / 上网找 / 翻一翻 / 拿出一张看，存永久句柄，跨轮
    找得回（``pictures`` 服务：手机和嘴经它认她的图）
  * :mod:`app.living.phone`       手机：信封可感、内容要她去看，持久游标；会话白名单决定哪些会话进她的
    视野，一轮之内只定一次（``phone`` 服务：发消息、撤回、读文件、提前叫醒她经它问手机上有什么）
  * :mod:`app.living.household`   她在通信机制里的那一面：三姐妹的收件箱是她唯一的入口，收件只存储，她
    醒来时读（``received`` 服务）；她做的事发出去，一段经历一条汇总给 world，只对姐妹说的话直接给那位
    姐妹，从她存下的经历里取，没确认发出去的原样再发，只在她的轮次队列上发
  * :mod:`app.living.reading`     读别人发给她的文件（没有"书"这个注册物，她读得到的严格等于她看得见的）；
    读一程的 agent 只有一只往后翻页的手，读完揉出新印象
  * :mod:`app.living.day_page`    跨天沉淀：每天凌晨她把刚过去那一天自己写成一页，之后每一轮都读得到
    （不是机器折叠原文，原始记录一条不动；``day_pages`` 服务），一条自己的队列
  * :mod:`app.living.persona_review` 每周回看：周一早上她读上一周自己写的那几页，重写一版「我是谁」进
    版本链（周一没写成，同一周后面几天的早上再试）。锚是 ``bot_persona.persona_core`` 那个扁平列——不给
    锚她就是在自我回流；一条自己的队列
  * :mod:`app.living.speaking`    嘴：把她写下的那句话发出去（**没有入口**），交出去之前过一道检查（禁用词
    和判词模型，坏了放行但记账）；撤回自己说过的话，按她自己台账上的编号指；渠道那边关于她这次开口的
    事实，事后按 id 取回来
  * :mod:`app.living.web`         上网：手里有问题就查，没有就随便翻
  * :mod:`app.living.guides`      手边那几份写好的说明：读一份 / 照说明在沙箱里跑一条
    命令。她自己长什么样就写在其中一份里；正文在盘上（:mod:`app.skills` 扫目录、热
    加载、渲染），有哪些可读只能从 prompt 变量进——工具 schema 装不下会变的清单
  * :mod:`app.living.life`        她的一轮本身（T2 她一直在推进 + T4 她对外说话 —— 嘴在外面，收到什么都不会
    直接让她跑一轮）：醒来周期默认「继续」，被带走时换的是一件事（:mod:`app.living.life.moment`）；她跨
    moment 的连续上下文（键、阈值、素材表、写失败怎么算）也在那里，存取和裁剪的机制在基础层
    :mod:`app.agent.continuity`；有人叫她、或者有她没看过、发件方要叫醒她的收件，就提前叫醒她一次，但不代
    她回复（:mod:`app.living.life.nudge`）；她进入这一轮时读到的是状态快照，不是历史回放
    （:mod:`app.living.life.now`）

三条贯穿全包的判断，散在各模块里容易只看到一半：

  * **每个人只读她自己的记录。** 她做过的事、她在哪，只给她自己读；别人做了什么由 world
    判断谁会察觉后告诉她，或者姐妹直接对她说，都进她的收件箱。life 里没有一条按位置把别人
    的经历分给她的路（``tests/living/test_only_her_own_records.py`` 守着）。
  * **占用是进程内的**：她的三条队列（轮次、日记、回看，:mod:`app.living.life`、
    :mod:`app.living.day_page`、:mod:`app.living.persona_review` 向宿主登记的 :class:`app.host.Turns`）都只在一个进程里互斥，前提是 agent-service 单副本；滚动发布时新旧
    两个进程会重叠一段，这段时间里同一格可能被跑两次。
  * **游标按提交序。** 她的经历按提交序 ``seq`` 往外讲（:mod:`app.living.household.outgoing`）；按发生
    时刻开窗会漏掉晚提交的记录。

旧引擎（``app/life`` / ``app/world`` / ``app/nodes/life_wake.py``）已经整批删掉，这个
包一行都没复用过它。最后留下的是「她是谁」那条版本链——它有 prod 数据、而且是活的，
所以搬进了 :mod:`app.living.persona`，不是重写也不是兼容层。
"""
