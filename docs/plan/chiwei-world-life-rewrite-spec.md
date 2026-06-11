# 重写赤尾的活法：world engine 推动的三姐妹 event 世界

> Clean-slate 重写。取代已作废的单角色 `chiwei-life-engine-rewrite-spec.md`（那一刀"只做 life"是残废，已否）。
> 设计来龙去脉见同目录 `chiwei-redesign-world-model.md`（世界模型）、`chiwei-redesign-day-walkthrough.md`（模拟一天）、`chiwei-life-engine-rewrite-facts.md`（现状代码实证）。

## Problem

赤尾现在"过日子"是 cron 每分钟拉一次、LLM 拍一个带 `state_end_at` 的大状态，没到期 engine 就 `return None` 干等，中途任何事进不来——她卡在"去上学 / 买冰淇淋的路上"。而且她基本是单角色孤立运转，没有别人，一旦 LLM 抽风拍了个"睡一天"的状态，没人纠正、她就真睡一天。根子是两条：她自己锁死自己的时间段、且世界是真空没有外力能打断或纠偏她。

## Goal

一个 world engine + 三姐妹三个 life engine（赤尾 akao / 千凪 chinagi / 绫奈 ayana），被 event 推着走、能自洽运转一天的最小活闭环。可验证：

- 一件指向某姐妹的事发生时，哪怕她正处在一个大状态里，也会被推醒重想，而不是干等到原定结束。
- 她脑子里没有"做到几点"的死时间段：有客观边界的事由 world 到点推 event 结束，没边界的事靠下一个 event 自然打断。
- world 作为发动机最长不睡过 10 分钟（保底心跳钉死最长停摆），但平时克制、绝大多数心跳不产 event、不淹 life。
- 三姐妹同一屋檐下能靠在场说话互相叫醒 / 搭话 / 当面追问，一个反常另一个能感知到、来管（纠偏不退化成 world 自导自演）。
- 信息差成立：某姐妹收不到她不在场、没被波及的事。
- 用户消息仍走 chat 即时回复快路径，且这次对话会作为一个 external event 回灌给那位姐妹（她事后知道"刚和谁聊过"），不让"聊天里的赤尾"和"世界里的赤尾"分叉。
- chat 与整点语音仍能读到某姐妹的当前状态（`current_state` / `response_mood` / `activity_type`）。

## Non-goals

- **跨空间不在场的 life↔life 直连（私信 / 电话）与地点痕迹（纸条）**：放下一刀。注意"在场说话 / 喊话"不在此列——它是环境感知的一种（说话是动作、声音是 ambient），第一刀就有。
- **从对话里提取"约定"语义回灌**：第一刀只回灌"发生过一次对话"，不抠"她答应了什么"。
- **原智鸿作为"特殊 life + 真人接管"**：第一刀他只是个外部消息源（用户发消息进来）。
- 状态分层（背景 / 此刻）、坐标地图、关系网络、world 按角色忙闲 / 心情调推送密度。

## Key design decisions

1. **第一刀范围 = 最小活闭环，但"在场说话"必须在内。** world + 三姐妹三个 life + 环境感知 + 意图动作。环境感知**涵盖在场说话 / 喊话**（说话是动作、声音是 ambient 副产品），所以三姐妹同一屋檐下的叫醒、搭话、当面追问、纠偏第一刀就能跑、不会退化成 world 自导自演。真正放下一刀的是"跨空间不在场的 life↔life 直连（私信 / 电话）"和"地点痕迹（纸条）"——它们是在已验证引擎上的低风险增量。

2. **world 是发动机，最长停摆由 10 分钟保底心跳钉死，不许自己决定睡多久。** 自排（emit_delayed）只能在 10 分钟内提前卡点。否则 world 自排长闹钟会把世界睡死（早 6 点排到下午 4 点没人能踹它，因为所有 life 都靠 world 启动）。心跳是世界时钟的滴答（只叫它看一眼），不替它决定产啥。**原语边界**：10 分钟心跳和 debounce 只决定"何时醒 / 何时攒批触发"，绝不进入任何角色行为或世界内容的决策；debounce 的 `max_buffer` 只是防积压溢出的安全阀，不得被包装成世界 / 角色的决策依据。"省"省在 world 醒后克制少产 event，不在它少醒。

3. **转译分两层，是信息差和不越界的结构保证。** world 做"客观事实 → 各接收位置的客观可感形态"（感官投影，依据它独有的全局客观信息），life 做"客观形态 → 主观意义与情绪"（解读）。world 绝不碰情绪。

4. **event 契约：机制层硬定、内容层留 LLM。** 硬定（是代码协议，不能含糊成"LLM 会判断"）："在场 / 被波及" = event 所锚定房间的当前在场集合（world 维护的房间级位置）；life 一轮的输入 = 她自己的主观快照 + 她自己信箱里的未读 event，**绝不读 world 全局快照**（信息差命门——全局真相一旦漏进某 life 上下文，她就全知了）；标已读 = 只标本轮实际读到的那批 event_id（防想一轮那几十秒里新进的 event 被误标已读、静默丢、绕回卡死表象）。留 LLM（宪法，不用代码阈值）：一件事"够不够格成为 event"、"谁该感知到"，由 world 的 LLM 判断。

5. **防淹没三道闸，不用阈值 / 计数器替决策。** 产生侧在场过滤（不投不在场的人）+ world 克制（大部分心跳不产 event）+ debounce 攒批唤醒（框架现成原语，语义见 decision 2 的原语边界）。

6. **clean-slate，不留兼容。** 旧 life / glimpse / proactive / schedule 生成都是行为参考，重写后删，无 re-export、无降级。

## Caller coverage（删 / 换的影响面，详见 facts 文档，动手前再 grep 实证一遍）

- **某姐妹的当前状态被读**：`chat/agent_stream` → `memory/context._build_life_state`、`memory/voice`、`memory/reviewer/heavy` 读 `current_state` / `response_mood` / `activity_type`。→ 改读新主观快照；旧 `find_latest_life_state` + `life_engine_state` 删。
- **日程读取**：`memory/context`（`build_schedule_section` → `get_current_schedule`）、`memory/voice`。→ 日程不再生成，无日程就不拼该段。
- **日程写隐藏入口**：主对话 `ALL_TOOLS` 的 `update_schedule`（emit `ScheduleRevisionCreated`）、admin schedule 路由。→ 摘除工具、停路由。
- **删除影响面比 `life_dataflow` 广**：glimpse / proactive 还散在 admin、chat context、quick_search、persona routing、工具 / 配置语义里（`LifeStateChanged` 仅 `glimpse_event_node` 消费）。删前必须全仓 grep，且分清"专属 glimpse / proactive 的删"与"别处也在用的通用 synthetic trigger 语义的保留"，别一删带崩通用机制。
- **voice + light/heavy reviewer 的 cron 也在 `life_dataflow` wiring 里但保留**：只把它们读状态 / 日程的口按新快照适配。删除精确到具体 wire / cron，不按文件名整删。

## Data & deployment impact

- **新增 Data**：`WorldState`（客观世界快照，含房间级在场，as_latest + Version）、`LifeState`（三姐妹各自主观快照，as_latest，对外提供 `current_state` / `response_mood` / `activity_type` + 时间）、环境感知与意图两类 event（transient 敲门信号）、durable 信箱（含已读标记）。形态为后续直连 / 痕迹预留扩展。
- **lane 隔离覆盖所有 durable 持久化状态**：`WorldState`、`LifeState`、信箱的 Key 都必须带 lane——runtime 持久化不会自动把 lane 加进 key，不显式加，coe / ppe 泳道就会覆盖 prod 的"她此刻状态"和未读事件（比 `feedback_ppe_lane_cron_pollution` 那条只读污染更重，是写脏线上客观真相）。
- **删除表**：`life_engine_state`、`glimpse_state`、schedule 生成相关表（均 SQLAlchemy Base，无别的服务依赖）。建 / 删表走 `ops-db submit`。
- **`emit_delayed` 约束**：`delay_ms` 上限约 24 天（分钟级无影响）；换 Data / schema 后在途旧 envelope 运行时丢弃，部署切换时在途唤醒会丢，可接受。
- **部署影响**：world + 三 life 是常驻 agent，部署杀 Pod 会中断在途唤醒；部署会停掉主动说话（glimpse）和日程生成（本就要删）。删 glimpse 同步关 `settings.glimpse_target_chat_ids`。一镜像多服务：agent-service 部署后按铁律确认 vectorize-worker 同步（本刀不动其逻辑）。

## Tasks

1. **event 流转骨架。** 目标：把 event 从产生 → 投递 → life 消化 → 回灌的管道立起来，支持环境感知、意图、外部消息（用户对话）回灌三种来源，数据形态为后续直连 / 痕迹预留扩展。产出：event 的数据形态、durable 信箱 + transient 敲门信号的攒批唤醒、意图回灌 world 的边、用户对话作为 external event 进对应 life 信箱的边。验证：一条 event 能流过完整闭环；多条积压能攒批成一次唤醒（不是来一条醒一次）；一条意图回灌能唤醒 world；用户聊完一次，对应姐妹信箱多一条"刚聊过"的 event；某 life 想一轮期间新进的 event 不被误标已读、不丢。

2. **world engine。** 目标：world 作为发动机被三源唤醒、维护客观世界、懂这家作息节律、克制地产出客观 event。产出：world 节点（保底 10 分钟心跳 + 自排提前卡点 + life 回灌三源唤醒）、客观世界快照（含房间级在场、lane 隔离）、这家作息节律的底子、LLM 推演产 event、按 event 锚定房间的在场集合投递、客观感官投影。验证：world 被心跳唤醒能读快照、产 event、自排下次醒；最长不睡过 10 分钟；只投给 event 所锚房间的在场者；投出的是客观可感形态、不含情绪；大部分心跳克制不产 event。

3. **life engine（三姐妹）。** 目标：三姐妹同构、纯被动、event 驱动、不卡死、信息差成立。产出：三姐妹各自的 life（被 event 攒批唤醒、读自己主观快照 + 自己信箱未读、LLM 想一轮做主观解读、更新快照、emit 意图、能消化外部消息 event）；无 `state_end_at`、不自排闹钟；主观快照对外提供 `current_state` / `response_mood` / `activity_type`；一轮输入绝不含 world 全局快照。验证：大状态进行中来一件指向她的 event 能把她推醒重想、不干等到原结束；有客观边界的事结束由 world 推、没边界的靠下个 event 打断；三姐妹同屋能靠在场说话互相叫醒 / 搭话（纠偏可跑）；某 life 读不到她不在场的事；新主观快照能被读到最新。

4. **取用端切换 + 删旧活法。** 目标：把读 life 状态的地方切到新主观快照，删掉旧的活法。产出：chat / memory / voice / reviewer 读 life 状态改读新主观快照、日程缺失优雅处理；删旧 life engine / glimpse / proactive / schedule 生成及相关 event / 表 / 配置；摘除 `update_schedule` 工具、停 admin schedule 路由；保留 voice / reviewer 的 cron 和别处在用的通用 synthetic trigger 语义（精确删 wire、不按文件整删）。验证：主对话与整点语音生成时拿到她的新状态、无日程不报错；全仓 grep 旧专属符号零残留、通用机制未被误删；voice / reviewer 心跳仍在；服务启动无 wiring / graph 报错；现有未删测试通过。

5. **lane 隔离 + coe 跑通一天。** 目标：world / life / 信箱都不污染 prod，在独立泳道把整个世界跑起来验一天。产出：`WorldState` / `LifeState` / 信箱标识全部 lane 感知；coe 泳道部署 world + 三 life + chat、绑 dev bot、复刻必要 schema + 种子（三姐妹 persona 等）到 chiwei-test。验证：coe 世界与 prod 隔离（不抢任何快照 / 信箱）；dev bot 能触发对话、世界能转；一天的关键机制可观测——不卡死（大状态被 event 打断）、信息差（某姐妹不知道不在场的事）、纠偏（一个反常另一个会动）、平淡不淹（大部分心跳不产 event）。

> Task 1 是 2、3 的底座；2 与 3 通过 event 闭环深度耦合，需联调（不是干净并行）；4 依赖 2、3 完成；5 依赖全部。实现阶段先按此依赖串行推进，动手前再 map 一次真实文件分区，能并行的局部再并行。
