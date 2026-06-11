# 给 agent 加会话续接(session_id + Redis 上下文)+ world/life 用上 + 降频

## Problem

world 和三姐妹现在每次被唤醒都是失忆的:`Agent.run` 纯无状态(core.py:517-551,对话只是个局部变量,跑完即弃),每轮拿"谁在哪+几点+这次为啥醒"现攒一段 prompt 扔给模型,跑完全丢。`session_id` 只是 langfuse 的归类标签,不读写任何历史。

coe 一小时实测暴露两个后果:
- **卡在一个 moment 出不来**:world 不记得"这顿饭吃了 45 分钟、广播过两百条餐桌动静",每次醒来都觉得"哦饭点,产条餐桌动静吧",41 分钟无限重开同一顿晚餐(3384 个 event 几乎全在餐桌/厨房)。
- **高频自激**:life 几乎每轮 raise_intent → 立即唤醒 world → world 必广播给全房间 → 三人又各自醒,82 event/min 打爆模型限流(429)、部分 world_tick 进 DLQ。

降频治第二个(频率),治不了第一个(场景推不动)。要让她们活得对,得让她们**真的记得自己经历过什么**——这正是 agent 基建完全没有的能力。

## Goal

agent 有"会话续接"能力:`Agent.run` 传入 session_id 就接着上次往下、不传就新建一个返回。world 和三姐妹各自用一个确定性 session_id 续接,于是每次被唤醒能看到自己之前 emit/move 过啥、想过啥、世界几点了、这顿饭吃多久了。配合降频闸,她们活得有节奏、世界沿时间往前走、不卡在单一场景。

第一刀只在 coe 测一小时,验收口径:
- langfuse 里 world / akao / chinagi / ayana 各一条**真正连续**的 session——点开某轮,输入里带着前几轮的对话与工具结果(模型在续接),不是又一段从零组装的 prompt;Redis 里能查到对应的 session 上下文、随轮增长。
- 世界**沿时间推进**:event 的房间/主题随现实时间变化(晚餐能收桌、散场、进入饭后),不是一小时全卡一个场景。
- 频率**健康**:不再 82/min 量级自激,429 消失或偶发。
- 6 张 data 表读写正常。

## Non-goals

- **上下文压缩**:第一刀不做。一小时几百轮塞得下,验证通过再做按需取+滚动摘要。
- **chat 主对话链路改造**:它的 quick_search 历史拼接照旧,不动;新会话能力对它默认关闭(不传 session_id 就是现在的无状态行为)。
- **对话记忆的强持久/审计**:它是 Redis 工作缓存,pod/Redis 重启或 24h 过期就丢——可接受,从 PG 硬事实降级续上。不追求 durable、不追求跨重启恢复对话。
- **跨角色记忆共享**:信息差命门不变,各自 session 只含各自视角,life 绝不读 world 全局。
- **跨天 session 滚动的精细化**:session_id 含日期、跨天自然换一条,边界后续再收。
- **agent 自动新建并返回随机 session_id**:world/life 用确定性 id 自己算、不需要 agent 生成返回;这个通用模式留待将来 chat 复用会话能力时再做。

## Key design decisions

1. **会话续接做成 agent 的能力,API 契约钉死:`Agent.run(messages, session_id=None, ...)`,返回类型不变(仍是 Message)。** session_id 由调用方提供(world/life 确定性派生、自己算),agent 不生成也不返回 session_id,所以不改返回签名。
   - **`session_id=None`**:完全无状态,行为与现在逐字节一致——不读不写 Redis。chat 等现有调用方零影响。
   - **`session_id` 给定**:有状态续接——读 Redis 里该 id 的历史拼到 messages 前、跑、跑完把本轮追加写回该 id。
   "把对话攒起来续接"是 agent 的基础能力,world/life 是第一批消费者;第一刀只做到够它俩用,不为 chat / 压缩 / 跨天一起设计。

2. **session 上下文由 agent 内部存 Redis,TTL 24h。** 不走 PG——它是给模型续接用的工作记忆/会话缓存,不是要永久审计的业务状态(观测有 langfuse trace 兜着)。每次唤醒按 session_id 从 Redis 读历史、跑完把本轮追加写回。选 Redis 而非 PG:轻、自带过期、契合"24h 会话"语义。agent-service 已有 Redis 接入(app/capabilities/redis.py),不新引依赖。
   - **TTL 语义**:成功写回时刷新 24h;读到 missing(过期/重启/首次)按冷启动从 PG 硬事实续上、不报错;不让活跃 session 因只读或长轮执行中途过期。
   - **value 上限安全阀**:单条 transcript 有字节/轮数上限(第一刀不做压缩,但要防失控)——触顶就丢最老几轮 + log,绝不静默撑爆 Redis value 或模型上下文。这是"别炸"的机制边界,不是内容决策。

3. **session_id 确定性派生,复用现有 `make_session_id(lane, actor, date)`。** world/life 是无状态唤醒,不用额外存"我的 session_id"——每次同一公式算出同一个 id 丢给 agent 即续。妙处:这个 id 同时是 langfuse 的 session 标签和 Redis 的上下文 key,于是"langfuse 里看到的连续 session"背后第一次真有连续上下文,彻底解掉"session 没复用"。(chat 这类有明确会话主体的场景可改用 agent 新建返回的随机 id,接口都支持。)

4. **存进 Redis 的对话要能无损喂回模型续接。** 存的是可重放的消息序列(含工具调用与结果),保留各 provider 私有字段(如思维签名 thought_signature),不是"能查的摘要"——丢字段会让重放时模型行为漂移。Redis 存 JSON 天然支持嵌套结构,不用动 persist 层。第一刀全量带、不压缩。

5. **降频分两层:硬机制闸 + 软内容判断。** 既兜住频率底线、又不违赤尾宪法。
   - **机制闸(硬,只管节奏)**:① world 被唤醒最小间隔 1 分钟——做在 `IntentRaised → world_tick` 这条边上(短于 1min 的连续 intent 合并/延后成一次唤醒),光靠 sleep 下限挡不住 intent 立即唤醒;sleep 自排配套 1min 下限、1h 上限。② 三姐妹一轮跑完进 cd,cd 是**延迟+合并不是丢事件**:cd 内到达的 event 攒着、cd 到了一起醒一并感知,绝不 drop(world 是唯一启动源,漏感知会压到死寂)。
   - **内容判断(软,交给模型)**:world 收到反馈后该不该广播,由她自己判断"符合世界、不需要纠偏就只 sleep 不广播"——内容决策用 prompt + 记忆引导,不用 if 分支强制。
   - **边界**:机制闸只决定节奏(跟现有心跳 10min、sleep 上限 1h 同类),兜住"不再失控到 82/min";广播与否绝不硬编码,由 prompt 负责质量。

6. **状态分两层:硬事实在 PG,对话记忆在 Redis。** presence(谁在哪)、world_time、event 信箱、LifeState 这些客观/可查硬事实仍在 PG(world 行动、emit 投递过滤、life 唤醒都依赖)。对话记忆是 Redis 工作缓存。好处:Redis 那层丢了(重启/过期),world/life 还能从 PG 硬事实 + 信箱续上,降级成"记不太清刚才聊啥、但知道在哪几点信箱里有啥",像睡了一觉、不崩。这也让现在的 LifeState/WorldState 各归其位(硬事实快照),不和对话记忆抢职责。

7. **world 串行化(锁覆盖全段)+ transcript turn 幂等 + 诚实交代残留重复风险。**
   - **锁覆盖全段**:world 现在无锁,确定性 session_id 又把三源打到同一个 Redis key,所以 world 要像 life 一样按 actor 串行化,且锁必须覆盖「读历史 → run/工具副作用 → 追加写回 + 刷 TTL」整段,不能只锁一头——否则两源并发各自读改写同一 transcript 会互相覆盖。
   - **turn 幂等**:同一 durable 触发(intent 重投)不能再次往 transcript 追加同一轮(沿用 intent_id 派生标识本轮、写回前查重)。
   - **残留风险诚实交代**:对话是 Redis 工作缓存、硬事实在 PG,大多数失败能降级恢复;但有一个残留漏洞——某轮已 emit 但 transcript 写回失败时,下一轮从 Redis 读不到"刚 emit 过"、从 PG 的 presence/world_time 也看不出"刚广播了啥",而 event_id 含 summary、模型换个措辞就绕过去重,可能重复广播一条近似动静。第一刀**接受这个偶发重复**(只测一小时、写回失败罕见、重复一条近似动静不致命),不为它引入 PG recent-emits ledger;上 prod 后若真困扰再补 ledger 作硬事实。

## Caller coverage

改动集中在 agent core + world/life 引擎,wiring 不变。逐个确认:
- `Agent.run` / `Agent.stream`(core.py)—— 现有调用方:chat 的 `agent.stream`(agent_stream.py)、world_tick、life_wake。新增 session 续接**必须是可选、默认关闭**:不传 session_id 时行为与现在逐字节一致;grep 全部 `.run(` / `.stream(` 逐个确认不被波及。
- `world_tick`(world/engine.py)—— 唯一入口,三源唤醒。接入续接 + 串行化 + 降频后,renotify / presence / world_time / cold_start / max_retries=1 / fire_self_wake 命门必须仍在。
- `life_wake_node` / `_run_life_round`(nodes/life_wake.py)—— 接入续接 + cd 后,single_flight / 空 inbox early-return / 只标本轮已读 / 信息差(不读 world) / max_retries=1 必须仍在。
- intent → world 边(intent_to_world_tick)—— 加最小唤醒间隔合并闸时,确认 IntentRaised 的 durable 幂等(intent_id 派生)不被破坏。

## Data & deployment impact

- **存储**:对话上下文存 Redis(session_id 为 key,TTL 24h),不新增 PG 表、不动 persist。
- **prompt**:world / life 循环指令在代码内联(不在 Langfuse),降频和"默认不广播"的措辞改代码字符串,无 Langfuse 发版。
- **部署**:agent-service + vectorize-worker 同步 release(一镜像多服务)。
- **重新部署前先清 RabbitMQ 堆积**:上轮 undeploy 只停了消费,IntentRaised / EventArrived / delayed_trigger 的 durable 积压还在队列,新版本起来会被旧高频消息淹没、污染验证,部署前要清掉。
- **冷启动**:验证前清空 coe 6 张 data 表 + 清掉 Redis 里相关 session key,从干净世界冷启动。

## Tasks

1. **agent 补"会话续接"能力**(task2/3 的地基,须先完成,2、3 再开;2 和 3 碰不同文件可并行)
   目标:`Agent.run`/`stream` 传 session_id 则从 Redis 读历史续接、不传则维持现在的无状态(返回类型不变);本轮对话(含工具调用与结果)跑完追加写回 Redis、刷新 24h TTL。
   产出:core 的 session 续接路径(传则续/不传则无状态)+ Redis 上下文读写(写回刷 TTL、读 missing 冷启降级)+ 无损可重放的消息序列化 + transcript 字节/轮数上限安全阀。
   验收:单测证明同一 session_id 连续两次 run,第二次的模型输入带着第一次的对话与工具结果(格式无损、不丢 provider 私有字段);不传 session_id 的 run 行为与现在逐字节一致(chat 路径回归绿);Redis 里能查到该 session 的上下文;单测覆盖 transcript 触顶截断 + TTL 刷新。

2. **world 接入会话续接 + 串行化 + 降频**
   目标:world_tick 用确定性 session_id 续接(记得自己这一天 emit/move 过啥、几点了),补串行化与 turn 幂等,落地属于 world 的降频(默认不广播、最小唤醒间隔 1min)。
   产出:world_tick 传 session_id 续接;按-actor 串行化锁 + durable 重投 turn 幂等;最小唤醒间隔 1min 做成 intent→world 边上的合并闸;循环指令从"宁可多产/别睡太久"改为"符合世界就只 sleep 不广播";sleep 自排下限 1min;world 既有命门(renotify/presence/world_time/cold_start/max_retries=1/self-wake)保持。
   验收:coe 冷启动后 langfuse 上 world 一条连续 session、后续轮带历史且 Redis 可查;世界沿时间推进、不卡单一场景;并发/重投下上下文不错乱不重复;单测覆盖续接 / 默认不广播 / 唤醒合并闸 / 重投幂等。

3. **life 接入会话续接 + cd 降频**
   目标:三姐妹每轮用各自确定性 session_id 续接(记得刚才想过做过啥),加一轮跑完后的 cd 把唤醒频率压下来。
   产出:life_wake 传 session_id 续接;raise_intent 的"回应刚才的动静就调"改为"真要改变处境才起意图、多数时候只是经历这一刻";life cd = 延迟合并不丢事件(cd 内 event 攒着、cd 到了一并感知);既有命门(single_flight/空inbox/只标本轮/信息差/max_retries=1)保持。
   验收:coe 上三姐妹各一条连续 session、后续轮带历史且 Redis 可查;raise_intent 频率显著下降、cd 内事件不丢;信息差 AST 断言仍通过(life 不 import/读 world);单测覆盖续接 / cd 延迟合并 / 一轮一意图。

4. **coe 一小时端到端验证**
   目标:清干净环境冷启动,实测新范式活得对。
   产出:清队列 + 清 6 表 + 清 Redis session → 部署 agent-service/vectorize-worker 到 coe → 跑约一小时 → 拉 langfuse + 查 Redis + 查表的证据。
   验收:四条 Goal 口径逐条给证据(连续 session 的 trace、event 房间随时间变化、频率回落/429 消失、Redis 里 session 上下文随轮增长);另记 Redis key 大小 / 单轮 replay token 量 / 会话写回失败计数,确认缓存没失控。
