# 赤尾阶段 1B:world / life 自排唤醒真生效

## Problem

阶段 1A 上 coe 验证暴露了"安静时段全体冻死、世界静止"。饭后入夜各自关门、客观上没有跨空间的动静,world 诚实地不 notify;而 life 只能被 notify 唤醒,于是没人醒、没人 act、world 拿不到任何"人的动作"输入,只能每轮给一张静态快照反复描边(实测一小时 23 版 detail,三姐妹姿势一次没变,世界唯一的变化是碗盘水珠慢慢变干、滴水声变稀)。

更底层的一层是:world 的 sleep 本就从未生效。`WorldState` 里没有存"下次该醒的时刻",所以 world 调 sleep(3600) 之后,600 秒的保底心跳照样无条件把它拍醒,长睡意愿一秒没兑现。`docs/plan/chiwei-world-self-schedule-cst-time-spec.md` 早把 self-wake gate 设计好了,但代码层一直没落地。

## Goal

world 和 life 都能自己排"过多久再醒",而且这个意愿真生效——不再被保底心跳拍醒。角色被唤醒过一次后,能自己排着持续往下过日子(写完一题接着写、收拾完挪去客厅、困了一觉睡到天亮),她们的 act 持续喂给 world,世界随之往前流动,不再停在一张静态快照上。

可观测验收:coe 上某角色被 world 起头唤醒后(如饭点的 notify),在之后没有新 notify 的安静时段,她仍按自排自然地一轮轮往下过(life 状态持续推进、act 自然出现),world 的 detail 随之体现出"人在变化"(位置、活动往前走),而不是只有环境物理(水干、天色)在微动。注:1B 解决的是"被起头唤醒后就冻死",冷启首轮仍依赖 world 在饭点 / 早晨这类钟点推出的动静起头(见 Non-goals),本刀不消除这层依赖。

## Non-goals

- 同场景角色之间的直连交流(留给 1C)。
- 中性 watchdog(漏投 / 自排丢失 / 信箱滞留这类机械失败的兜底,后置)。
- life 独立保底心跳。第一次醒仍靠 world 在饭点 / 早晨这类钟点推出的动静 notify 起头,之后由 life 自排接力;不引入定时扫所有角色的心跳。
- 按时段动态调 sleep 上限。代码按现在几点替 agent 判断该睡多久属于工程脑,改用固定上下限 + agent 自主决定。
- 真人飞书聊天打断 world / life(新范式下归属待重审,不在本刀)。

## Key design decisions

1. **到点 gate(world 与 life)。** 各自的 state 存"下次该醒的现实时刻"(用现实 aware 时间,不用会因 gate 停滞的 world_time)。唤醒按语义分两类:外部刺激(world 的 notify、补敲未读、角色的 act 唤醒,以及未来的真人聊天)永远放行、能立刻打断长睡;自排(self)与保底心跳走 gate。gate 判两件事——一是到点没到(now ≥ 目标时刻),二是这条唤醒还作不作数:self / 心跳唤醒携带它被排时的目标时刻,到期时与 state 里当前的"下次该醒时刻"比对,不一致说明已被更新的自排或外部打断覆盖、判废。选这个而不是"把心跳调得更聪明":让自排意愿真生效的唯一办法,是让心跳和 self 尊重这个时刻;而被覆盖的旧唤醒必须能识别作废,否则它到期会误触发、或它那个未来值会一直挡住心跳。

2. **life 自排照搬 world 的 sleep 机制,不另起炉灶。** 一轮内多次排只留最后一次(round-scoped 覆盖)、循环收口只 emit 一条延迟唤醒。world 那套已经为"防唤醒风暴"验证过,life 是同构问题、同构解,复用同一套延迟触发管线。

3. **sleep / schedule 固定上下限,不按时段动态。** 下限防排得太密(像神经质每分钟醒一次想一轮)、上限放宽到能睡一整觉(夜里一觉到天亮);具体睡多久由 agent 自己看钟点定。agent 知道现在几点、能自己判断夜里睡久白天睡短,真有事 world 的 notify 会把她从长睡里叫醒(外部刺激不走 gate)。

4. **给 life 补 round marker 幂等,不靠单飞锁 + 冷却兜重放。** life 现在只有单飞锁加 45 秒冷却挡重复唤醒,没有 world 那样的轮幂等。durable 重投和 debounce 补敲两个唤醒源叠加时,life 会重放出两轮、transcript 重复(意识流落 PG durable 之后,这个缺陷从"24 小时自愈"变成永久)。把 world 已有的轮幂等模式对称补给 life。

5. **next_wake_at 存进各自的 State,不新建表。** 它是"该主体下次醒"的状态,天然属于该主体的 state,而 State 已经是 durable、按 key 可查可清。不为它单开一张表,避免无谓的读写面。

6. **life 的自排唤醒是独立信号,不复用 event 通道。** life 现有入口在信箱空时 early return,且 act / 幂等的种子取自本轮读到的 event_ids;而自排唤醒时信箱往往是空的(她不是被新动静叫醒、是自己排的时间到了),复用 event 通道会让 gate 过了也因空信箱不跑、或多次自排共用空种子导致误去重。所以自排是独立的唤醒形态(独立信号 + 自排缘由):life 收到它时即使信箱空也跑一轮(输入语义是"你自己排的时间到了,过这一刻"),且幂等 / act 种子改用对自排也成立的稳定派生(不依赖 event_ids)。

## Caller coverage

- **WorldState / LifeState**:加"下次该醒的时刻"字段,影响所有读写方(world 写状态、life 存状态、各自的 read 方)。现状无此字段;改后自排时写入、唤醒时读取判 gate。framework migrate 对已有数据的表加 nullable 列,additive、不阻塞。
- **world 唤醒入口(心跳 / self 唤醒 → world 推演)**:现状无条件唤醒;改后走 gate,未到时刻的心跳 / self 唤醒判废早返,外部刺激(act 唤醒等)不受影响。
- **life 唤醒入口(信箱敲门 → life 醒来)**:现状有 event 就跑;改后区分唤醒源——外部刺激(信箱敲门 / 补敲未读)永远跑,新增的独立 self 自排唤醒走 gate(到点 + stale 判定),且 self 唤醒时信箱空也跑一轮。
- **life 的 agent 工具集**:现状是更新状态 + act 两件;改后加自排一件。
- **life_wake prompt(langfuse)**:加一句说明新的自排工具(像 world prompt 说明 sleep 那样),让角色知道可以自己排下次醒;不增删既有变量。

## Data & deployment impact

- **Data 变更**:WorldState / LifeState 各加一个 nullable 的"下次该醒时刻"字段,framework migrate 自动 ADD COLUMN;新增一个 life 延迟唤醒信号 Data(对应 world 的 self 唤醒信号),按 framework Data 注册三步检查走(grep 保留列名 + 读 sibling Data + 端到端 insert 验证)。本次只加列、不删列,不触发 migrate 删列 fail-closed。
- **Prompt 变更**:life_wake 加自排工具说明,先发 coe 泳道 label、trace 验证后再同步线上;变量契约不变。
- **部署**:agent-service 改动,部署需同步 release vectorize-worker(一镜像多服务)。
- **部署中断**:部署杀 pod 会中断正在跑的 world / life loop;coe 验证本就清库冷启重跑,无额外影响。
- **coe 冷启**:新列 additive、旧 coe 表自动补上;MQ 里遗留的旧 schema 延迟唤醒消息靠 per-message drop 自愈,清库时注意 PG 之外的遗留。

## Tasks

**Task 1 — world 到点 gate + next_wake_at(先单独把样板跑通)**
目标:让 world 的 sleep 第一次真生效,长睡不再被保底心跳拍醒。
产出:WorldState 加"下次该醒的现实时刻"字段;world 的 sleep 把目标时刻写进 state、并让它排的 self 唤醒携带该时刻;world 唤醒入口对心跳 / self 走 gate(未到点、或目标时刻与 state 当前值不符即判废),对外部刺激(act 唤醒、补敲)放行。
验收:coe 上 world 调一个长 sleep 后,后续保底心跳不再把它拍醒、它睡到自己定的时刻才醒(以 trace / world_state 的时间间隔为证);一条被 gate 判废的心跳唤醒留下可观测痕迹(日志,或不产出新 state)。
为什么先单独做 world:world 已有现成的 self 唤醒信号作为 gate 的合法对象,先在它上面把 gate + stale 判定 + next_wake_at 生命周期跑通、作为 life 侧的样板;life 的 gate 没有合法 self 来源(那要 Task 2 才建),不能先于 self wake 落地,否则会被逼着误用 event 通道。

**Task 2 — life 自排(独立 self wake + 工具 + 空信箱语义 + life gate)**
目标:角色能自己排下次醒、被起头唤醒过一次后持续往下过日子,不再干等 notify。
产出:life 多一个自排工具(照搬 world sleep 的 round-scoped 覆盖 + 收口只 emit 一条);一个独立的 life 自排唤醒信号(携带目标时刻、自排缘由),不复用 event 通道;life 收到自排唤醒时即使信箱空也跑一轮(输入语义是"你自排的时间到了"),幂等 / act 种子改用对自排也成立的稳定派生;life 唤醒入口加 gate(外部刺激放行,自排 / 心跳走到点 + stale 判定),自排时把目标时刻写进 LifeState;life_wake prompt 加该工具说明。
验收:coe 冷启后,某角色被 world 起头唤醒后,在之后没有新 notify 的安静时段仍按自排一轮轮往下过(life 状态持续新增、act 自然出现);world 的 detail 随之体现人在动,不再是静态快照。
依赖:Task 1(沿用其 gate + stale 判定 + next_wake_at 生命周期样板)。

**Task 3 — life round marker 幂等**
目标:堵住 life 重复唤醒重放出两轮、transcript 重复的缺陷。
产出:life_wake 加轮幂等(对称 world 的 round marker),覆盖 durable 重投与 debounce 补敲两个唤醒源。
验收:同一批唤醒(durable 重投 + 同时 debounce 的 event)只让 life 跑一轮、transcript 不重复(测试复现双源唤醒并断言单轮)。
