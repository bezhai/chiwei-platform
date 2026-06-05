# 赤尾自主化重设计 · 阶段 0 地基实现 spec

落地 [chiwei-world-life-autonomy-redesign.md](chiwei-world-life-autonomy-redesign.md) 的阶段 0:不动范式、风险最低的两块地基——CST 时间统一 + life 上下文三层归位。CST 细节沿用已 codex T1 过的 [chiwei-world-self-schedule-cst-time-spec.md](chiwei-world-self-schedule-cst-time-spec.md) 的 Task 1，本 spec 只补它没固化、且经代码核实后才浮现的真实改动面与设计决策。

## Problem

两个地基坏了。**时间不统一**:喂给同一个 agent 的 prompt 里 CST 和 UTC 混在一起,模型看到两个"现在"——world 的 `world_time` 存 CST，life 写的 intent `occurred_at` 存 UTC，真人聊天 event `occurred_at` 存 Unix 毫秒，`core.py` 全局给每个 prompt 注入的 `currTime` 是 naive 系统时间。**上下文层次错位**:life 把"几点 / 上一刻状态"这些每轮都变的动态值塞进 langfuse 模板（=进 system prompt），既废掉前缀缓存复用、又语义错位——system prompt 本该是纯静态身份。

## Goal

喂给 agent 的所有时间都显示成 CST(北京时间)、所有时间比较按真实时刻归一(不再差 8 小时)、所有新写时间是带时区的 aware ISO。life 的 system prompt 收敛成纯静态身份(只剩 `persona_name` / `persona_lite`)；她"几点了"和"信箱里的新动静"每轮现拼进当前 USER message；她"上一刻什么状态"正常情况下从当天连续意识流(transcript)里自然延续、不再每轮从 PG 现捞，只有意识流断了(冷启 / Redis 丢)时才从 PG 的 LifeState 兜底恢复、作"醒来记得之前在做什么"喂进当前 USER。world 侧本已基本达标，只把它 USER 里混着的 UTC intent 时刻显示成 CST。

## Non-goals

- 不动范式:不删 `move_persona` / `raise_intent`、不改唤醒语义、不改 world 的自调度——那些是阶段 1。
- 不动 world 的指令结构:`world_loop_instruction()` 在每轮 USER 重复是冗余，但把它上提到 system 属阶段 1A，本次不碰。
- 不改 chat / main 模板的 prompt 结构:`core.py` 的 `currTime` 只修时区(naive→CST)、不挪位置——把 `currTime` 移出 main 的 system 是另一条链路的大改，不在本次。**已知技术债(codex 建议)**:chat / main 的 system prompt 仍含 `currTime` 这个动态 var、前缀缓存复用问题阶段 0 不解决，留作后续。
- 不迁移历史数据:已落库的 UTC / Unix 毫秒时间不改存储，由 helper 向后兼容读。
- 不做万能时间兼容层:helper 只解析当前代码实际产生的那几种格式。

## Key design decisions

**1. 一个时间 helper，只解析当前代码实际产生的格式，不做万能兼容。** 当前产生三种:带 offset 的 aware ISO(world 写 CST、life 写 UTC)、Unix 毫秒字符串(chat 写)。helper 负责把这三种解析成 aware datetime(归一真实时刻供比较)、把 aware datetime / 原始串显示成 CST、产出当前 CST aware ISO 供新写。选"只认实际格式"而非"任意表示万能解析"，因为后者是为想象的输入过度设计、且更易把脏数据静默解析错。

**2. CST 统一抓两类出口分别处理:全局注入点改时区、各模块自注入点改时区 + 历史值显示兜底。** `core.py` 的 `currTime`/`currDate` 是所有 Agent.run 的全局注入点，naive→显式 CST 一处修正整条 chat 线。life / voice 各自注入的 `current_time`、world USER 里的 intent `occurred_at`、life `_format_unread` 里的 event `occurred_at`——这些显示出口过 helper 转 CST(后两者还要兜历史 UTC / Unix 毫秒)。

**3. 新写时间一律 CST aware ISO，连带修正下游比较。** life 的 `observed_at` 从 UTC 改 CST aware（intent `occurred_at` 是它的 pass-through，一并归正），chat 回灌 event 的 `occurred_at` 从 Unix 毫秒改 CST aware ISO。改完后 world 的 `_intent_since_cutoff` 锚点(life intent)与 `now` 同为 CST，naive fallback 分支保留只为兜历史脏数据。

**4. life 的 system prompt 收敛成纯静态身份，动态全出。** langfuse `life_wake` 模板删掉 `current_time` / `prev_state` / `prev_mood` / `prev_activity` 四个动态变量，只留 `persona_name` / `persona_lite`；代码侧 `prompt_vars` 同步只剩这两个。选纯静态是因为动态值进 system 会让前缀缓存每轮失效、且把"会变的东西"钉死在本该恒定的身份层是语义错位。

**5. 上一刻状态靠意识流延续，LifeState 只在意识流断了时兜底——不是无脑挪位置。** 正常每轮 USER 只放当轮新感知(几点 CST + 信箱动静)，不放 prev_state——她的状态在当天连续 transcript 里本就有、每轮再塞是冗余。只有 transcript 为空(冷启 / Redis 24h 过期丢失 / 跨天新 session)时，才从 PG 的 LifeState 读出 current_state / mood / activity 作恢复段喂进当前 USER。LifeState(PG durable)因此从"每轮 prompt 的输入"退成"意识流断裂时的状态恢复源"。

**跨天先记得、不翻篇(bezhai 决策，列为优化项):** codex 提醒 session 按天切后每天第一轮 transcript 必空、无条件恢复会把昨晚最后状态当今早"上一刻"，与"像睡一觉翻篇"冲突。但当前进度只到地基(~10%)、还不到体验优化的时候，bezhai 拍板**先让她跨天也记得**——transcript 空就恢复、不判日期，实现最简、不丢状态。"跨天像睡一觉、昨天细节翻篇、要紧的事靠长期记忆"列为后续优化项(归到目标架构开放问题的"跨天 memory 沉淀")。所以冷启探测只判 transcript 空不空、不判 `observed_at` 是哪天;恢复段措辞诚实即可——"你上次记得在做……"。

**双读一致性(codex 必改):** life 节点要判 transcript 空不空，得自己 `load_session` 探一次;`Agent.run` 内部跑时还会再 load 一次。这正是 `world_tick` 已在用的模式(它在 `_run_world_round` 自己 `load_session` 做 turn 幂等、`Agent.run` 再 load)——直接复用、非新发明。两次读一致靠 life 已有的 `(lane, persona)` single_flight 锁:锁覆盖"探测 → run → 写回"整段、同 session 无并发写。以 life 节点那次探测决定要不要注入恢复段，`Agent.run` 那次只管拼 messages。

**6. 改 langfuse `life_wake` 模板走泳道 label，命中条件必须钉死(codex 必改)。** `life_wake` 当前只挂 `coe-world-life` / `coe-world-life2` label、本就没 production 版。代码按 `get_lane() or settings.lane` 找**同名 label**、找不到才 fallback——所以删 4 变量的新模板必须发到**等于本轮实际部署验证泳道名**的 label，否则跑该泳道时命中不到新模板:要么变量缺失渲染失败、要么继续命中旧模板把动态状态留在 system→验证失真(看着改了其实没生效)。实现时三步钉死:① 确认本轮 coe 验证泳道名 X;② 删 4 变量的新模板版本发到 `label=X`;③ 代码删 `prompt_vars` 4 变量的那个 commit 与模板 `label=X` 新版本在泳道 X **原子对齐**(同泳道里代码和模板同时是新的)。另确认旧 `coe-world-life` / `coe-world-life2` 没有别的还在跑的部署引用——有就别动旧 label、只发 X。改前后用 `prompt-vars` + `diff-prompt` 核对变量集合、grep 代码注入点确认契约一致。

## Caller coverage

时间显示出口(过 helper 转 CST):
- `app/agent/core.py:565-566` 全局 `currDate` / `currTime`(naive → CST)——影响所有走 Agent.run 的模板(chat / main 在用 `{{currTime}}`)
- `app/nodes/life_wake.py:192` `current_time`（UTC %H:%M → CST，并按决策 4/5 挪进 USER）
- `app/memory/voice.py:69` `current_time`（UTC %H:%M → CST）
- `app/world/engine.py` `_intent_batch_text`（intent `occurred_at` 显示，来自 life 历史 UTC → CST）
- `app/nodes/life_wake.py:105-113` `_format_unread`（event `occurred_at` 显示，来自 chat 历史 Unix 毫秒 / world CST → CST）

新写时间(改为 CST aware ISO):
- `app/nodes/life_wake.py:183-184` `observed_at`（UTC → CST；连带 `app/nodes/life_tools.py:126` intent `occurred_at` pass-through）
- `app/nodes/chat_node.py:333` 回灌 event `occurred_at`（Unix 毫秒 → CST aware ISO）

时间比较 / 窗口(按真实时刻归一):
- `app/world/engine.py:306-330` `_intent_since_cutoff`（决策 3 后锚点与 now 同 CST，naive fallback 留兜历史）
- `app/data/queries/intents.py:59-60` 已用 `::timestamptz` cast——保留，正确

上下文三层归位:
- `app/nodes/life_wake.py:185-231` `prompt_vars` 收敛 + stimulus 重拼 + 冷启探测注入
- langfuse `life_wake` 模板删 4 变量

不改(已达标):`app/world/engine.py:502` world_time（已 CST aware）；`app/world/tools.py:180` event `occurred_at`（已 CST aware）；world system prompt（langfuse `world_deliberate`，0 变量，已纯静态）。

## Data & deployment impact

- 无 schema 变更:LifeState / EventEnvelope 字段不变，只改写入值的格式。历史 UTC / Unix 毫秒数据不迁移，helper 向后兼容读。
- langfuse:`life_wake` 模板删 4 变量、发新泳道 label，代码 `prompt_vars` 同步原子发版（变量契约纪律）。`voice_generator` / main 模板不改字面、只是注入值变 CST，模板不动。
- 部署:agent-service 改动需同步 release vectorize-worker（一镜像多服务）。本次不动后台异步任务（rebuild / afterthought），但部署仍会杀 in-flight，照常确认。
- 验证泳道:coe（隔离 chiwei-test，不污染 prod）。

## Tasks

**Task 1 — 时间 helper + CST 统一接入。** 目标:喂给 agent 的所有时间显示 CST、比较归一真实时刻、新写为 CST aware ISO。产出:新建时间 helper（解析当前实际产生的三种格式 → aware / CST 显示 / now CST ISO）+ 上面 caller coverage 里所有显示出口、新写点、比较点接入。验收:单测覆盖 helper 三种历史格式解析 + 跨格式比较不差 8 小时；信箱 `list_unread_events` 的"按发生先后"排序在格式统一后正确(它当前按 raw TEXT `occurred_at` 排序，Unix 毫秒 / ISO 混排会乱——评估是否需 `::timestamptz` 归一，参照 `intents.py`，单测覆盖混格式排序)；在 coe trace 里看 world / life / chat 的 prompt，所有时间都是 CST 且是同一个"现在"，不再 CST/UTC/Unix 毫秒混框。

**Task 2 — life 上下文三层归位。** 目标:life 的 system prompt 纯静态身份，当轮新感知进 USER，上一刻状态靠意识流延续、仅冷启时从 LifeState 兜底恢复。产出:`life_wake` 节点的 `prompt_vars` 收敛成 `persona_name`/`persona_lite`；stimulus 重拼成当轮感知（几点 CST + 信箱动静）；冷启探测（transcript 空才注入 LifeState 状态恢复段）；langfuse `life_wake` 模板删 4 动态变量、发新泳道 label。依赖 Task 1（CST 显示）。验收:单测覆盖——`prompt_vars` 只剩两个静态变量、prev_state 不在 prompt_vars、当轮感知在 USER message；transcript 非空时 USER 不含状态恢复段、transcript 空时含恢复段(只判空、不判日期，决策 5);状态进 messages → 进 transcript → 第二轮可 replay。coe 实跑:同一角色当天多轮续接，trace 里 system prompt 逐轮字节一致（纯静态、前缀可复用），状态从意识流延续；手动清掉该 session 的 Redis key 模拟丢失，下一轮她从 LifeState 恢复状态、不彻底失忆。
