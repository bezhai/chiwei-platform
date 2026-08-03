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
- 入站 `inbound_lane.{lane}`：**当前只按 lane 分区，但拆分后 owner 是 `channel + lane`**——同一个队列同时承载 QQ 和飞书的信封。RabbitMQ 是竞争消费，两个服务同时订阅会各拿一半；更糟的是拿错的一半不会报错——消费者查不到对应 channel 的 handler 时会按 no-op 成功返回并 ACK，消息静默消失。这不是 cutover 期的临时问题，是拆完之后的稳态缺陷。修法是让队列也按 `channel + lane` 分区，不同 owner 不共享队列，竞争消费自然不存在。

在分区落地之前，消费侧必须校验信封的 channel 且**不属于自己的绝不 ACK**。这是即时防线，分区完成后它退化成一条防御性断言而不是被删掉——因为"队列里只会有我的消息"是个需要被持续保证的不变量，不是可以默认的事实。

## Caller coverage

本次改的是模块归属而非函数签名，覆盖面按"谁依赖飞书代码"清点。

| 依赖方 | 现状 | 拆分后 | 适配 |
|---|---|---|---|
| `infrastructure/integrations/lark-client.ts:3` | import `@plugins/lark/bot-identity` | 搬入 lark-service，依赖方向理顺 | 重组 |
| `infrastructure/integrations/lark/basic/message.ts:6`、`basic/group.ts:4`、`utils/mention-utils.ts:8` | 同上，3 处反向边 | 同上 | 重组 |
| `infrastructure/crontab/services/daily-photo.ts:8,11`、`emoji.ts` | import `@plugins/lark/services/photo/*` | 搬入 lark-service；channel-server crontab 无 service 剩余 | 迁移 |
| `workers/chat-response-handler.ts:170`、`recall-worker.ts:178` | `getCapabilities(payload.channel ?? 'lark')` | lark 分支消失；默认值删除，channel 变必填且 fail-closed | 改 |
| `workers/recall-worker.ts` 整体 | 消费 `recall_{lane}` | 删除（QQ 不实现 recall） | 删 |
| `plugins/runtime.ts:76` | `getChannelRuntime(env.channel ?? 'lark')` | 默认值删除 | 改 |
| `infrastructure/integrations/inbound-lane.ts:23,36` | 注释与 `params: unknown` 按飞书语义 | 泳道信封由 lark-service 消费；**订阅方唯一性是 cutover 的一部分** | 改 |
| agent-service `chat_response` / `recall` publish | 单一 routing key | 按 channel 分 rk | 改 |
| agent-service 读 common_*、monitor-dashboard 读 common_* | 直接读库 | 不变（共库） | 无 |
| `packages/lark-utils` | 仅 media-sync-worker 使用 | 不变；lark-service 可复用而非复制 SDK 封装 | 无 |
| `core/boundary.test.ts` | BASELINE 已空 | 守卫范围跟随新边界更新，且需变异验证 | 改 |

**未被现有守卫覆盖、本次必须一并处理的飞书代码**：`core/models/message.ts` + `message-metadata.ts`（417）、`types/lark.ts`（241）、`types/mongo.ts` 的 `LarkMessageMetaInfo`、`types/content-types.ts` + `post-node-types.ts`（128）、`types/meme.ts`、`types/image-post.ts` + `types/pixiv.ts`、`types/send-message.ts`、Mongo `lark_event` 集合与 `dal/mongo/client.ts` 的 `insertLarkEvent`。

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

这三条都是 codex 两轮 review 挖出来的，各有明确理由不在拆分这批里动。

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

**四、`/config` 指令写的灰度配置，agent-service 读不到——这条链路目前是断的。**

`/config` 写进 `lark_base_chat_info.gray_config`（`plugins/lark/commands/command-handler.ts:176-182`），而 agent-service 的 `find_gray_config` 读的是 `common_conversation.attachment_policy["gray_config"]`（`app/data/queries/messages.py:281-299`）。全仓 grep 确认：TS 侧所有 `gray_config` 命中都落在 `lark_base_chat_info` 那一列上，没有任何一处往 `attachment_policy` 里写它；Python 侧对 `attachment_policy` 只有一处列定义和两处 select，只读不写。

雪上加霜的是飞书投影**每条消息**都整体覆写 `attachment_policy`，所以即便有谁手工往里塞过 `gray_config`，下一条消息就会把它抹掉。

这与拆分无关，是既有状态。写在这里是因为 Task D 要迁 `/config` 指令，照搬会把断链原样搬过去。迁之前需要 bezhai 确认这个功能是否还要——要就得决定灰度配置的权威位置在哪（`lark_base_chat_info` 是飞书私有表，而读它的是渠道无关的 agent-service，所以大概率该往 common 侧走），不要就连指令一起删掉，别搬一个不通的东西过去。

## Data & deployment impact

- **无 schema 变更，无新表，无 DDL**。表的物理位置与结构完全不动，只换代码所有者。lark_* 七张表 + Mongo `lark_event` 归 lark-service 独占；common_* 五张表三方共写，写入矩阵见决策二。
- **无 prompt 变更、无 Dynamic Config 变更**（flag 语义不变，读取方从 channel-server 变成 lark-service）。
- **新服务要在 PaaS 注册**：ImageRepo、App envs、ConfigBundle `required_keys` 覆盖新 app、Deployment 与 Service。飞书凭据只下发给 lark-service。**`MONGO_HOST` 必须在首次部署前就位**——lark-service 用它写 `lark_event` 原始事件审计，配置缺失会按既有的 fail-closed 风格在启动时崩溃（这是刻意的，但要求配置先行）。
- **注册新 workspace 成员会连坐所有镜像**：bun 对"已声明但不在构建上下文里"的 workspace 成员是退出码 1 的硬失败，所以每个跑 `bun install --frozen-lockfile` 的 Dockerfile 都要复制新 app 的 `package.json`。加 app 时这几处必须同一个 commit 落地，否则构建中断（好在是显式失败，不会产出坏镜像）。
- **资源增量要核算**：多一个服务意味着多一份 DB / Redis / MQ 连接池和一组消费者。需要给出新增连接数、消费者 prefetch 与队列积压的观测口径，避免拆完打爆连接上限。其中一个具体数字要盯：投影锁在持续争用下最坏等 75 秒（两个租约 + 余量，推导见 `message-lock.ts` 文件头）才放弃，泳道消费者 prefetch 期间会被占住这么久。观测口径里要能看见"等锁超时"这条错误的频次——它出现就说明有任务卡在锁里，而不是锁的参数配错了。
- **跨服务部署顺序有约束**：agent-service 的 rk 分流必须先于 lark-service 开始消费（否则新队列无生产者），而 lark-service 必须先能消费再切入站（否则积压）。
- **部署中断在途任务**：channel-server 部署杀在途 chat_response 处理；agent-service 部署杀 rebuild / afterthought / world / life。
- **回滚路径分两段**：切换稳定前，回滚 = 入口切回 channel-server + 队列订阅切回 + agent-service rk 回退，三步都是配置级，因为两侧代码都在，可逆；清理完成后，回滚只能靠镜像版本回退，代价大得多。这正是清理必须在切换稳定之后的原因。

## Tasks

A 是全部前置。B 建骨架，C/D 依赖 B 的骨架产出。E 依赖 B/C/D。F 依赖 E 稳定运行。**B/C/D 不预先宣称文件不重叠——并行前需按实际触达文件重新分区。**

**Task A — 抽取渠道无关的共享能力**
- **Goal**: 两服务共用的能力在 `packages/` 下有唯一定义，且共享包不认识任何具体渠道；channel-server 切过去后行为不变。
- **Deliverable**: `packages/` 下的共享包及其公共 API 边界；channel-server 改为依赖它，旧位置实现删除。
- **Verification**: 共享包内 grep 不到任何渠道名、渠道实体、渠道凭据；DataSource 的实体注册由调用方传入而非包内写死；bot 身份加载支持按 channel 过滤且有测试；channel-server 全量测试绿；被提取符号在 `apps/channel-server/src` 下零定义残留。

**Task B — lark-service 骨架与入站闭环**
- **Goal**: 新服务可独立启动并接收飞书事件（webhook / WS / 泳道信封三个入口），完成 common 投影、规则与指令、publish `chat_request`，全程不依赖 channel-server 进程。
- **Deliverable**: `apps/lark-service` 服务骨架（启动装配、配置、依赖注入）与完整入站链路；两套飞书解析合并为一套；EventRegistry 消化进渠道抽象；飞书私有模型从 `core/models` 归位。
- **Verification**: 三个入口各自的完整入站路径有测试，断言投影写入的表与字段、`chat_request` payload 的 common id 与 lane header 与拆分前逐值一致；骨架产出的公共装配点足以让 C/D 挂载。**另需组装级断言固定各部署入口的 bot 加载范围**：lark-service 只加载 `channel='lark'`，channel-server 的三个入口（server / chat-response-worker / recall-worker）仍加载全部渠道——否则拆分时容易顺手把 channel-server 也收窄，QQ 静默失去 bot 配置。

**Task C — 出站闭环与 channel 分流**
- **Goal**: 飞书的 `chat_response` 与 `recall` 由 lark-service 消费并完成发送/撤回；agent-service 按 channel 分 rk；两侧消费者对不属于自己的 channel fail-closed。
- **Deliverable**: lark-service 出站 worker（反查、渲染、飞书 API、common+lark_message 写入）；agent-service rk 分流；channel-server recall-worker 删除；两侧 fail-closed 校验。
- **Verification**: 测试断言 rk 分流后飞书消息只进飞书队列、QQ 只进 QQ 队列，lane 后缀与 header 语义沿用已上线的 header-only 口径；**故意投递错 channel 时消费者拒绝并告警**（不能只测 happy path）；出站写事务原子性有测试掩护。**`safety_status` 的 recall 侧终态更新需要直接断言**——写入矩阵里它是三处字段级重叠中唯一还没有测试正面覆盖的（现有测试只覆盖了 agent-service 侧的"不写 blocked"），而拆分后飞书 recall 移到 lark-service，正是这个字段跨服务写入的地方。

**Task D — 飞书专属业务迁移**
- **Goal**: 10 条飞书指令、photo/meme/callback、**入站附件管线**、daily-photo 与 emoji 定时任务在 lark-service 内正常工作。
- **入站附件管线不能漏**：`enqueueLarkImagePipeline` / `enqueueLarkFilePipeline`（`plugins/lark/image-pipeline.ts`、`file-pipeline.ts`）把飞书的 image_key / file_key 交给 tool-service 下载存进 TOS。这是入站链路的一部分、不是指令，最初的任务划分里两边都没写到它。漏掉的后果是切流之后附件静默不再入库，`read_book` 之类依赖 TOS 里 `files/<file_key>` 的能力会稳定读不到东西——而且入站本身照常работа，不会有任何错误信号。搬的时候注意它的 gate 是 `allowDownloadResource()`（群没开"所有人可下载"就整体跳过），在 lark-service 侧对应 `attachment_policy.download_allowed`；两个管线都是 fire-and-forget，失败只记日志、不阻塞入站。
- **`repeatMessage` 的并发前提变了**：拆分前整个规则段跑在 om_id 锁里，所以复读功能那套 Redis get-modify-set 天然是同一条消息串行的。lark-service 的 om_id 锁只包投影（见 Task B 的行为差异），规则段在锁外，多个 bot 会并发跑到它。搬之前要么让它自身幂等，要么自己取一把锁——`make_reply` 那把去重锁覆盖不到它（那把锁只在有待发 chat.request 意图时才取）。
- **Deliverable**: 上述业务在 lark-service 下的实现（按决策六，先有行为断言测试再实现）；`lark_emoji` 仓储随之迁移。
- **Verification**: 指令与服务测试在新服务下全绿；定时任务的 lane gate 行为保持（非 prod 部署不启动）；附件管线在"群允许下载"和"群不允许"两种情况下的行为各有测试；与拆分前的行为差异逐项列出并解释。

**Task E — 泳道验证与生产切换**
- **Goal**: 证明拆分后飞书链路端到端行为与拆分前一致，并在不丢消息、不双跑的前提下完成生产切换。**此阶段 channel-server 的飞书代码仍在，回滚是配置级的。**
- **前置铁律**：在 `inbound_lane` 完成按 channel 分区（见决策八）之前，**不得把 lark-service 部署到任何泳道**——它抢到的消息会被静默丢弃，没有任何错误信号。切流期间还须确认每个队列只有一个订阅者。
- **切流判据是 `/api/ready` 返回 200，不是 `/api/health`**：飞书 SDK 的 `start()` 只是异步发起重连、**不等待首次连接成功**，所以"进程起来了"完全不代表它在接飞书事件。两个端点职责已分开——`/api/health` 恒 200（liveness，重连抖动不该触发重启），`/api/ready` 在 `connected !== expected` 时返回 503。用错端点会造成"旧的已停、新的没连上"且无告警的静默断流窗口。
- **必须实际触发一次"首次认领"**：身份与会话的收敛靠两条手写 `ON CONFLICT ... COALESCE`（`lark_user_open_id` / `lark_base_chat_info`）。它们的 SQL 由真 TypeORM 生成并被断言，但开发机连不到库，**从未在真 PG 上执行过**。泳道验证必须包含一个此前没见过的用户或会话，让这两条语句真的跑一次。
- **Deliverable**: 泳道验证记录（命令 + 实际输出）；四个入口/owner 的切换与回滚执行记录。
- **Verification**: 泳道内走通完整飞书往返（收消息、AI 回复、指令、撤回），lane 跨服务不丢；切换过程中确认四个 owner 各自唯一——WS 长连只有一个持有者、webhook 只指向一个服务、`inbound_lane.{lane}` 只有一个订阅者、定时任务只有一个执行者；切换后旧路径零流量、新路径全量，且期间无消息丢失或重复发送的证据。

**Task F — 清理与边界收口**
- **Goal**: 在切换稳定后删除 channel-server 的飞书代码，关闭 cutover 窗口。
- **Deliverable**: 残余飞书代码（plugins/lark、types/、lark_* 实体、mongo 飞书方法、infrastructure/integrations/lark*）删除；`?? 'lark'` 兜底清除；`core/boundary.test.ts` 守卫范围按新边界更新。
- **Verification**: 明确写出"切换已稳定"的判据并确认满足后才动手；`grep -ri lark apps/channel-server/src` 的每条命中都能解释为非飞书含义；channel-server 全量测试绿；边界守卫在故意违规时确实转红（变异验证，不能只看绿）。
