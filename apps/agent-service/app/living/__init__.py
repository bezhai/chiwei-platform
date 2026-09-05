"""living —— 新 world/life 引擎（chiwei-life-engine-minimal）。

骨头层（T1）——三件持久的事和一条并发纪律，不含任何循环 / 轮次 / prompt：

  * :mod:`app.living.records`     三类持久数据的形态
  * :mod:`app.living.place`       位置比对规则（三档，纯函数，不问模型）
  * :mod:`app.living.anchor`      时间锚：一缝 / 一轮的身份，派生 id 全挂在它上面
  * :mod:`app.living.serial`      进程内排他占用 + 提交序 append
  * :mod:`app.living.happening`   谁在哪、对谁、通过什么渠道、做了什么说了什么
  * :mod:`app.living.whereabouts` 她此刻在做什么、在哪
  * :mod:`app.living.upcoming`    将要发生什么

世界一直有东西在到期（T3）——这两条腿一免费一低频，life 循环不参与：

  * :mod:`app.living.calendar`    这个家的客观时刻表 + 到期变成发生过的事（不花模型钱）
  * :mod:`app.living.world`       稀疏轮次：有什么新东西该出现了吗，默认「没有」
  * :mod:`app.living.clock`       两条时间源和它们的节点

她一直在推进（T2）+ 她对外说话（T4）——嘴在外面，耳朵没有：

  * :mod:`app.living.scope`       一缝里每个工具都要问的那四样（谁 / 哪个泳道 / 几点 / 哪一缝）
  * :mod:`app.living.snapshot`    她进入一缝时读到的东西：状态快照，不是历史回放
  * :mod:`app.living.loose_ends`  她自己挂着没了结的事
  * :mod:`app.living.moment`      一缝：默认「继续」，被带走时换的是一件事
  * :mod:`app.living.phone`       手机：信封可感、内容要她去看，持久游标
  * :mod:`app.living.whitelist`   会话白名单：哪些会话进她的视野（手机那道主闸的判据）
  * :mod:`app.living.reading`     读别人发给她的文件（没有"书"这个注册物）
  * :mod:`app.living.pictures`    她自己做过的图：存永久句柄，跨缝找得回
  * :mod:`app.living.web`         上网：手里有问题就查，没有就随便翻
  * :mod:`app.living.mouth`       嘴：把她的意思渲染成人话发出去（**没有入口**）
  * :mod:`app.living.takeback`    撤回自己说过的话，按她自己台账上的编号指
  * :mod:`app.living.landing`     渠道那边关于她这次开口的事实，事后按 id 取回来
  * :mod:`app.living.nudge`       有人叫她就提前一缝，但不代她回复

三条贯穿全包的判断，散在各模块里容易只看到一半：

  * **"谁收得到"在写入时就定死。** 事件行里存着"发生那一刻谁在哪"的快照，读取侧
    一次位置查询都不做——同一条记录什么时候读都裁成一个样子。
  * **占用是进程内的**，前提是 agent-service 单副本；这个前提本来就压在 framework
    的 interval time source 上，见 :mod:`app.living.serial`。
  * **游标和消费是两回事。** 身边发生的事按提交序 ``seq`` 往前读（她在场就是感知到
    了，没有"已读"这个动作）；日历项按"拿走过没有"交付（会有晚提交，时间窗必漏）。

跟 ``app/life`` / ``app/world`` / ``app/nodes/life_wake.py`` 那套旧引擎**没有任何
关系**：不 import、不复用、不兼容。旧引擎在这个实验里不参与。
"""
