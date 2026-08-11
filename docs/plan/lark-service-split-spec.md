# 把飞书渠道拆成独立服务 lark-service

## Problem

channel-server 现在同时是三样东西：飞书渠道实现、QQ 渠道实现、common 层消息流水线。飞书部分（`plugins/lark/` 5692 行 + 散落在外约 2300 行）占绝大头，边界是破的——`infrastructure/integrations/lark*` 有 4 处反向 `import @plugins/lark`，`core/models/Message`（417 行）字段全是飞书形状（`chat_mode` / `permission_config` / `union_id`）却住在 core、QQ 一次没用过，crontab 两个飞书任务按"定时任务"归类。

代价是具体的：改飞书要重启处理 QQ 的进程；飞书 SDK 的依赖和故障面覆盖整个 common 层；`plugins/lark` 之外的飞书代码没人认领，边界守卫（`core/boundary.test.ts`）也抓不到，因为它们不 import 飞书 SDK、只是"长得像飞书"。

## Goal

- `apps/lark-service` 独立进程承载飞书渠道全闭环：webhook + WS 入站、飞书→common 投影、规则与指令、出站发送、撤回、飞书专属定时任务。
- 飞书代码在 channel-server 中零残留，`channel` 参数不再有 `'lark'` 兜底。
- channel-server 退化成"common 层 + QQ 渠道宿主"，职责单一可描述。
- 共享能力在 `packages/` 里有唯一定义，且**共享包不认识任何具体渠道**。
- 飞书链路行为不变，切换过程中不丢消息、不重复发送。

## Non-goals

- **不拆 QQ**。QQ 留在 channel-server，本次是过渡态不对称，接受。
- **不拆库**。lark_* 与 common_* 继续同一 PG 实例，两个跨表原子写事务原样保留。见决策一。
- **不做 DB 层的渠道隔离**（不加独立 DB role、不加行级权限）。共库前提下 role 隔离收益有限，且项目现有所有服务共用同一 PG 账号。隔离靠决策二的写入契约 + 消费侧 fail-closed。
- **不动 `channel-registry` 的存在性**。拆分后两服务各剩一个渠道，注册表退化成单元素 Map，是已失去价值的抽象——但它不阻碍拆分，删它是独立清理项。
- **不修 `common_agent_response` 的 TS/Python 双写**。两侧都 INSERT pending 行、都写 `safety_status`，且该表无 `channel` 列。既存风险，单独立项——但拆分使它多了一层依赖，见决策二。
- **不修泳道重放会重新生成 `common_message_id` 的现状**。泳道侧入站是全量重放（`inbound-lane-consumer` 直接调 `handleMessageReceive`，绕过 `dispatchLarkEvent`），信封里的 `global_message_id` 只当幂等 key，泳道会另铸一个 id。这是已知缺陷不是本次引入，保留现状可让"行为与拆分前一致"成为可验证的判据；修它要改跨 lane 的 id 语义，单独立项。
- **不改 `common_*` 五张表的 schema**，不加 `channel` 列，不动任何 DDL。
- **不重写飞书 SDK 封装与富文本渲染的内部算法**。见决策五。

## Key design decisions

**一、共库，服务边界靠代码所有权而非物理分库。** lark_* 七张表由 lark-service 独占读写，common_* 由三方共写——这是项目既有模式（agent-service 现在就在写 `common_message` 和 `common_agent_response`）。选它而非拆库，因为拆库会把两个现存原子写事务变成分布式事务：`common-projector.ts:375` 和 `:489` 在单个 `AppDataSource.transaction` 里同时 insert `common_message` + `lark_message`，报错文案明确要求回滚。为不存在的问题引入 outbox 是纯自伤。服务边界的真实价值——代码所有权、独立部署、飞书 SDK 依赖隔离、故障面隔离——共库全部拿得到。

**二、共库的代价是隔离依赖从"进程内"变成"MQ 路由正确性"，必须显式补防线。** 拆分前 `common_agent_response` 的写入方在同一进程里，靠代码路径隔离；拆分后 lark-service 和 channel-server 各写各的行，而该表**没有 `channel` 列**，DB 层无法拒绝越界写入。唯一的隔离手段是 agent-service 的 routing key 分对了。因此本次必须补两条防线，缺一不可：
- **消费侧 fail-closed**：每个出站消费者拿到 payload 先校验 `channel` 是否属于自己，不属于就拒绝并告警，绝不"顺手处理了"。这条比 rk 分流本身更重要——rk 配错是配置问题，fail-closed 能让它立刻暴露而不是静默写脏。
- **写入矩阵是两个服务的共同契约**，见下方"common_* 写入矩阵"一节。矩阵里标红的三处字段级重叠必须有测试固化，否则"共库不打架"只是口头承诺。

**三、出站由 lark-service 自己消费，agent-service 按 channel 分 routing key。** 飞书出站 payload 很重（markdown→PostContent 渲染、图片下载上传、mention 解析），走 HTTP RPC 要新造一套跨进程 `OutboundCapabilities` 协议并多一跳网络。让 lark-service 直接消费飞书的出站队列，出站在服务内闭环。副产品：channel-server 的 recall-worker 删除——QQ 本来就不实现 recall（`plugins/qq/outbound-capabilities.ts:220`），拆走 lark 后它空转。

**四、入站零新增契约。** `plugins/lark/events/handlers.ts:287` 现在就是飞书插件自己 publish `chat_request` 给 agent-service，channel-server 不做中转。拆分后 lark-service 直连 agent-service，两服务间入站方向不需要任何通信机制。这是本次成本远低于预期的主因。

**五、共享包只放"与渠道无关"的能力，且依赖方向单向。** 判据不是"两边都在用"，而是**这段代码是否需要知道任何具体渠道的存在**。共享包不得 import 任何渠道实现、不得注册渠道专属实体、不得加载渠道专属凭据。因此：
- DataSource 与实体注册**由各服务本地组合**——共享包提供连接构造与 common_* 实体，lark_* / qq_* 实体由各自服务注册。否则两个服务都会加载对方的表。
- bot 身份管理**按 channel 过滤加载**，lark-service 不该持有 QQ 的凭据，反之亦然。
- 规则引擎、`chat_request` 组装与 pending 写入、MQ 客户端与队列拓扑、Redis、泳道路由、请求上下文——这些满足判据，进共享包。
- `core/models/Message` 及其 metadata（417 行）不满足判据，跟随 lark-service 走。

**六、"重新设计"与"行为等价重写"的界线。** 项目重构规则禁止 cp/mv 搬代码。8000 行全部从零重写没有收益，但也不能靠 cp 蒙混，因此按**职责边界在目标架构里是否改变**分类：
- **职责变了 → 重新设计**：飞书内容解析目前有两套并行实现（`events/factory.ts` 的 `MessageTransferer` 产出 `Message`，`inbound.ts` 的 `parseLarkContent` 产出 `InboundMessage`，逐 case 对应但结构不同），合并成一套；`EventRegistry`（205 行 `Map<string, (params:any)=>Promise<void>>`）与 ChannelPlugin 抽象平行存在且只服务飞书，消化掉；`infrastructure` 对 `plugins/lark` 的 4 处反向依赖在新服务里按正确方向重组；`core/models/Message` 归位。
- **职责没变 → 行为等价重写**：飞书 SDK 客户端池、OpenAPI 薄封装、富文本渲染管线、10 条指令、photo/meme/callback、7 个 lark_* 实体、`types/lark.ts` 一族。这里"行为等价"指**先有断言现有行为的测试，再在新位置实现到测试通过**，不是 `cp` 之后改 import 路径。

**七、cutover 是多入口状态机，不是一个开关；且清理必须在切换稳定之后。** 飞书流量有四个独立入口/owner，任何一个漏掉都会双跑或断流：WS 长连（同 app_id 随机投递，两进程同开会静默分流）、HTTP webhook 路由（gateway 规则指向哪个服务）、`inbound_lane.{lane}` 队列（**RabbitMQ 竞争消费——两个服务同时订阅会各拿一半**）、出站队列与定时任务。切换必须按 ready→切流→drain→停旧推进，每一步有可观测的判据。

关键约束两条：
- **飞书入口在异步处理前就 ACK**（`dispatch.ts` fire-and-forget 后立刻 `return {}`），所以"靠平台重试兜空窗"不成立。切换必须做到入口连续，不能有"两边都不接"的窗口。
- **channel-server 的飞书代码在切换稳定前不能删**。这是回滚能力的物理前提——代码删了就只能回滚镜像版本，而那会连带回滚 common 层的其他改动。因此清理是切换**之后**的独立 task，不是之前。这个窗口内两个服务都有飞书代码，是受控的 cutover 窗口而非长期兼容层，关闭条件在 Task F 的验收里写死。

**八、队列的分区维度必须与消费者的所有权维度一致。** 这是决策三的一般形式，两处都要遵守：

- 出站 `chat_response` / `recall`：拆分后 owner 按 channel 分，所以 routing key 按 channel 分。
- 入站 `inbound_lane.{lane}`：**当前只按 lane 分区，但拆分后 owner 是 `channel + lane`**——同一个队列同时承载 QQ 和飞书的信封，而 RabbitMQ 是竞争消费。两个服务同时订阅会产生三个后果，一个比一个隐蔽：
  1. **流量被随机劈成两半**。cutover 窗口内 channel-server 仍然注册着 lark runtime，所以它抢到的飞书信封会被**真的处理掉**，不报错也不留痕。切流因此永远做不干净——新旧两条路径各跑一半，行为差异无从观测。
  2. **fail-closed 的那一侧只能把消息弹回去**。lark-service 抢到 QQ 信封时会 `nack(requeue)` 退回（`lane-queue.ts:246-255`），channel-server 抢到自己没有 runtime 的 channel 时是 `getChannelRuntime` 抛错、同样 requeue（`plugins/runtime.ts:29-31` → `inbound-lane-consumer.ts:89`）。而这条队列刻意不配 TTL 和 DLX，所以退回去的消息只能一直弹——两个服务互相推诿同一批信封，压成慢轮询活锁。
  3. **两侧的幂等 key 格式不同**。channel-server 是 `inbound_lane:{event_type}:{global_message_id}:{lane}`（`inbound-lane.ts:49-51`），lark-service 多一段 channel（`lane-queue.ts:130-138`）。同一条消息重投时换了消费者，就认不出自己处理过。

  这不是 cutover 期的临时问题，是拆完之后的稳态缺陷。修法是让队列也按 `channel + lane` 分区，不同 owner 不共享队列，竞争消费自然不存在。

在分区落地之前，消费侧必须校验信封的 channel 且**不属于自己的绝不 ACK**。这是即时防线，分区完成后它退化成一条防御性断言而不是被删掉——因为"队列里只会有我的消息"是个需要被持续保证的不变量，不是可以默认的事实。

**九、换队列的通用协议是"消费侧先双订阅 → 切生产者 → 旧队列排空 → drain 屏障移交"，四步都不能省。** 三条队列（`chat_response` / `recall` / `inbound_lane`）换名字时面对的是同一个问题：生产者和消费者在不同的 Deployment 里，不可能原子发布。先切生产者，旧消费者守着空队列；先切消费者，新队列没有生产者。所以消费侧必须有一段时间**同时订阅新旧两套队列**，把生产者的部署时刻从关键路径上摘掉。

第三步"移交给 lark-service"是唯一真正危险的一步，而危险不在我起初以为的地方。**同一个队列上的两个消费者只会分摊消息，不会各发一遍**——RabbitMQ 轮询投递，一条消息只交给一个消费者。真正的双发窗口是**旧 worker 已经调完飞书 API、还没 ACK 的那一瞬间**被杀：消息 requeue，换个消费者再发一次，用户看到两条。这个窗口现在就存在（`chat-response-handler.ts` 发送后无条件 ACK，注释里认了这条 at-least-once 残留），交接只是把它从"进程偶然崩溃"变成"我们主动触发"。

因此移交必须走 drain 屏障，而不是"停旧起新"：先对旧消费者发 `basic.cancel`（停止新投递，已投递的继续处理完）→ 等它的 unacked 归零 → 再启动新消费者。中间没有消息在飞，也没有队列堆积。

**"先停旧、让消息堆一会儿"这条路对泳道队列根本不成立**：`chat_response_{lane}` 带 10s TTL + DLX 回落 prod rk，堆过 10 秒的消息会被弹到 prod 队列由 prod 实例处理——泳道的回复从 prod 发出去，比堆积严重得多。prod 队列没有 TTL 可以堆，泳道队列不行。所以交接顺序是**先 prod、再逐个泳道**，且泳道交接必须在 drain 屏障内完成，不能有堆积。

`inbound_lane` 的迁移同理，别因为它是入站就以为可以"同一批部署"糊过去：消费者先同时订阅 `inbound_lane.{lane}` 和 `inbound_lane.{channel}.{lane}` → 投递侧切新队列名 → 旧队列排空 → 按 drain 屏障把新队列移交给 lark-service。

**十、持长连的进程和出站消费必须是两个 Deployment，因为它们的部署策略天然冲突。** 飞书 WS 长连同 app_id 是随机投递，两个进程同时开着会静默分流（决策七），所以持长连的那个 Deployment 只能 `replicas=1` + `Recreate`——滚动更新会有一段两个 Pod 都连着的时间。而出站消费恰恰相反：它是竞争消费，天然可以多副本、可以滚动更新、崩一个不影响别的。

把两者塞进一个进程，等于让出站也继承"单副本 + 停机式部署"这套约束：改一行渲染逻辑要停整个飞书入口，出站的一次 OOM 会带走长连。这不是资源账能抵消的，所以 lark-service 也是一镜像多服务：

| 进程 | 职责 | 副本与发布策略 |
|---|---|---|
| `lark-service` | HTTP webhook + WS 长连 + 泳道信封消费 | `replicas=1`，`Recreate` |
| `lark-outbound` | `chat_response` 与 `recall` 两条出站队列 | 可多副本，滚动更新 |

出站只拆一个进程而不是照搬 channel-server 的两个：recall 的流量极低，且与发消息同属"把赤尾的动作送到飞书"这一件事，没有分开扩缩容的理由。真需要了再拆，那时加一个编译目标就行。

**但这张表现在一格都落不了地。** `Makefile` 的每次 release 都硬编码 `"replicas":1`（sibling 也一样），所以手工扩容会被下一次发布重置；`paas-engine` 的 deployer 里根本没有部署策略字段，`Strategy` / `RollingUpdate` / `Recreate` 全仓零命中，用的是 K8s 默认的滚动更新。于是想要的两件事都拿不到：`lark-service` 拿不到 `Recreate`，`lark-outbound` 拿不到多副本。

这不只是新服务的问题——channel-server 现在就持着飞书长连，滚动更新时"新旧两个 Pod 同时连着"的窗口**已经存在于线上**，只是没人把它写下来过。

**bezhai 已拍板（2026-08-11）：短期接受，只登记不修。** 拆分不会让这个窗口变差（新服务与 channel-server 现状同形），而改 paas-engine 是动所有服务共用的部署路径，风险大于此刻解决它的收益。于是这张表降级为**目标形态**，不是 Task E 的前置：`lark-service` 与 `lark-outbound` 都按单副本、默认滚动更新部署。单副本同时避开了下面"已知缺陷"里那条跨段终态不单调的问题被多副本放大。等 paas-engine 补上副本数与发布策略字段之后再回来落这张表。

## Caller coverage

本次改的是模块归属而非函数签名，覆盖面按"谁依赖飞书代码"清点。

| 依赖方 | 现状 | 拆分后 | 适配 |
|---|---|---|---|
| `infrastructure/integrations/lark-client.ts:3` | import `@plugins/lark/bot-identity` | 搬入 lark-service，依赖方向理顺 | 重组 |
| `infrastructure/integrations/lark/basic/message.ts:6`、`basic/group.ts:4`、`utils/mention-utils.ts:8` | 同上，3 处反向边 | 同上 | 重组 |
| `infrastructure/crontab/services/daily-photo.ts:8,11` | import `@plugins/lark/services/photo/*` | 搬入 lark-service；channel-server crontab 无 service 剩余 | 迁移 |
| `infrastructure/crontab/services/emoji.ts` | 不碰 `@plugins/lark`，走 `@http/client` 与 `lark_emoji` 仓储 | 同上一起搬 | 迁移 |
| `workers/chat-response-handler.ts:170`、`recall-worker.ts:178` | `getCapabilities(payload.channel ?? 'lark')` | lark 分支消失；默认值删除，channel 变必填且 fail-closed | 改 |
| `workers/recall-worker.ts` 整体 | 消费 `recall_{lane}` | 删除（QQ 不实现 recall） | 删 |
| `plugins/runtime.ts:76` | `getChannelRuntime(env.channel ?? 'lark')` | 默认值删除 | 改 |
| `infrastructure/integrations/inbound-lane.ts:23,36` | 注释与 `params: unknown` 按飞书语义 | 泳道信封由 lark-service 消费；**订阅方唯一性是 cutover 的一部分** | 改 |
| agent-service `chat_response` / `recall` publish | 单一 routing key | 按 channel 分 rk | 改 |
| agent-service 读 common_*、monitor-dashboard 读 common_* | 直接读库 | 不变（共库） | 无 |
| `packages/lark-utils` | 仅 media-sync-worker 使用 | 不变；lark-service 可复用而非复制 SDK 封装 | 无 |
| `core/boundary.test.ts` | BASELINE 已空 | 守卫范围跟随新边界更新，且需变异验证 | 改 |

**未被现有守卫覆盖、本次必须一并处理的飞书代码**：`core/models/message.ts` + `message-metadata.ts`（417）、`types/lark.ts`（241）、`types/mongo.ts` 的 `LarkMessageMetaInfo`、`types/content-types.ts` + `post-node-types.ts`（128）、`types/meme.ts`、`types/pixiv.ts`、`types/send-message.ts`、Mongo `lark_event` 集合与 `dal/mongo/client.ts` 的 `insertLarkEvent`。

`types/image-post.ts` 曾列在这里，**但它是死代码**：三个导出类型全仓零引用。emoji 服务的 `getAllEmojis` / `getEmojiByKey` 同理，只有定义和自己的测试、没有生产调用方。这两处 Task D 不迁，Task F 直接删。

## common_* 写入矩阵

拆分后每张 common_* 表的写入方与字段所有权。**"归属"列写的是拆分后由哪个服务承担该次写入**，不是表的所有者——这五张表没有单一所有者，这正是需要矩阵的原因。

| 表 | 写入方 | 写什么 | 场景 | 拆分后归属 |
|---|---|---|---|---|
| `common_user` | 飞书投影 | 全字段 upsert，`channel='lark'` | 每条入站消息的发送者、每个 mention 各一次 | lark-service |
| | QQ 投影 | 全字段 upsert，`channel='qq'` | QQ 入站 | channel-server |
| | bot 身份层 | 生成 `common_user_id` 并回填 `bot_config` | 启动时，幂等 | 两服务各自启动时按自己的 channel 做 |
| `common_conversation` | 飞书投影 | insert 或 update，`channel='lark'` | 每条入站消息；bot 入群事件 | lark-service |
| | QQ 投影 | 同上，`channel='qq'` | QQ 入站 | channel-server |
| `common_message` | 飞书投影 | insert user 行 | 入站（与 `lark_message` 同事务） | lark-service |
| | 飞书投影 | update `bot_name` + `common_user_id` | 抢到去重锁的 bot 认领消息 | lark-service |
| | 飞书投影 | insert assistant 行 | 出站（与 `lark_message` 同事务） | lark-service |
| | QQ 投影 | 对称的三次写 | QQ 入站/出站 | channel-server |
| `common_agent_response` | **chat_request 组装** | INSERT pending 行 | 入站派发 AI 请求前 | **飞书在 lark-service、QQ 在 channel-server** |
| | **agent-service** | **INSERT pending 行** | **fan-out 场景** | **agent-service（⚠️ 与上一行同字段）** |
| | agent-service | UPDATE `bot_name` / `persona_id` | 确定人设后 | agent-service |
| | 出站 worker | 追加 `replies`（jsonb `\|\|` 拼接） | 每发出一段回复 | 飞书在 lark-service、QQ 在 channel-server |
| | 出站 worker | UPDATE `status` / `response_text` | 回复完成 | 同上 |
| | **recall worker** | **UPDATE `safety_status` / `safety_result`** | **撤回流程** | **飞书在 lark-service（⚠️ 见下）** |
| | **agent-service** | **UPDATE `safety_status` / `safety_result`** | **安全判定** | **agent-service（⚠️ 与上一行同字段）** |
| `common_bot_presence` | 飞书事件 | upsert `is_active` | bot 入群 / 退群 | lark-service |
| | 飞书投影 | upsert `is_active=true` | **每条入站消息各刷一次**（见下） | lark-service |
| | QQ 事件 | 同上 | QQ 对应事件 | channel-server |

「每条入站消息刷一次 presence」不是冗余写入，是**自愈**：飞书不保证入群/退群事件必达，而 agent-service 拿这张表当群投递闸门，presence 因丢事件而不准会直接导致该投不投。一次幂等 upsert 换掉这个失效模式，划算。

**三处必须被测试固化的重叠**：

1. **`common_agent_response` 的 pending 行有两个 INSERT 方**（chat_request 组装侧和 agent-service 的 fan-out 侧），而该表有 `session_id` 唯一约束。两侧生成 session_id 的方式不同，现有代码里没有找到明确的仲裁逻辑。拆分不改变这个形态，但会让 INSERT 方从 2 个变成 3 个（飞书、QQ、Python 各一），必须确认唯一约束的冲突语义。
2. **`safety_status` 被 recall worker 和 agent-service 双向写**，且该表**没有 `channel` 列**——DB 层无法拒绝越界写入。拆分后飞书的 recall 移到 lark-service，隔离完全依赖 agent-service 的 routing key 分对了。这是决策二要求消费侧 fail-closed 的直接原因。
3. **`common_message` 的 user 行与 `lark_message` 同事务写入**。共库让这个事务得以保留（决策一），但矩阵里它是唯一一处"common_* 与渠道私有表在同一事务"的写入，任何试图把 common_* 的写入上收到共享层的重构都会打破它。

## 已知缺陷（本次不修，但要记在案上）

这些都是 codex 历次 review 挖出来的，各有明确理由不在拆分这批里动。共同的理由是：这次拆分唯一能依靠的验收口径就是"行为与拆分前一致"，边搬边修会让这个判据失效。

**一、同一个人在两个飞书应用下并发首次出现时，公共层身份不收敛。**

`(app_id, open_id)` 上的 `ON CONFLICT` 保证了同一个飞书应用下"首写者成为 canonical"，但同一个人在每个应用下 open_id 不同，两条流认领的是两把不同的自然键，压根不会撞上同一个冲突。`lark_user_open_id.union_id` 上没有唯一约束（一个 union_id 天然对应多行），"union 维度的首写者"在 DB 层无从表达。

现状是确定性收敛 + 自愈：`larkUserByUnionId` 按 `common_user_id ASC` 取最小值，两个进程算出来一样，`linkLarkUser` 会把指错的行拉回去。代价是收敛发生在**下一条消息**，而窗口期里落的 `common_message` 会永久挂在被淘汰的那个 id 上——事后改映射修不了历史。

不修的理由：两条候选解法都超出拆分的范围，且需要 bezhai 拍板。一是按 union_id 批量 UPDATE 把同 union 的行拉到最小 id，但 memory 里记着"union_id 非全局唯一"，若成立这会把两个人合并，血本太大；二是把 `common_user_id` 改成由 union_id 派生的确定性 uuid，无竞态但换掉了全表的 id 语义（丢掉 v7 的时间有序性），是跨系统决策。

**二、交接分支不落账，所以同一条飞书消息交接到泳道时会带上不同的 `global_message_id`。**

多 bot 群里飞书把同一条消息推给每个 bot，每个 bot 都走一遍投影。判定要交接时函数直接返回、不写 `lark_message`，于是下一个 bot 进来仍然读不到 `existing`，又铸一个新 id。泳道侧的三元组去重 key 是 `event_type + global_message_id + lane`（不含 bot_name），认不出这几条是同一条消息。

**拆分前的 channel-server 是逐字相同的形态**（`plugins/lark/events/handlers.ts` 里那个 `return` 前的注释自己写着"还没写 lark store entry"），所以这不是拆分引入的回归，是等价迁移过来的既有行为。不在这批修是因为改它要动"交接前是否落账"这个决策，而先分叉后落账正是为了避免 prod 落一笔它并不处理的账。Task E 泳道验证时要实际观察多 bot 群的交接行为，确认这个形态在拆分后的表现与线上一致。

**三、飞书入口 ACK 之后，交接失败没有第二次机会。**

飞书要求快速 ACK，从 ACK 那一刻起消息的可靠性就全靠我们自己。加了 publisher confirm 之后，"broker 没收到却按成功返回"这个静默窗口堵上了，但确认失败之后仍然只能记一条错误日志——本地不留账，飞书也不会再推。泳道消费那条路有 requeue 兜底（不丢），webhook 和 WS 两个直连入口没有。

要真正堵住得引入本地 outbox（先落库再投递、后台补投），那是入口可靠性的独立议题，不该塞进拆分。

**五、同一次回复的多段之间没有串行屏障，终态可以被倒着写。**

一次回复会被切成多段（`part_index`）分别投递，而消费端 prefetch 是 10 —— 哪怕只有一个副本，多段也在并发处理。`replies` 的追加是单条 `jsonb ||` 更新，不会丢，但顺序按完成先后而不是 `part_index`。更糟的是终态：某一段失败写了 `failed`，之后最后一段成功写 `completed`，就把失败盖掉了。台账上看是圆满完成，实际有一段没送到真人眼前。

`common_message` 与 `lark_message` 的同事务只保证**单段**的原子性，跨段的顺序和终态单调它管不着。

这是拆分前就有的形态，照搬过来了（Task C2 补了测试把这两个行为钉住，测试名里写明"这是当前行为不是期望行为"）。不在拆分里修的理由：修它要引入按 `session_id` 的串行屏障或者让终态单调，两者都改变可观测行为，会让"行为变了没有"这个判据失效——而那正是这次拆分唯一能依靠的验收口径。**但决策十的多副本会放大它**，所以在修掉之前 `lark-outbound` 按单副本运行。

**六、平台不返回 message_id 时合成的那个假 id，不按 bot 和会话隔离。**

飞书偶尔不回 `message_id`，这时会用触发消息的 id 加段号拼一个（主动发的场景下前半截是空的，结果长得像 `_part0`）。这个合成键没有 bot 维度也没有 session 维度，而两条 insert 都是 `ON CONFLICT DO NOTHING` —— 并发撞上时会留下一条没有 `lark_message` 映射的 `common_message`，后面的消息还可能复用到第一条记录。

与拆分前逐字一致，登记在案。注意它是"两条 insert 同生共死"这个保证之外的东西：事务保证的是两条一起成功或一起回滚，保证不了第二条被 `DO NOTHING` 静默跳过。

**七、每次发布持长连的服务，都有一段新旧两个 Pod 同时连着飞书的窗口。**

K8s 默认的滚动更新是先起新 Pod、Ready 之后才停旧 Pod。而飞书对同 app_id 的多客户端是随机投递不是广播（决策七），所以那几十秒里事件被随机分给新旧两个进程——旧进程收到的那部分处理完就随 Pod 一起没了，两边都不报错，是静默丢消息。

消除它需要 `strategy.type: Recreate`，而 `paas-engine` 的 deployer 没有部署策略字段（`Strategy` / `RollingUpdate` / `Recreate` 全仓零命中），`Makefile` 的每次 release 又硬编码 `"replicas":1`，两个旋钮都不存在（决策十）。

**这是 channel-server 现在就有的线上缺陷，不是拆分引入的**，拆分后 lark-service 与它同形，不会更差。bezhai 2026-08-11 拍板短期接受，只登记不修：改 paas-engine 会动到所有服务共用的部署路径，风险大于此刻的收益。等拆分收尾后单独立项。

**八、撤回的终态短路挡不住并发，它只在终态已经落库之后才生效。**

同一个 `session_id` 的两条撤回并发进来（prefetch 是 10），两条都读到 `pending`、都去删、都写终态。第二条的删除全部失败（消息已经没了），于是 `recalled` 被盖成 `recall_failed`。台账上看是撤回失败，实际撤干净了。

这是缺陷五（跨段终态不单调）在 safety 两列上的孪生，成因一样：短路是读-改-写之间的检查，不是屏障。channel-server 的 recall-worker 逐字同形，Task C3 照搬。

**九、撤回的延迟重投不是原子的，两个方向都会出错。**

重投是「publish 一条延迟消息 → ack 原消息」两步。中间崩会重复投（下一次投递的 `x-retry-count` 还是旧值，等于白送一次重试额度）；`publish` 失败则抛出去进死信，撤回请求就此丢失而台账停在 `pending`——没有任何东西会再来撤这条违规内容。

另外 `x-retry-count` 只活在 AMQP header 上，DB 里不留痕，所以从死信队列重放会让计数从 0 重新开始。

与 channel-server 逐字一致，登记在案。

**十、共享 MQ 客户端对同一队列重复 `consume` 会留下一个再也摘不掉的消费者。**

注册消费者时无条件覆写记下的 consumer tag，而排空只 cancel 当前那个 tag。于是第二次订阅同一队列会在 broker 上留下两个消费者，第一个的 tag 永久丢失——它能活过之后所有的排空和整个交接窗口，而两个消费者分享同一个队列正是决策八要避免的东西。

订阅侧现在用「先排空再订阅」在应用层绕开它（订阅抛错通常意味着连接已死，客户端重连会把它恢复出来，此时直接重试就正好制造幽灵）。**端口级的修法是注册前先 cancel 掉还活着的 tag，本次不做**：应用层已经收敛且有测试钉住，而改共享客户端的 `consume` 语义会波及所有消费者，在即将依赖它做切流的分支上不值得。

顺带两条同源的：channel-server 启动时对旧队列的那次订阅不在任何账本里，抛错是进程退出（不静默），但 reconcile 也永远不会补订它；四态簿记规则现在 lark-service 和 channel-server 各有一份，**不抽共享包**——channel-server 那份随双订阅一起在 Task F 删掉，重复是有期限的。

**四、`/config` 指令写的灰度配置，agent-service 读不到——这条链路目前是断的。**

`/config` 写进 `lark_base_chat_info.gray_config`（`plugins/lark/commands/command-handler.ts:176-182`），而 agent-service 的 `find_gray_config` 读的是 `common_conversation.attachment_policy["gray_config"]`（`app/data/queries/messages.py:281-299`）。全仓 grep 确认：TS 侧所有 `gray_config` 命中都落在 `lark_base_chat_info` 那一列上，没有任何一处往 `attachment_policy` 里写它；Python 侧对 `attachment_policy` 只有一处列定义和两处 select，只读不写。

雪上加霜的是飞书投影**每条消息**都整体覆写 `attachment_policy`，所以即便有谁手工往里塞过 `gray_config`，下一条消息就会把它抹掉。

这与拆分无关，是既有状态。写在这里是因为 Task D 本来要迁 `/config` 指令，照搬会把断链原样搬过去。

**bezhai 已拍板（2026-08-11）：连指令一起删掉，不迁。** 它今天写进去的值没有任何读取方，删掉唯一可观测的变化是管理员敲它不再收到"已设置"的回复。按群灰度这个能力其实另有一个能用的实现——`permission_config.is_canary`，入站链路真的在读它。所以 D0 的投影读端口**不暴露 `gray_config`**，D4 的斜杠指令组少一条。`lark_base_chat_info.gray_config` 这一列本身留着不动（删列要走 schema 变更，且与拆分无关）。

## Data & deployment impact

- **无 schema 变更，无新表，无 DDL**。表的物理位置与结构完全不动，只换代码所有者。lark_* 七张表 + Mongo `lark_event` 归 lark-service 独占；common_* 五张表三方共写，写入矩阵见决策二。
- **无 prompt 变更、无 Dynamic Config 变更**（flag 语义不变，读取方从 channel-server 变成 lark-service）。
- **新服务要在 PaaS 注册**：ImageRepo、App envs、ConfigBundle `required_keys` 覆盖新 app、Deployment 与 Service。飞书凭据只下发给 lark-service。**`MONGO_HOST` 必须在首次部署前就位**——lark-service 用它写 `lark_event` 原始事件审计，配置缺失会按既有的 fail-closed 风格在启动时崩溃（这是刻意的，但要求配置先行）。
- **注册新 workspace 成员会连坐所有镜像**：bun 对"已声明但不在构建上下文里"的 workspace 成员是退出码 1 的硬失败，所以每个跑 `bun install --frozen-lockfile` 的 Dockerfile 都要复制新 app 的 `package.json`。加 app 时这几处必须同一个 commit 落地，否则构建中断（好在是显式失败，不会产出坏镜像）。
- **资源增量要核算**：多一个服务意味着多一份 DB / Redis / MQ 连接池和一组消费者。需要给出新增连接数、消费者 prefetch 与队列积压的观测口径，避免拆完打爆连接上限。其中一个具体数字要盯：投影锁在持续争用下最坏等 75 秒（两个租约 + 余量，推导见 `message-lock.ts` 文件头）才放弃，泳道消费者 prefetch 期间会被占住这么久。观测口径里要能看见"等锁超时"这条错误的频次——它出现就说明有任务卡在锁里，而不是锁的参数配错了。
- **跨服务部署顺序有约束**，三条队列的换名协议见决策九。要点：消费侧的双订阅必须先上线，生产者才能切；移交给 lark-service 那一步必须走 drain 屏障（`basic.cancel` → 等 unacked 归零 → 起新消费者），不能靠"停旧让它堆一会儿"——泳道队列带 10s TTL，堆过 10 秒会 DLX 弹回 prod，泳道的回复就从 prod 实例发出去了。交接顺序是先 prod、再逐个泳道。
- **顺序做反了的症状要认得出来**：如果 agent-service 先切了 rk 而消费侧还是旧版，新队列没有消费者，prod 的消息一直积压，泳道的消息 10 秒后回落到同 channel 的 prod 新队列继续积压。**不会双发，但赤尾整个不说话了**。这个失败是安静的——队列在涨，服务全部健康。所以"消费侧先上线"要当发布门禁执行，不能靠记性。
- **切换用的几个 Dynamic Config 开关都是全局的，不是每泳道的旋钮**。判归属和决定订阅面都发生在请求上下文之外，`DynamicConfig` 拿不到 lane 就按 prod 解析——给它们设按泳道的 override 会**静默失效**。后果是收窄 channel-server 的入站所有权会让所有泳道一起不再认领飞书信封，只有部署了 lark-service 的那条泳道有人接。因为 `inbound_lane` 本来就只有泳道消费、而泳道是我们自己拉起又拆掉的临时环境，这个代价可以接受；但 Task E 排期时要按"所有泳道同时切"来安排，别指望逐条泳道灰度。
- **所有权配置的记忆只活在进程里**。读不到配置时保持上次结论，但重启会丢掉这份记忆：如果重启那一刻恰好也读不到，就会退回"拥有全部"，并按这个结论决定启动时的订阅面。真正关掉这个窗口的是 Task F——把 lark 从两份 channel 清单里删掉之后，再宽也宽不到它身上。
- **部署中断在途任务**：channel-server 部署杀在途 chat_response 处理；agent-service 部署杀 rebuild / afterthought / world / life。
- **回滚路径分两段**：切换稳定前，回滚 = 入口切回 channel-server + 队列订阅切回 + agent-service rk 回退，三步都是配置级，因为两侧代码都在，可逆；清理完成后，回滚只能靠镜像版本回退，代价大得多。这正是清理必须在切换稳定之后的原因。

## Tasks

A 是全部前置。B 建骨架，C/D 依赖 B 的骨架产出。E 依赖 B/C/D。F 依赖 E 稳定运行。C 内部：C1 是 C2/C3 的前置（它们要用新的路由口径），C2 与 C3 共用同一片出站代码因而串行，C4 与其余三条互不相干。**不预先宣称文件不重叠——并行前需按实际触达文件重新分区。**

**Task A — 抽取渠道无关的共享能力**
- **Goal**: 两服务共用的能力在 `packages/` 下有唯一定义，且共享包不认识任何具体渠道；channel-server 切过去后行为不变。
- **Deliverable**: `packages/` 下的共享包及其公共 API 边界；channel-server 改为依赖它，旧位置实现删除。
- **Verification**: 共享包内 grep 不到任何渠道名、渠道实体、渠道凭据；DataSource 的实体注册由调用方传入而非包内写死；bot 身份加载支持按 channel 过滤且有测试；channel-server 全量测试绿；被提取符号在 `apps/channel-server/src` 下零定义残留。

**Task B — lark-service 骨架与入站闭环**
- **Goal**: 新服务可独立启动并接收飞书事件（webhook / WS / 泳道信封三个入口），完成 common 投影、规则与指令、publish `chat_request`，全程不依赖 channel-server 进程。
- **Deliverable**: `apps/lark-service` 服务骨架（启动装配、配置、依赖注入）与完整入站链路；两套飞书解析合并为一套；EventRegistry 消化进渠道抽象；飞书私有模型从 `core/models` 归位。
- **Verification**: 三个入口各自的完整入站路径有测试，断言投影写入的表与字段、`chat_request` payload 的 common id 与 lane header 与拆分前逐值一致；骨架产出的公共装配点足以让 C/D 挂载。**另需组装级断言固定各部署入口的 bot 加载范围**：lark-service 只加载 `channel='lark'`，channel-server 的三个入口（server / chat-response-worker / recall-worker）仍加载全部渠道——否则拆分时容易顺手把 channel-server 也收窄，QQ 静默失去 bot 配置。

**Task C 拆成四条。** 一条 task 装不下"跨 TS/Python 两个语言、三个服务、三条队列，外加一整套飞书富文本出站的重写"。拆开之后每块都能单独测、单独部署、单独回滚。C2/C3/C4 的新消费者一律**带一个默认关闭的开关交付**——代码可以先上线、先部署、先观察，消费什么时候开是 Task E 的事，与代码发布解耦。

**Task C1 — 队列拓扑的 channel 维度与双订阅兼容层**
- **Goal**: `chat_response` / `recall` 的队列名与 routing key 带上 channel 维度，TS 与 Python 两侧同一套命名口径；agent-service 按消息的 channel 发对应 rk；channel-server 的两个 worker 同时订阅新旧两套队列，使生产者的部署时刻不再位于关键路径上（决策九）。
- **Deliverable**: 两侧的路由命名（含泳道后缀与 DLX 回落的组合语义）；agent-service 的 rk 分流；channel-server 两个 worker 的双订阅与"订阅哪些 channel"的运行期开关。本 task 不含任何飞书业务逻辑。
- **Verification**: 测试断言同一条出站消息在 channel 不同时落到不同队列、且泳道后缀与 header 语义沿用已上线的 header-only 口径；断言泳道队列的 TTL/DLX 回落目标在加了 channel 维度之后仍指向同 channel 的 prod 队列（弹错 channel 比不弹更糟）；双订阅在两套队列上各投一条都能被处理；**故意投递不属于自己的 channel 时消费者拒绝并告警**，不能只测 happy path。

**Task C2 — lark-service 的飞书出站发送闭环**
- **Goal**: lark-service 能消费飞书的 `chat_response`，完成反查、富文本渲染、发送、落库与台账更新，行为与拆分前逐值一致。
- **Deliverable**: 出站链路的完整实现（common id → 飞书坐标的反查、markdown 与 mention 与图片的渲染、飞书发送 API、`common_message` assistant 行与 `lark_message` 的同事务写入、`common_agent_response` 的 replies 追加与终态更新）；独立的出站进程入口（决策十）；默认关闭的消费开关。
- **Verification**: 出站写事务的原子性有测试掩护（其中一条 insert 失败时另一条不留痕）；渲染管线的顺序不变量有测试钉住（mention 必须先于图片替换，否则 @名字 会落进图片 alt 被改坏）；分段发送、主动发送、回复原消息三种分支各有测试；平台返回空 message_id 时的落库行为与拆分前一致；**lark-service 侧的队列名断言接到 C1 那份跨语言契约向量上**——现在只有 ts-shared 和 agent-service 读它，第三方还在写死字面量，跨语言契约就没有真正闭环。

**Task C3 — lark-service 的撤回闭环**
- **Goal**: lark-service 能消费飞书的 `recall`，逐条撤回并写下 safety 终态。
- **Deliverable**: 撤回链路实现（台账反查、逐条撤回、部分失败的计数语义、终态与延迟重投）；默认关闭的消费开关。
- **Verification**: **`safety_status` 与 `safety_result` 的终态更新需要直接断言**——写入矩阵里它是三处字段级重叠中唯一还没有测试正面覆盖的（现有测试只覆盖了 agent-service 侧的"不写 blocked"），而拆分后飞书 recall 移到 lark-service，正是这个字段跨服务写入的地方；部分撤回失败时的计数与终态、达到重投上限时的终态各有测试；终态短路（已是 recalled / recall_failed 时不重复撤）有测试。

**Task C4 — `inbound_lane` 的分区迁移**
- **Goal**: 泳道信封队列按 `channel + lane` 分区，两个服务不再共享队列；两侧的幂等 key 口径统一。
- **Deliverable**: 队列命名的 channel 维度与两侧的双订阅；幂等 key 统一（现在两侧格式不同，换手重投会认不出自己处理过）；默认关闭的新队列消费开关。
- **Verification**: 分区后两个服务的队列名与幂等 key 均不重叠；"分区后不该再收到别人的信封"这条不变量有断言钉住（决策八要求这段校验退化成断言而不是被删）；双订阅期间新旧两个队列各投一条都能被正确处理且不重复处理。

**Task D — 飞书专属业务迁移**
- **Goal**: 10 条飞书指令、photo/meme/callback、**入站附件管线**、daily-photo 与 emoji 定时任务在 lark-service 内正常工作。
- **入站附件管线不能漏**：`enqueueLarkImagePipeline` / `enqueueLarkFilePipeline`（`plugins/lark/image-pipeline.ts`、`file-pipeline.ts`）把飞书的 image_key / file_key 交给 tool-service 下载存进 TOS。这是入站链路的一部分、不是指令，最初的任务划分里两边都没写到它。漏掉的后果是切流之后附件静默不再入库，`read_book` 之类依赖 TOS 里 `files/<file_key>` 的能力会稳定读不到东西——而且入站本身照常工作，不会有任何错误信号。搬的时候注意它的 gate 是 `allowDownloadResource()`（群没开"所有人可下载"就整体跳过），在 lark-service 侧对应 `attachment_policy.download_allowed`；两个管线都是 fire-and-forget，失败只记日志、不阻塞入站。
- **`repeatMessage` 的并发前提变了**：拆分前整个规则段跑在 om_id 锁里，所以复读功能那套 Redis get-modify-set 天然是同一条消息串行的。lark-service 的 om_id 锁只包投影（见 Task B 的行为差异），规则段在锁外，多个 bot 会并发跑到它。搬之前要么让它自身幂等，要么自己取一把锁——`make_reply` 那把去重锁覆盖不到它（那把锁只在有待发 chat.request 意图时才取）。
- **`user_group_binding` 也要一起搬，它是第八张飞书独占表。** 上面"飞书独占的七张表"那句少数了一张：`/bind`、`/unbind` 和退群自动拉回都读写它，全仓使用点没有一处不是飞书，而 lark-service 现在的实体清单里没有它。漏掉的后果是这三个功能切流后直接失效。
- **卡片回调现在会被静默丢弃。** lark-service 的 `/webhook/{bot}/card` 路由已经注册、事件槽也会把它标成 `card.action.trigger`，但入站的事件处理表里只有消息接收一项，于是回调进来只打一条"没人处理这个事件类型"的 warn 就扔掉。三种卡片交互（更新图卡、拉图片详情、更新日报卡）全在这条路上，且它们不经过规则引擎，是独立于指令系统的第二条入站路径。
- **定时任务是三个不是两个**（发图日报、次日新图、emoji 同步），并且**归 `lark-service` 进程**，不归 `lark-outbound`。决策十那张进程表没写 cron 归谁：拆分前它跟 HTTP 服务同进程，照搬；更重要的是它必须待在单副本的那个进程里，否则往写死的真实飞书群发日报会发两遍。lane gate 沿用现有的"非 prod 部署不启动"。

**Task D 的切分：一条前置 + 四条并行。** 直接四路并行会让四条 task 同时改飞书 API 端口、投影读端口、规则序列、配置清单和依赖清单这五处，所以先落 **D0（装配缝）**：把飞书出站 API 端口扩到指令需要的全部方法、把投影读端口扩到 `is_admin` / `permission_config` / 群成员 / 用户组绑定（**不含 `gray_config`**，见已知缺陷四）、把规则序列改成从一份指令清单拼接（顺序契约不变：utility 在前、人格 catch-all 在后）、补 cron 注册器与 lane gate、补齐依赖与配置清单。D0 落地后，**D1 附件管线 / D2 发图与卡片回调与图片日报 / D3 emoji 与复读 / D4 其余指令**四条可以全并行。严格说不是"零相交"：三份账本（指令清单、定时任务清单、组装根）是共用的，但每条任务在上面只加一行、删一行——填自己那个槽、递自己那个依赖。冲突是行级且语义无关的。

三处不建议拆开：图片卡片的构建被指令、卡片回调、定时任务三个入口共用，拆开必然三方共改同一处；`lark_emoji` 的唯一读端就是复读功能，写端（同步任务）和读端放一起才有可测的闭环；发图与卡片回调共用同一套上传与渲染。

- **Deliverable**: 上述业务在 lark-service 下的实现（按决策六，先有行为断言测试再实现）；`lark_emoji` 与 `user_group_binding` 两个仓储随之迁移；卡片回调接进入站事件处理表。
- **Verification**: 指令与服务测试在新服务下全绿；定时任务的 lane gate 行为保持（非 prod 部署不启动）；附件管线在"群允许下载"和"群不允许"两种情况下的行为各有测试；卡片回调三种 action 各有测试且不再落到"没人处理"分支；与拆分前的行为差异逐项列出并解释。
- **`lark_base_chat_info.chat_mode` 不是会话类型的可靠分类，照搬读它的判断会踩雷。** 公共层投影对这一列的 upsert 冲突键是 `chat_id`，值写死成"直聊就 p2p、否则 group"，于是**每条入站消息都重写它**——话题群的 `topic` 会被自己的消息流冲成 `group`（线上 905 行里只有 2 行还是 `topic`，且都是没有消息流的会话）。后果是上游那些 `chat_mode === 'group'` 的判断，在任何走得到它的场景下都恒真、等于没有条件。**把这类判断结构照搬、但改读事件上的真实会话类型，就会把一个恒真判断变成真会拦人的闸**——D2 在发图的人数闸上踩过一次：≤20 人的话题群本来允许发图，照搬后变成要开白名单。迁移时遇到读 `chat_mode` 的代码，先确认它在实际数据下取值范围是什么，别按列名的字面含义理解。
- **拿赤尾测指令会看到"没反应"，那不是接错线。** 上游十条指令里九条声明了 `category: 'utility'`，而共享规则引擎在 bot 是 persona 角色时会跳过 utility 类规则、继续往后兜底到聊天主链路（只有"撤回"没声明 category，所以它对谁都跑）。于是用赤尾这类 persona bot 验证指令，现象是"指令静默、赤尾照常聊天"。这与拆分前逐字一致（同一个共享引擎），迁移时不要把它当成自己的接线错误去"修"。验证指令要用 `tool` 这类 utility bot。
- **注意大部分待迁代码没有测试**：指令、meme、卡片回调、图片卡片构建、图片日报这些现在是零覆盖，决策六要求的"先有断言现有行为的测试"在这批基本等于从零写，不是补几条。

**Task E — 泳道验证与生产切换**
- **Goal**: 证明拆分后飞书链路端到端行为与拆分前一致，并在不丢消息、不双跑的前提下完成生产切换。**此阶段 channel-server 的飞书代码仍在，回滚是配置级的。**
- **前置铁律**：在 `inbound_lane` 完成按 channel 分区（见决策八）之前，**不得把 lark-service 部署到任何泳道**。它和该泳道的 channel-server 会竞争消费同一个队列：飞书信封被谁抢到全看运气（而 channel-server 此时仍有 lark runtime，抢到就真处理），QQ 信封被 lark-service 抢到则会一直 requeue 弹回来，两个服务互相推诿。切流期间还须确认每个队列只有一个订阅者。
- **切流判据是 `/api/ready` 返回 200，不是 `/api/health`**：飞书 SDK 的 `start()` 只是异步发起重连、**不等待首次连接成功**，所以"进程起来了"完全不代表它在接飞书事件。两个端点职责已分开——`/api/health` 恒 200（liveness，重连抖动不该触发重启），`/api/ready` 在 `connected !== expected` 时返回 503。用错端点会造成"旧的已停、新的没连上"且无告警的静默断流窗口。
- **必须实际触发一次"首次认领"**：身份与会话的收敛靠两条手写 `ON CONFLICT ... COALESCE`（`lark_user_open_id` / `lark_base_chat_info`）。它们的 SQL 由真 TypeORM 生成并被断言，但开发机连不到库，**从未在真 PG 上执行过**。泳道验证必须包含一个此前没见过的用户或会话，让这两条语句真的跑一次。
- **Deliverable**: 泳道验证记录（命令 + 实际输出）；四个入口/owner 的切换与回滚执行记录；**PaaS 侧的两个新 App 注册**（`lark-service` 与 `lark-outbound` 共用一个镜像，后者只是换 command；两者都按单副本 + PaaS 默认滚动更新部署，理由见决策十与已知缺陷七）；**镜像与服务映射表登记**——`CLAUDE.md` / `README.md` / `docs/service-topology.md` 里那张表现在完全没有 lark-service，而它是"查日志该用哪个服务名"的单一来源，漏登记的后果是排查时对着错的 Deployment 捞日志。
- **Verification**: 泳道内走通完整飞书往返（收消息、AI 回复、指令、撤回），lane 跨服务不丢；切换过程中确认四个 owner 各自唯一——WS 长连只有一个持有者、webhook 只指向一个服务、`inbound_lane.{lane}` 只有一个订阅者、定时任务只有一个执行者；切换后旧路径零流量、新路径全量，且期间无消息丢失或重复发送的证据。

**Task F — 清理与边界收口**
- **Goal**: 在切换稳定后删除 channel-server 的飞书代码，关闭 cutover 窗口。
- **Deliverable**: 残余飞书代码（plugins/lark、types/、lark_* 实体、mongo 飞书方法、infrastructure/integrations/lark*）删除；**recall-worker 整个 Deployment 下线**（QQ 不实现 recall，拆走飞书后它空转）；**出站与入站老队列的双订阅删除**（决策九的窗口在此关闭）；**两份 channel 清单里的 `lark` 删除**（删掉之后所有权配置读不到时也不可能再宽到飞书，前面那个重启窗口才算真正关上）；**泳道信封的幂等占位协议收进共享包**（现在 channel-server 和 lark-service 各写了一份，靠两边断言同样的字面量对齐——删掉 channel-server 那份之后就该只剩一处定义）；`?? 'lark'` 兜底清除；`core/boundary.test.ts` 守卫范围按新边界更新。删 Deployment 连带要改 `Makefile` 的 sibling 声明和 `CLAUDE.md` / `README.md` / `docs/service-topology.md` 里的镜像与服务映射表。
- **Verification**: 明确写出"切换已稳定"的判据并确认满足后才动手；`grep -ri lark apps/channel-server/src` 的每条命中都能解释为非飞书含义；channel-server 全量测试绿；边界守卫在故意违规时确实转红（变异验证，不能只看绿）。
- **删老队列前，"队列深度为零"不是充分条件**：recall 的重试用的是延迟投递，在途的延迟消息压根不出现在队列深度里，最长能晚十几秒才落地。判据要三条同时成立——旧 Pod 全退、超过最大重投延迟的等待期已过、旧 rk 的入流速率与 unacked 都为零——之后才解绑删队列。
- **这一步是回滚级别的分界线**，要在执行记录里写明：动手之前回滚是改配置，动手之后回滚要同时回镜像和队列拓扑。跨过去就没有便宜的退路了。
