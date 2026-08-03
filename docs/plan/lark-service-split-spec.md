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
| | QQ 事件 | 同上 | QQ 对应事件 | channel-server |

**三处必须被测试固化的重叠**：

1. **`common_agent_response` 的 pending 行有两个 INSERT 方**（chat_request 组装侧和 agent-service 的 fan-out 侧），而该表有 `session_id` 唯一约束。两侧生成 session_id 的方式不同，现有代码里没有找到明确的仲裁逻辑。拆分不改变这个形态，但会让 INSERT 方从 2 个变成 3 个（飞书、QQ、Python 各一），必须确认唯一约束的冲突语义。
2. **`safety_status` 被 recall worker 和 agent-service 双向写**，且该表**没有 `channel` 列**——DB 层无法拒绝越界写入。拆分后飞书的 recall 移到 lark-service，隔离完全依赖 agent-service 的 routing key 分对了。这是决策二要求消费侧 fail-closed 的直接原因。
3. **`common_message` 的 user 行与 `lark_message` 同事务写入**。共库让这个事务得以保留（决策一），但矩阵里它是唯一一处"common_* 与渠道私有表在同一事务"的写入，任何试图把 common_* 的写入上收到共享层的重构都会打破它。

## Data & deployment impact

- **无 schema 变更，无新表，无 DDL**。表的物理位置与结构完全不动，只换代码所有者。lark_* 七张表 + Mongo `lark_event` 归 lark-service 独占；common_* 五张表三方共写，写入矩阵见决策二。
- **无 prompt 变更、无 Dynamic Config 变更**（flag 语义不变，读取方从 channel-server 变成 lark-service）。
- **新服务要在 PaaS 注册**：ImageRepo、App envs、ConfigBundle `required_keys` 覆盖新 app、Deployment 与 Service。飞书凭据只下发给 lark-service。
- **资源增量要核算**：多一个服务意味着多一份 DB / Redis / MQ 连接池和一组消费者。需要给出新增连接数、消费者 prefetch 与队列积压的观测口径，避免拆完打爆连接上限。
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
- **Goal**: 10 条飞书指令、photo/meme/callback、daily-photo 与 emoji 定时任务在 lark-service 内正常工作。
- **Deliverable**: 上述业务在 lark-service 下的实现（按决策六，先有行为断言测试再实现）；`lark_emoji` 仓储随之迁移。
- **Verification**: 指令与服务测试在新服务下全绿；定时任务的 lane gate 行为保持（非 prod 部署不启动）；与拆分前的行为差异逐项列出并解释。

**Task E — 泳道验证与生产切换**
- **Goal**: 证明拆分后飞书链路端到端行为与拆分前一致，并在不丢消息、不双跑的前提下完成生产切换。**此阶段 channel-server 的飞书代码仍在，回滚是配置级的。**
- **Deliverable**: 泳道验证记录（命令 + 实际输出）；四个入口/owner 的切换与回滚执行记录。
- **Verification**: 泳道内走通完整飞书往返（收消息、AI 回复、指令、撤回），lane 跨服务不丢；切换过程中确认四个 owner 各自唯一——WS 长连只有一个持有者、webhook 只指向一个服务、`inbound_lane.{lane}` 只有一个订阅者、定时任务只有一个执行者；切换后旧路径零流量、新路径全量，且期间无消息丢失或重复发送的证据。

**Task F — 清理与边界收口**
- **Goal**: 在切换稳定后删除 channel-server 的飞书代码，关闭 cutover 窗口。
- **Deliverable**: 残余飞书代码（plugins/lark、types/、lark_* 实体、mongo 飞书方法、infrastructure/integrations/lark*）删除；`?? 'lark'` 兜底清除；`core/boundary.test.ts` 守卫范围按新边界更新。
- **Verification**: 明确写出"切换已稳定"的判据并确认满足后才动手；`grep -ri lark apps/channel-server/src` 的每条命中都能解释为非飞书含义；channel-server 全量测试绿；边界守卫在故意违规时确实转红（变异验证，不能只看绿）。
