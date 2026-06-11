# 让 world 的 sleep 真正定节奏 + sleep 动态上限 + agent 时间出口统一 CST

## Problem

world 现在每 10 分钟被固定心跳无条件拍醒。它在循环里调 `sleep(N)` 排了一个 N 秒后的自唤醒（self-wake），但 600s 的心跳总是先到、把它拍醒，排好的 self-wake 永远等不到自己触发。昨晚 coe 数据实锤：深夜 world 调了 `sleep(3600)` 想睡一小时，DB 里那一小时还是每 10 分钟一版 world_state——长睡意愿一秒没兑现。同时 sleep 上限是死的 3600s，不分昼夜、不看外部有没有动静。

还有一处地基坏了：系统时区不统一。world_time 存 CST、life 写的 intent occurred_at 存 UTC、真人聊天的 event occurred_at 存 Unix 毫秒、created_at 存 UTC。喂给 agent 的同一个 prompt 里 CST 和 UTC 的时刻混在一起，模型看到两个"现在"；只有 intents.py 一处用 `::timestamptz` cast 把比较兜住了，其他地方差 8 小时。

## Goal

world 的 sleep 真正决定下次醒的时间：她说睡多久就睡多久，期间有外部动静（三姐妹 intent / 真人飞书聊天）随时打断她去反应，固定心跳退回纯兜底——拍醒后发现没到她自己定的点就接着睡、什么都不做。sleep 的上限按世界作息动态收放：夜里（CST 23:00–06:00）最长能睡 7200s，白天最长 900s，但最近 30 分钟有过外部信号就一律压到 600s 保持响应，她这轮没调 sleep 就默认 600s；时段和这 30 分钟窗口都按醒着这刻的真实时间算。她选的 sleep 超过当前上限时不偷偷改成上限值，而是被拒绝、把上限和原因告诉她让她在护栏内重选。

喂给 agent 的所有时间统一显示成 CST（北京时间），所有时间比较按真实时刻归一（不再差 8 小时），存储层不动现有格式、只保证新写的时间都是带时区的 aware ISO。

## Non-goals

- 不迁移历史数据的时区、不动已落库时间的存储格式（append-only 事件会随时间滚出窗口；helper 向后兼容读旧的 Unix 毫秒 / UTC / CST）。
- 不把 self-wake 改成跨进程 durable（仍 in-process，部署杀 pod 后靠心跳冷启兜底——这正是保留心跳的理由）。
- 不改 world deliberate 的内容决策（move 谁、emit 什么、睡多久仍 agent 自主）；本次只动"何时醒、睡多久的上限护栏、时间怎么显示 / 比较"这些机制边界。
- 不为想象中的未来外部源搭抽象，当前外部信号就 intent + 真人聊天两个。

## Key design decisions

**1. self-wake 和心跳都走"到点检查"gate，持久化的"下次该醒时间"是唯一权威；只有外部信号（intent / 聊天）不受 gate。** 不简单调大心跳间隔——那会让冷启后 world 干等一个长周期才第一次醒、响应太慢。改成：心跳仍每 600s 拍一下，拍醒后先读"下次该醒时间"，没到点且唤醒原因是心跳或 self-wake 就直接返回、不 deliberate、不落快照、不推 world_time；到点了、或唤醒原因是 intent / 聊天才真 deliberate。**self-wake 也走这个 gate 是关键**：`emit_delayed` 排出的 self-wake 取消不掉，被外部信号提前唤醒并重排后，那条旧的 self-wake 到点仍会触发——让它也过 gate，旧的早到时 now 还没到新的"下次该醒时间"就被挡掉，一个权威同时解决"心跳让路"和"stale self-wake 判废"，不需要给 tick 另带 token。gate 放在 `renotify_unread`（补敲信箱的机械 IO 兜底）**之后**：每个心跳都先补敲、再判断要不要 deliberate，否则积压的信箱敲门会被拖到长睡结束才恢复。冷启没有"下次该醒时间" → 照常 deliberate（兜底第一脚）。

**2. "下次该醒时间"持久化进 WorldState。** 心跳 / self-wake 拍醒都要查它判到点没，跨重启也要查得到。WorldState 是 pydantic Data，加一个字段框架自动 add-column。它和 self-wake 的 `emit_delayed` 来自同一个 sleep 决定，是 gate 的唯一权威、也是判废 stale self-wake 的依据（决策 1）。

**3. sleep 动态上限是"别睡死"的护栏、超限拒绝让 agent 重选，不静默改值。** 上限按时段（夜 23:00–06:00 → 7200 / 白天 → 900）和外部信号窗口（最近 30min 有信号 → 600）取小，下限仍 60s，没调 sleep 默认 600s。她选的值超过上限时，沿用现状的拒绝机制（报错喂回模型，连同当前上限 + 原因，让她在护栏内重选）——不 clamp、不替她拍板；prompt 里也先把当前时段上限告诉她。时段判断和 30min 窗口都用醒着这刻的真实 now 转 CST，不用旧 `world_time`（加了 gate 后它只在真 deliberate 时推进、会滞后）。这不违赤尾原则：睡多久始终是她自己定，系统只按世界作息给个随昼夜和动静变的"最长别超过"护栏，和现在那条固定 3600s 上限同性质。

**4. 统一"外部信号"口径，intent + 真人聊天都能打断 world，也都作 30min 窗口输入。** 外部信号 = 非 world 自发、代表外部有动静要 world 保持响应的信号，当前是三姐妹 intent（已是 wake source、world 已能查）和真人飞书聊天（投在信箱 data_event_envelope、kind=external，world 现在既不被它唤醒、也不读它）。两件事都要补：① 让真人聊天经一道合并门唤醒 world（reason=external，不受 gate，和 intent 对称），打断她的长睡去反应；② 让 world 能在最近 30min 窗口里把 intent + 聊天一起查出来，作为 sleep 上限的输入。聊天的 occurred_at 现在是 Unix 毫秒，归一到决策 5。

**5. agent 时间出口统一 CST + 比较归一 + 存储 aware ISO，是上面几条的地基。** 一个时间 helper：只解析当前代码实际产生的那几种历史格式（带或不带 offset 的 aware ISO、Unix 毫秒），不做"任意表示"的万能兼容层；喂给 agent 时一律转 CST 显示、比较时一律按真实时刻。新写一律 aware ISO（含把聊天的 Unix 毫秒改成 aware ISO）。历史数据不迁移，由 helper 向后兼容读。时段判断（决策 3）和外部信号窗口（决策 4）都建在这个归一基准上，否则差 8 小时。

## Caller coverage

时间喂给 agent 的出口（都要过 CST helper）：
- `app/world/engine.py` world prompt 的「世界此刻」（world_time，现 CST）和 intent 批次时刻（occurred_at，现 UTC，与前者同框混着）
- `app/nodes/life_wake.py` life stimulus 里 event 时刻（EventEnvelope.occurred_at，现混乱）和 prompt_vars 的 current_time（现 UTC）

时间比较 / 窗口（都要按真实时刻归一）：
- `app/world/engine.py` `_intent_since_cutoff`（anchor UTC vs now CST，fallback 可能错）
- `app/data/queries/intents.py` 已用 `::timestamptz` cast（对，保留）；新增的最近外部信号窗口查询要同样归一

外部信号生产 / 消费 / 唤醒：
- intent：`app/nodes/life_tools.py` 写 / `app/data/queries/intents.py` world 读 / `app/world/engine.py` + `app/wiring/life_dataflow.py` 经合并门唤醒 world（已通）
- 真人聊天：`app/nodes/chat_node.py` `_replay_conversation_to_mailbox` 写 EXTERNAL event（occurred_at 现 Unix 毫秒，要改 aware ISO）→ `app/nodes/life_wake.py` `_format_unread` 读（life 侧）→ **world 侧新增：经合并门唤醒 world（决策 4）+ 在 30min 窗口读 data_event_envelope**

新写时间的地方（保证 aware ISO）：
- `app/world/tools.py`（world event，现 CST aware，合规）、`app/nodes/life_wake.py`（intent / life state，现 UTC aware，合规）、`app/nodes/chat_node.py`（EXTERNAL event，现 Unix 毫秒，要改）

world 调度入口：
- `app/world/engine.py` `_run_world_round`（renotify 之后加到点 gate，覆盖 heartbeat + self）、收口处（写"下次该醒时间"）、`app/world/tools.py` `sleep`（上限改动态、超限拒绝重选）

## Data & deployment impact

- WorldState 加「下次该醒时间」字段：pydantic Data 框架自动 add-column，coe 启动自动建；prod 上线前确认 migration 行为。
- EventEnvelope.occurred_at 写入格式从 Unix 毫秒改 aware ISO：影响写入方（chat_node）和读取方（life `_format_unread` + 新增 world 读）。历史已存的 Unix 毫秒由 helper 向后兼容解析，不迁移。
- 真人聊天新增一条唤醒 world 的边（合并门 + transient tick），只加 wiring、不改 schema；world 新读 data_event_envelope（只读）。
- Langfuse：life_wake 模板的 `{{current_time}}` 变量名不变、只是注入值变 CST，模板不用改；world / life 的时间都是 stimulus 注入不是模板字面，不用改 langfuse。实现时确认 `{{current_time}}` 仍在用。
- 部署 agent-service 必须同步 release vectorize-worker（一镜像多服务）。部署杀 pod 会丢 in-process self-wake，冷启靠心跳第一拍兜底（设计已含）。

## Tasks

**Task 1 — 统一时间基准（地基）。** 目标：喂给 agent 的所有时间显示为 CST、所有时间比较按真实时刻、所有新写时间为 aware ISO。产出：一个时间 helper（只解析当前实际产生的几种历史格式 → aware → CST 显示 / 真实时刻比较，不做万能解析）+ 上面 caller coverage 里所有出口 / 比较 / 写入点接入。验收：在 trace 里看 world / life 的 prompt，所有时间都是 CST 且是同一个"现在"（不再 CST/UTC 混框）；构造跨 UTC intent / Unix 毫秒 chat / CST world_time 的数据，时间窗口比较不差 8 小时（单测）。

**Task 2 — 统一外部信号口径：world 既被聊天打断、也能查到聊天。** 目标：真人聊天能打断 world 长睡，且 world 能在最近 N 分钟窗口里查到所有外部信号（intent + 聊天）。产出：① 真人聊天经一道合并门唤醒 world（reason=external，不受到点 gate，和 intent 对称）；② 一个"最近外部信号"查询（跨 data_intent_raised 与 data_event_envelope 的 external 事件、按真实时刻过滤），world 侧接入。依赖 Task 1 的时间归一。验收：构造 intent 与 chat external 各一条落在窗口内、各一条落在窗口外，查询只返回窗口内两者（单测）；coe 真发一条飞书消息，world 侧被唤醒一轮（打断当前睡眠）、且能在 30min 窗口查到这条聊天信号。

**Task 3 — world 自调度 + sleep 动态上限。** 目标：world 的 sleep 真正决定下次醒，心跳 / self-wake 退回到点检查兜底，sleep 上限按时段 + 外部信号动态、超限拒绝重选。产出：WorldState 加「下次该醒时间」并在收口写入；`_run_world_round` 在 renotify 之后加到点 gate（以"下次该醒时间"为唯一权威，覆盖 heartbeat + self；intent / external 不受 gate）；`sleep` 上限改成按真实 now 的时段（夜 7200 / 日 900）与最近 30min 外部信号（有则 600）取小，超限沿用拒绝机制把上限 + 原因喂回让她重选，没调 sleep 默认 600。依赖 Task 1（时段判断 + 时间比较）+ Task 2（外部信号窗口）。验收：单测覆盖——夜间 `sleep(7200)` 写出 now+7200 的下次醒时间、白天 `sleep` 超 900 被拒绝并喂回当前上限、30min 内有外部信号上限降到 600、没调 sleep 默认 600、心跳 / self 在没到点时被 gate 挡掉不 deliberate、intent / external 唤醒不被 gate 挡、被重排后到点的旧 self-wake 被判废；coe 实跑一段，DB world_state 相邻版本间隔随时段和外部信号变化（夜里拉长到接近 2h、有聊天 / intent 后缩回 ≤10min），不再雷打不动每 10 分钟一版。
