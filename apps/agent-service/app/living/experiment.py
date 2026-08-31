"""这批代码允许在哪个环境里自己跑起来 —— 实验的边界，一处定义。

**这不是行为开关，不违反"不用工程思维解决 agent 的不确定性"**：它管的是"哪套引擎在
这个泳道上被注册"，不是"她该不该醒"。她醒不醒由她自己的循环和 prompt 决定，跟这里
无关。

一句话说清它在拦什么：**同一个泳道上不能有两套引擎同时跑。**

  * 新引擎（``app.living`` 的四条钟）只在 ``coe-*`` 上注册。不加这道门，这个分支合进
    main、prod 一发版，world 加三个 life 立刻在 prod 库上开跑——建表、写数据、烧模型钱。
  * 旧引擎（``app/wiring`` 里那批 world / life / chat / cron 的 wire）在 ``coe-*`` 上
    **一条都不注册**。不加这道门，dev bot 发一条消息旧新引擎各回一份，旧 world 每十
    分钟一轮、眼睛每小时一班照烧钱，``persona_review`` 还会中途给 ``bot_persona`` 盖
    新版本——而新引擎每一缝都读这张表。

两边共用**同一个判据、相反的分支**，所以不可能出现"两边都注册"或"两边都不注册"。判据
只在这里写一次；抄一份到别处，迟早会有一边先改。

**判据是一个具体的泳道名，不是 ``coe-*`` 这一整个等级。** ``coe`` 是通用隔离等级
（独立离线基建、写坏了不外溢），任何人都可能为任何目的开一个 ``coe-xxx``；拿等级当
实验身份有两个后果，而且都是静默的：

  * 一个跟 living 毫无关系的 coe 部署会被拉起四条钟，在人家的库上开始生活、烧钱；
  * **旧引擎从此没地方做隔离验证**——schema 变更、消息协议变更这类改动恰恰只能在 coe
    上验（ppe 共用 prod 组件、会写脏线上数据），而那正是被关掉的那一批 wire。

所以是 :data:`LIVING_EXPERIMENT_LANE` 这一个名字。要再开一个实验泳道，改这里的常量、
或者把它扩成一个明确的名单，别退回按等级判。
"""

from __future__ import annotations

from app.runtime.lane_policy import current_deployment_lane

# living 实验就跑在这一条泳道上。名字本身遵守泳道命名规范（``coe-*`` = 独立离线基建，
# 见 ``.claude/rules``），但**身份是这个名字，不是那个前缀**。
LIVING_EXPERIMENT_LANE = "coe-living"


def on_the_living_experiment_lane(lane: str | None = None) -> bool:
    """本进程部署在 living 实验的那条泳道上吗。

    ``lane`` 省略时读进程环境（``LANE``）。拿不到泳道 = prod = 不是实验泳道。
    """
    resolved = lane if lane is not None else current_deployment_lane()
    return resolved == LIVING_EXPERIMENT_LANE
