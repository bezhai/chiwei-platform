# Agent 基建设计：你们的 dataflow 框架已经是 agent fabric

> 状态：设计，未动代码，未定稿。
> 配套：`agent-runtime-self-build-research.md`（思考核心去 langchain 的依赖面调研，是本文档的"下层"附录）。
> 写作日期：2026-06-02。
> 一句话：自研的目标不是"造一个会过日子的 agent"，是**把基建搭好，让 world engine / life engine 这类自治 agent 在上层无痛长出来**。而调研发现：你们的 dataflow 框架其实**已经是**这套基建，缺的只有一个干净的"思考核心"和一撮约定。

---

## 0. 结论先放

1. **框架不缺原语。** "7×24 跑 / 定时心跳 / 事件驱动 / 持有可被读的状态快照 / 按 persona 扇出 / 容错重试 / 被审查"——这七样你们的 dataflow 框架现在全有，而且 `app/wiring/life_dataflow.py` 就是一个活生生的、正在生产跑的"自治 agent 集"。

2. **老 life engine 的毛病是设计，不是框架。** 它每分钟从日程重拍一个粗状态、中间没有连续的世界事件推动，所以会"卡在去上学/买冰淇淋的路上"。框架本身既支持定时心跳、也支持即时事件路径（`LifeStateChanged → glimpse_event_node` 就是），是上层没用起来。

3. **真正要做的"基建"只有两件**，比"做一个 agent 框架"窄得多：
   - **思考核心**（self-built，替掉 langchain）：让一个 agent"想一轮"这件事，变成 node 里一个干净、可组合、可观测的值。范围见调研报告。
   - **自治 agent 的蓝本约定**：把 `cron/interval 心跳 + 事件 wire + as_latest 快照 + 思考核心 + 动作 Data emit` 这套已经存在的拼法，固化成"定义一个自治 agent"的标准写法（可能加一个薄 helper，但**不加新框架原语**）。

4. **world / life engine 是上层 wiring，不是基建。** 基建对了，它们就是几十行 `wire()` + 几个 `@node`，跟 `life_dataflow.py` 现在的样子一模一样。这就是"无痛"。

5. **守住两条线**：状态流转不写进代码 if/else（赤尾宣言）；但投递/幂等/并发是硬的工程底线，不拿"她是活的"开脱。

下面用框架的真实原语逐条把话说死。

---

## 1. 核心洞察：dataflow 框架已经是 agent fabric

把"一个自治 agent 需要什么"逐项对到框架里**已经存在**的东西，全部带证据：

| 自治 agent 需要 | 框架里就是 | 证据 |
|---|---|---|
| 7×24 一直活着 | `Runtime.run()` 阻塞 + source loop + consumer loop 并发 | `app/workers/runtime_entry.py`、`app/runtime/engine.py` |
| 定时心跳（"该动一下了"） | `Source.cron(expr, tz)` / `Source.interval(seconds)`，每 tick emit 一个 Tick Data | `app/runtime/source.py:50-67`；`life_dataflow.py:64-69` 一排 cron |
| 感知世界（吃事件） | node 的入参就是 Data；事件经 wire 投递到 node | `app/runtime/node.py:77-83`（参数必是 Data 子类） |
| 即时反应外部变化 | 事件 Data 直接 wire 到反应节点 | `life_dataflow.py:86` `LifeStateChanged → glimpse_event_node` |
| 工具/动作把结果回灌成事件 | 工具 emit Data，durable wire 到消费者 | `life_dataflow.py:90` `ScheduleRevisionCreated → sync_life_state_node .durable()` |
| 持有状态、别人能读快照 | `wire(StateData).as_latest()` 自动持久化最新版；`select_latest` / `with_latest` 读 | `app/runtime/wire.py:105-107`、`131-133`；life 用 `find_latest_life_state` 读 |
| 按 persona 扇出、互不连坐 | `wire(Req).fan_out_per(_persona_dicts).to(node)` | `life_dataflow.py:79-85` |
| 思考核心 | `Agent(cfg, tools).run()/stream()` 直接在 node 里调 | `app/nodes/life_dataflow.py` life_tick_node 调 `Agent(...).run()` |
| 同步问一句等回答 | `emit_and_wait(req, wait_for=Reply)` | `app/runtime/emit_wait.py`（pre-safety 在用） |
| 容错：重试 / 死信 / 租约接管 | `.retry(n, backoff, lease_ms)` / `.on_error('dlq'|'manual-review'|...)` / inflight 租约 | `app/runtime/wire.py:135-222`、`app/runtime/inflight.py` |
| 并发去重 / 单飞 | `single_flight(key, ttl)`（Redis）+ inflight（框架侧） | `app/runtime/single_flight.py` |
| 延迟自调度（"N 分钟后再动"） | `emit_delayed` / `emit_at`，durable 版落 `DelayedTriggerEnvelope`、重启不丢 | `app/runtime/emit.py`（来自子 agent 调研，落地前再核一遍） |

读完这张表，结论很硬：**"agent fabric" 不需要从头造，它就是这个 dataflow 框架。** 现在的 `life_dataflow.py` 已经是一个用 cron 心跳 + 事件路径 + 按 persona 扇出 + agent 思考 + 快照状态拼出来的自治 agent 集——你想要的 world/life engine，是它的"想明白版"，不是另起炉灶。

---

## 2. 把你的愿景词汇翻译成框架原语

你在群里描述的那套，逐词落到原语上，证明它"无痛可表达"：

- **"world engine 7×24 跑、带定时器、直接跟外界交互（定时联网搜索）"** = 一个绑了 `Source.cron`/`Source.interval` 心跳、工具集里有"联网搜索/外部 I/O"的 agent node。它每个 tick（或被外部事件唤醒）调思考核心，把"世界发生了什么"emit 成一串 **世界事件 Data**。

- **"life engine 等 world engine 写 event 把她推进"** = life node 的入参是那些世界事件 Data（以及 @、群消息、todo 等其它事件 Data），wire 把它们投给 life node。

- **"对话时读 engine 当前快照"** = 聊天 node 用 `with_latest(LifeState)` / `select_latest(LifeState, persona)` 把 life engine 的最新快照拿来当 context，**不打断、不驱动** life engine。

- **"@她、群里聊的、她的 todo、外界因素、她在读小说，都当 event 丢给 life engine"** = 这些各自是一种 Data 类型，各自 `wire(它).to(life_react_node)`。来源可以是 MQ（用户消息）、cron（时间）、工具 emit（她自己产生的 todo）、world engine（外界/虚拟）。

- **"她能执行什么后台动作"** = life node `run` 完，emit 出 **动作 Data**（如 `SendProactiveMessage` / `UpdateTodo` / `StartActivity` / `ReactToGroupMsg`），各自 wire 到对应 handler。动作就是"她 emit 的一种 Data"。

- **"控制好状态的流转 = 异常时捕获、重新 fit 一个更好的流转态，捕获器本身也是 agent"** = 一个 **审查 node**：消费 life node 产出的"下一个状态"Data，自己用思考核心判断这个流转合不合理，合理就放行（emit 原状态给下游），不合理就 emit 一个"修正后的状态"。这就是 `wire(NextState).to(reviewer_node)`，reviewer 是个 agent。**这不需要新原语，是一段 wiring。**

一句话：你的世界观里每一个名词，框架里都有一个现成的对应物。**这就是"基建已经在了"的意思。**

---

## 3. 那"基建"到底要做什么（收窄到两件）

### 3.1 思考核心（self-built，替掉 langchain）
让一个 agent"想一轮"——吃 messages + 工具，跑 ReAct 循环，吐结果——变成 node 里一个干净、可组合、可观测的值。这部分的依赖面、provider、调用方、硬骨头，全在 `agent-runtime-self-build-research.md`。这里只补一个**与框架的接缝**要求：

> 思考核心的输出形态，要能自然地映射成"node emit 若干 Data"。

也就是说，agent 想一轮产出的不该只是"一段最终文本"，而是一串可被 node 逐个 emit 的**行为（act）**：一段要发的话、一个状态变更、一个动作请求、一段只进 trace 的私有思考。chat node 现在已经在手工干这件事（流式 token 按 `---split---` 切成多条 `ChatResponseSegment` emit）——思考核心要把这件事变成它的原生输出形态，而不是让每个 node 自己解析 token 流。这样 `说=emit SpeakSegment`、`变=emit StateChange`、`用=调工具`、`想=只进 trace`，全都顺着框架的 emit 走。

### 3.2 自治 agent 的蓝本约定
把 `life_dataflow.py` 已经示范的拼法，固化成"定义一个自治 agent"的标准三件套，让上层照抄即可：

1. **心跳**：`wire(XxxTick).from_(Source.cron/interval).to(fan_out_xxx)` —— 让它定时醒。
2. **感知**：`wire(各种事件 Data).to(xxx_react_node)` —— 让外部/内部事件能推它。
3. **状态 + 动作**：node 内调思考核心 → `wire(XxxState).as_latest()` 存快照、emit 动作 Data 给各自 handler。

这一层最多加一个**薄 helper**（比如一个 `define_agent(...)` 把这三件套的样板 wiring 收一下），**但绝不加新框架原语**。能不加 helper、直接照 `life_dataflow.py` 写，更好。

---

## 4. 用这套基建怎么"无痛"长出 world / life engine（wiring 草图）

纯示意，证明它就是上层几十行（真实命名/字段待 spec）：

```python
# ---- world engine：带心跳 + 外部 I/O 的 agent，产出世界事件 ----
wire(WorldTick).from_(Source.interval(60)).to(world_engine_node)
# world_engine_node 内：调思考核心（工具含 web_search 等外部 I/O），
# 读自己的世界快照，emit 一串 WorldEvent（"下课铃响""天气变了""群里有人提到你"）
wire(WorldState).as_latest()                       # 世界自己的快照
# WorldEvent 同时喂给相关 persona 的 life engine

# ---- life engine：消费世界事件 + 其它事件，演化快照，吐后台动作 ----
wire(WorldEvent).fan_out_per(_affected_personas).to(life_react_node)
wire(UserMentioned).to(life_react_node)            # @ / 群消息
wire(TodoChanged).to(life_react_node)              # 她自己的 todo
wire(LifeState).as_latest()                        # 她的快照，对话读它
# life_react_node 内：read_latest(LifeState) + 新事件 → 思考核心 → 新 LifeState + 动作 Data
wire(SendProactiveMessage).to(send_node).durable()
wire(StartActivity).to(activity_node)

# ---- 流转审查：异常捕获 + 重新 fit（reviewer 也是 agent）----
wire(LifeStateDraft).to(life_flow_reviewer_node)   # 先产 draft
# reviewer 觉得流转正常就 emit LifeState（落 as_latest），异常就 emit 修正后的 LifeState

# ---- 对话：只读快照，不驱动 life engine ----
# chat node: with_latest(LifeState) 把当前快照当 context；
# 同时把"刚跟你聊过"emit 成一个事件回灌 life engine
wire(ChatRequest).with_latest(LifeState).to(chat_node)
```

注意这里**没有一个 `if 状态==上学 then ...`**。状态怎么流转，全在 world_engine_node / life_react_node / reviewer_node 这几个 agent 的脑子里（context + prompt + 思考核心），代码里只有"谁的事件流到谁"。这正是赤尾宣言要的。

---

## 5. 反过度抽象：明确**不要**加的东西

我对子 agent 调研给的"框架缺口"清单做了复核，下面这些**不要做**，因为框架已用现成原语覆盖，加了就是 SDK 级过度抽象（违反"业务代码不是 SDK""别为未来造抽象"）：

- ❌ **不要造 `AgentState` 装饰器/基类** —— `wire(D).as_latest()` + `select_latest` 就是 agent 的 durable 快照状态，life_engine_state 现在就这么用。
- ❌ **不要造 `supervised_by()` 新原语** —— "审查节点否决/修正"就是 `wire(Draft).to(reviewer)` 一段 wiring（见 §4），reviewer emit 修正后的 Data 即可。
- ❌ **不要造熔断器 / circuit breaker / 自动降级原语** —— 没有真实需求驱动；真要降级在 `on_error="swallow_and_log"` 处自定义即可。
- ❌ **不要上 Raft / 全局分布式锁 coordinator** —— `single_flight`（Redis）+ inflight 租约已覆盖并发去重和 worker 死亡接管。
- ❌ **不要给 Agent 造"session 生命周期 hook"框架** —— 多轮状态就是 `as_latest` 快照 + 事件回灌，不需要常驻 session 对象。

判断准则就一条：**先问"这事能不能用现有 wire/Source/as_latest/emit 拼出来"，能就不加原语。** 几乎都能。

---

## 6. 真正的开放设计问题（这些才值得讨论，且都不是框架缺口）

1. **world engine 怎么产事件而不爆炸/不烧钱。** 它若是个 LLM narrator 每分钟模拟她一整天，成本和发散都不可控。要定的是：事件从哪来——日程展开（确定性骨架）+ 外部真实信号（联网搜索、群消息）+ 少量虚拟补全？心跳多密？一次产多少事件？**这是 world engine 设计的真核心，留待它自己的 spec。**

2. **状态要分层，解决"卡在大状态里"。** 快照至少两个时间尺度：慢的"背景/日程"（今天在上学、这周备考）+ 快的"此刻"（正在课间、刚被搭话、在买冰淇淋路上）。world engine 同时推两层；快层会被下一个事件自然顶掉，所以卡不住。`as_latest` 存一个结构化快照即可承载两层，不需要新原语。

3. **流转审查（异常 refit）怎么设计才不变成状态机。** reviewer 是 agent、判断"这个流转像不像人"，输出"放行 / 修正"。要小心别把它写成一堆"如果 X 状态不能跟 Y 状态"的规则——那又回到工程脑。它该是宽泛标准（"她是不是卡住了/前后矛盾/无视了外部请求"）交给模型判。参考 `feedback_self_feedback_needs_reviewer`：自反馈 LLM 系统必须有 reviewer 打断死循环——这正是它的价值。

4. **成本与延迟。** 多 agent（world + life + reviewer + chat）+ 丰富的"想"通道，烧 token 也烧时间。心跳频率、思考深浅要能按场景调；"想"通道按需开关，不默认全开。

5. **可观测性是必须项。** 她内心越复杂，越要在 trace 里看清"这个状态/这句话是从哪些事件、哪几步思考长出来的"。每个 act 一个 span、子 agent 挂父 trace——这是思考核心 trace 设计的硬要求（见调研报告硬骨头）。

---

## 7. 守住的线：工程底线 vs 她的不确定性

赤尾宣言说别用工程确定性消除她的不确定性——对，但那说的是**她的行为**，不是**基建的正确性**，两者必须分清，否则会拿哲学给 bug 开脱：

- **留给模型的不确定性**：她此刻是什么状态、要不要主动找你、用几个声音想、回不回这条群消息——代码不插手，靠 context/prompt/事件流。
- **必须硬的工程底线**：不重复发消息、不丢事件、重试不重放、幂等、并发安全、快照读到的是一致版本——这些是 dataflow 框架已经用 inflight/single_flight/as_latest 版本锁保证的，**不能因为"她是活的"就松**。

---

## 8. 下一步

1. 跟 bezhai 对齐本文档的核心判断（框架已是 fabric、基建=思考核心+约定、不加新原语）。
2. 先做**思考核心**（调研报告范围）：它是一切 agent 的"想"引擎，且要把输出做成"act → emit Data"的形态。这步可独立于 world/life engine 推进、独立 coe 验证。
3. 思考核心稳了之后，world/life engine 各自写 spec（按 §6 的开放问题），用 §4 的拼法落成上层 wiring。
4. 全程 coe 泳道真机演练，禁静态审计找 bug。

---

### 附：本设计挂靠的框架真实原语（已亲自核对）
- `app/runtime/node.py:56-108`：`@node` 契约（async、参数即 Data、返回 Data|None 自动 emit）。
- `app/runtime/source.py:25-72`：`Source.cron/interval/mq/http`。
- `app/runtime/wire.py:84-227`：`wire().to/from_/durable/as_latest/when/debounce/with_latest/retry/fan_out_per/on_error` 全套 DSL。
- `app/wiring/life_dataflow.py:1-91`：现存的自治 agent 集（cron 心跳 + 事件路径 + 按 persona 扇出 + durable 工具事件）——本设计的活样板。
- `app/runtime/emit.py` / `emit_wait.py` / `single_flight.py` / `inflight.py`：emit 分发、同步问答、单飞、消费幂等与租约（细节见子 agent 调研，落地前复核 emit_delayed）。
- 思考核心现状与去 langchain 依赖面：`agent-runtime-self-build-research.md`。
