# 修复 lane 跨服务传递，解锁 channel-server 泳道部署

## Problem

lane 由 channel-server 根据 `lane_routing` 绑定产生，但发 MQ 时只编码进队列名和消息 body，**没有写进 AMQP header**。下游 agent-service 的 `extract_context` 只读 header，读不到就 `lane=None`，导致处理泳道消息时所有出站 HTTP 不带 `x-ctx-lane`、被 sidecar 打到 prod。泳道队列本有 10s TTL + DLX 降级回 prod 的设计（意图是"下游没部泳道就让 prod 接手"），但降级后 prod 服务不知道消息属于哪个泳道，泳道性彻底丢失。结果只能靠全链路 pod 的 `env LANE` 相同来掩盖——这就是"必须上下游一起部署"。

另一半问题是 channel-server 泳道会产生全局副作用：飞书 WS 长连接开关 `LARK_DIRECT_INGRESS` 挂在 app 级 env、所有泳道继承，而飞书对共享 app_id 随机投递，泳道一起来就静默分走线上消息；`initializeCrontabs()` 也无 lane gate，泳道会照跑 daily-photo（硬编码真实群 id）和 emoji-sync（全量覆写 `lark_emoji`）。

## Goal

- lane 随消息在 AMQP header 中跨服务传递，header 是唯一权威来源。
- prod agent-service 收到从泳道队列降级回来的消息时，能带着原 lane 继续处理，出站正确路由。
- 只部署 channel-server 一个 Deployment 到泳道，即可完成飞书链路验证，不需要同步部署 agent-service 和两个 worker。
- channel-server 部署到泳道不再产生任何全局副作用：不抢飞书长连接、不跑 cron。

## Non-goals

- 不拆 lark-service（阶段二，另出 spec）。
- 不动 QQ 渠道的结构。
- 不统一 `ctx:lane` 与 `lane` 两套 AsyncLocalStorage store key。`context-propagation.ts` 存 `ctx:lane`、`bot-context.ts` 存 `lane`、laneRouter 只读 `lane`，两套并存且 `getContextHeaders()` 无生产调用方。改它要动 ts-shared 公共中间件并波及 monitor-dashboard，本次不碰。
- 不给 laneRouter 加 env LANE 兜底。见决策三。
- 不修 paas-engine 的 `POST /api/paas/releases/` envs replace 语义。它与 `PUT` 的 merge 语义不一致、且无测试覆盖，是真实隐患（见决策五），但影响所有 app 的部署行为，应单独立项。
- 不动 `runChannelInitializers` 的 `NEED_INIT` gate（是 env gate 不是 lane gate，同类隐患；当前该变量未配置，泳道不会触发）。
- 不改 cron/interval source 的 `lane=None`（`engine.py:486/540`）。实现过程中确认了它的实际后果比预想的大：泳道 pod 上所有 cron 驱动的 durable publish 和 `emit_delayed` 都会投进 **prod 队列**并带 prod header——header 与队列一致，所以不是本次修的那类 bug，但整个工作逃出了泳道。尤其 `DELAYED_TRIGGER_ROUTES` 特意设了 `lane_fallback=False` 来防"泳道信封溢出到 prod"，而这条路径压根没把信封放进泳道队列。改它会改变路由行为，需要单独立项。当前非 prod 泳道默认关闭 time source，所以暂无实际影响。
- 不清理 `outbox_dispatcher.py` 的 `_Bound` 重复实现。与本缺陷无关，属于顺手扩 scope。

## Key design decisions

**一、lane 注入放在 `publish` 内部，不改调用点。** `rabbitmq.ts` 的 `publish` 已经算好了 `effectiveLane`，在组装 header 处无条件写入即可，三个调用点一行都不用改，未来新增调用点自动正确。选它而非"逐个调用点显式传 header"，因为后者正是当前 bug 的成因——漏一处就静默降级，没有任何报错。

**二、header key 对齐 Python 既有约定：`lane` 和 `trace_id`。** agent-service 的 `propagation.py` 已经用这两个 key 收发了大半年（durable / debounce / sink_dispatch 六条路径），TS 侧必须复用同一套。空值写空字符串而不是省略 key，与 `inject_context` 现有行为一致。

**三、消费侧 header-only，不读 body.lane，更不回落 env LANE。** 两个理由。其一，Python 的 `_coerce` 把"header 明确写空"和"header 缺失"都归一成 `None`，无法区分，回落 body 会错误复活已被上游判定为 prod 的消息。其二，env 兜底会毁掉本次要修的核心能力——prod agent-service 消费 prod 队列时，收到的既可能是真 prod 消息、也可能是从泳道队列降级回来的泳道消息，用 env 兜底会把后者错判成 prod。部署窗口内的在途旧消息（无 header）按 lane 缺失处理，即当 prod 走，这是可接受的降级而非错误。env LANE 只用于没有上游的自发工作负载：worker 决定消费哪个队列、启动期拓扑声明、lane gate 判定。

**四、TS 侧的 MQ header 读取收敛成一个模块，写入留在 `publish` 内。** 起初判断不需要抽（注入一处、消费两处），实现后两个 worker 各自出现了一份同样的 header 解析逻辑，违反单一定义，于是收敛进 `amqp-context.ts`。它只管"从入站 AMQP 消息读出 lane / trace_id"，是 `publish` 写入侧的对侧，也是 `propagation.py` 跨语言约定的 TS 消费端。**不放进 `rabbitmq.ts`**——那个模块被多个测试文件用 bun 的 `mock.module` 全局 mock（进程级、`mock.restore()` 不撤销），桩里缺新导出会让跨文件测试炸成 `not a function`。同理 `lane-policy.ts` 也独立成模块、直接读 `process.env.LANE` 而不复用 `getLane()`。

**五、全局副作用的 lane gate 写在代码里，不靠环境变量。** 原方案是把 `LARK_DIRECT_INGRESS` 从 App envs 下沉到 prod Release envs，已验证**不可行**：`make deploy` 走 `POST /api/paas/releases/`，body 不带 `envs`，而 `release_service.go:146` 是无条件 `existing.Envs = req.Envs`，于是下一次例行发版就会静默清空 Release envs、prod 飞书 WS 入站整个挂掉且无告警。改为在代码里按 lane 判定：非 prod lane 一律不启动飞书长连接、不启动 crontab。这与 `startInboundLaneConsumer` 已有的 `if (lane)` gate 同构，也与 Python 侧 `lane_policy.time_sources_enabled_by_default`（prod 才默认开 cron）对齐。`LARK_DIRECT_INGRESS` 保留在 App envs 不动，语义收窄为"prod 是否用长连接"的回退开关，与 lane gate 是与关系。

**六、生产侧 header 与队列必须同源，为此新增 Python 原语 `outbound_context()`。** 原实现里 `inject_context` 默认从 contextvar 读 lane，而 `mq.publish` 用带 env 兜底的 `current_lane()` 决定队列。泳道 pod 上 contextvar 为空时，消息会进 `xxx_ppe-x` 队列却带 `lane: ""` 的 header——在 header-only 的消费口径下正好重现本次要修的 bug。`outbound_context()` 把 lane 解析一次（contextvar → 显式 fallback → `current_lane()`），同一个值同时喂给 header 和队列，物理上无法漂移。**env 兜底只存在于这个生产侧原语里，消费侧仍是 header-only**，两者不矛盾：进程要往哪发由自己的 lane 决定，而收到什么消息不由自己决定。

## Caller coverage

**TS publish（3 处，全部不传 lane header）** —— 改 `publish` 内部即可覆盖全部：

| 调用点 | 现状 | 改后 |
|---|---|---|
| `plugins/lark/events/handlers.ts:286` | headers 传 `undefined` | 自动带 lane/trace header |
| `plugins/qq/events/handlers.ts:183` | headers 传 `undefined` | 同上 |
| `workers/recall-worker.ts:85` | 只传 `x-retry-count` | 保留 retry，追加 lane/trace |

`PROACTIVE_EVAL` 是死 route，全仓无 publish 调用，不涉及。

**TS consume（3 处，lane 均来自 body）**：

| 调用点 | 现状 | 改后 |
|---|---|---|
| `workers/chat-response-handler.ts:137` | `createContext(botName, undefined, payload.lane)` | 改读 header。Python `sink_dispatch.py:41-50` 早已注入 header，其第 9-11 行注释明确在等这个改动 |
| `workers/recall-worker.ts:123` | 同上 | 同上 |
| `infrastructure/integrations/inbound-lane-consumer.ts:76-77` | 从信封 body 读 `e.lane` | **不改**。信封是 channel-server 内部的跨 lane 投递格式，body 就是权威，不存在降级回 prod 的语义 |

**Python consume**：`runtime/engine.py:678-682` 经实测**本来就是对的**——`extract_context` 读了两个字段，补 trace_id 时原样传递 `lane=ctx.lane`，`bind_context` 两个 contextvar 都设。用变异测试确认过（人为把 lane 置 None 后新测试转红）。所以入站缺口纯粹在上游不注入 header，engine.py 未改，只补了覆盖缺口的测试（原测试只发过 `lane: ""`，一个丢掉 lane 的消费者也能保持绿）。`durable.py` / `debounce.py` / `delayed_trigger.py` 已正确 extract+bind，不改。

**Python publish**：`runtime/emit.py` 的 `_mq_publish_for_source` 完全没有 `inject_context`，是本缺陷在 Python 侧的对称形态（`emit()` → `Source.mq` 跨进程时 lane 同样丢）。另有四处（`sink_dispatch` 的 else 分支、`debounce` 两处、`review_queue`）虽然注了 header，但 header 走 contextvar、队列走 `current_lane()`，泳道 pod 无 context 时两者漂移——一并改用 `outbound_context()`。**队列选择逐值不变**，只改 header，无路由影响。`durable.py` 的三处（publish_durable、retry republish、emit_delayed）本来就把同一个 lane 值同时给 header 和队列，不改。`nodes/dlq_admin.py` 原封透传入站 header 但 lane 参数取 `current_lane()`，有同样的漂移形态，但它在 `nodes/` 下且信封契约需要单独审视，不在本次范围。

**启动期全局副作用（`startup/application.ts`）**：`initializeCrontabs()` 无 lane gate，需加；`startChannelDirectIngresses` 现有 gate 只看 env，需追加 lane 条件。同文件的 `startInboundLaneConsumer`（`if (lane)`）和 `declareTopology`（按 lane 声明队列）已正确，不改。`multiBotManager.initialize()` 启动即 upsert `common_user` 并回写 `bot_config`，是幂等写、泳道跑起来本就需要 bot 身份，不改。

**下游受害端（不需要改）**：tool-service 和 sandbox-worker 全目录 `lane` 零命中，纯靠 sidecar 按 header 分流，header 没了就落 prod。

## Data & deployment impact

- **无 DB schema 变更，无新表，无 prompt 变更，无 PaaS 配置变更。**
- **消息格式向后兼容**：header 是新增字段，body 的 `lane` 字段保留不变（仍被 agent-service 用于回填 `chat_response.lane`），只是消费侧不再读它判 lane。旧消费者忽略未知 header。**部署顺序无约束**，两侧可独立上线。部署窗口内的在途旧消息会按 lane 缺失处理（当 prod 走），不会失败。
- **DLX 降级保留 header**：RabbitMQ 的 dead-letter 只改路由元数据并追加 `x-death`，不删自定义 header，所以 10s TTL 降级回 prod 的消息仍带 lane。这是本次核心能力的前提，必须在真实 broker 上验收，不能只靠单测。
- **触发跨服务部署**：channel-server 及其两个同镜像 Deployment（recall-worker、chat-response-worker）prod 必须同步 release；agent-service 独立部署。
- **部署会中断在途后台任务**：agent-service 部署会杀掉正在跑的 rebuild / afterthought / world / life。部署前需确认无正在运行的任务。
- **`make deploy` 会连带部署 sibling**：Makefile 的 sibling 循环会把两个 worker 一起部到同一泳道。Task E 要求泳道只跑 channel-server 一个 Deployment，需绕开该循环或事后清理。

## Tasks

A、B、C 三个任务文件不重叠，可并行。D 与 A/B/C 无文件冲突也可并行。**E 依赖 A–D 全部完成**，且是受控的线上验证，不可并行。

**Task A — publish 侧注入 lane 与 trace_id**
- **Goal**: channel-server 发出的每条 MQ 消息，AMQP header 都携带 lane 和 trace_id。
- **Deliverable**: `apps/channel-server/src/infrastructure/integrations/rabbitmq.ts` 的 `publish`；配套单测。
- **Verification**: 单测断言三种场景（泳道、prod、显式传 lane）下 amqplib 收到的 header 内容正确，且原有 `x-retry-count` 等自定义 header 不被覆盖；`bun test` 全绿。

**Task B — 两个 worker 消费侧改读 header**
- **Goal**: chat-response-worker 和 recall-worker 从 header 恢复 lane，使 prod worker 能正确处理降级回来的泳道消息。
- **Deliverable**: `apps/channel-server/src/workers/chat-response-handler.ts`、`workers/recall-worker.ts`；配套单测。
- **Verification**: 单测覆盖三种入站消息（header 有 lane、header 为空、无 header），断言 handler 内取到的 lane 正确且不再读 body 字段；`bun test` 全绿。不改 `inbound-lane-consumer.ts`。

**Task C — agent-service 生产侧 header 与队列同源，消费侧补测试**
- **Goal**: `emit()` 等跨进程投递注入 header，且 header 里的 lane 与实际投递队列永远同源；MQSource 消费侧的 lane 恢复有测试掩护。
- **Deliverable**: `apps/agent-service/app/runtime/` 下的 `propagation.py`（新增生产侧原语）、`emit.py`、`sink_dispatch.py`、`debounce.py`、`review_queue.py`；`tests/runtime/` 补用例。
- **Verification**: 消费侧断言 header 带真实 lane 时 node 内 `lane_var` 等于该 lane（原用例只发过空字符串，一个丢 lane 的消费者也能保持绿）；生产侧对每个 publish 点断言"contextvar 为空 + env LANE 有值"这个组合下，header 的 lane 与 broker 实际看到的 routing key 可互相推导。`uv run pytest` 全绿，且队列选择逐值不变。

**Task D — 非 prod 泳道禁用全局副作用**
- **Goal**: channel-server 部署到任何非 prod 泳道时，不启动飞书 WS 长连接、不启动 crontab，因而不会抢占线上飞书事件、不会重复发群消息或覆写共享表。
- **Deliverable**: `apps/channel-server/src/startup/application.ts` 及飞书 ingress gate 所在模块；配套单测。
- **Verification**: 单测断言 lane 为 prod/空时两者都启动、lane 为任意泳道值时都不启动，且飞书长连接同时受原 env 开关约束（两者是与关系）。`bun test` 全绿。不引入任何新的环境变量或 PaaS 配置。

**Task E — 泳道端到端验证**
- **Goal**: 证明只部 channel-server 一个 Deployment 到泳道，飞书链路能完整跑通，且 lane 在降级路径上不丢。
- **Deliverable**: 验证记录（命令 + 实际输出）。
- **Verification**: 泳道只运行 channel-server 这一个 Deployment（不部 agent-service、不部两个 worker），绑定 dev bot 后发消息，确认：泳道 channel-server 收到并处理；chat_request 消息 header 带正确 lane；prod agent-service 接手降级消息且处理时 lane 正确（不是 None、不是 prod）；回复正常送达飞书。同时确认泳道 pod 日志中没有飞书长连接启动和 cron 调度记录。全程不部署任何其他服务的泳道。
