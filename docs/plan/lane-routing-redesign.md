# 泳道分流重新设计：从「入口分流」到「统一处理层之后分流」

> 状态：设计稿 · 日期：2026-05-31
> **本文取代 `docs/plan/channel-proxy-redesign.md`**。那一版把 channel-proxy 退化成「薄的平台无关入口泳道路由」——本质仍是「在入口查泳道、再转发到目标 lane 的 channel-server」，还是入口分流的旧思路，方向已被用户否定。旧文档保留不动，仅作为 channel-proxy 现状的事实参考。
> 配套阅读：`docs/plan/channel-layer-redesign.md`（平台无关 core + 进程内插件，提供本设计要用的统一用户体系模型）、`docs/service-topology.md`（现状拓扑）。

---

## 0. 一句话

把泳道分流从「飞书 webhook 入口」搬到「消息已经被翻译成统一用户体系之后」。新链路：飞书 webhook 经 **api-gateway 直达 prod channel-server**（取消 channel-proxy 独立服务），channel-server 的飞书插件接住原始 webhook、解密验签、解析成平台无关的 `InboundMessage`（统一 User / Conversation / Bot / Message），处理核心基于这些**统一概念**算出该走哪个 lane，然后**通过 MQ 把消息投给目标 lane 的 channel-server 消费处理**。分流天然平台无关——决策只看统一概念，不再去飞书消息体里抠字段。

---

## 1. 现状拆解（基于真实源码）

### 1.1 现在泳道怎么走：入口分流

泳道在飞书 webhook 进门的第一跳（channel-proxy）就被决定。完整链路（与 `docs/service-topology.md` §三一致）：

```
飞书 → api-gateway (default-channel-proxy-webhook 规则, /webhook/ → channel-proxy:3003, target.lane 留空)
     → channel-proxy:3003 (/webhook/:bot/event 或 /card)
     → 飞书 SDK 解密验签 → 抠 chat_id → 查 lane_routing 算出 lane
     → 注入 x-ctx-lane，转发到 channel-server /api/internal/lark-event
     → channel-server 读 x-ctx-lane 注入 context（sidecar 据此路由到 channel-server-{lane}）
     → 规则引擎 → 存储 → publish chat_request → agent-service → ...
     → safety_check / vectorize / recall / chat_response 队列（带 lane 后缀）
     → chat-response-worker → 飞书回复
```

channel-proxy 是这套机制的命根：它单拎出来独立部署，就是为了「接飞书 webhook + 决定打到哪个 lane 的 channel-server」这薄层保持稳定——channel-server 重、更新频繁会重启丢消息，所以把分流入口隔离出去。

### 1.2 channel-proxy 到底做了什么（逐文件源码实证）

源码在 `apps/channel-proxy/src/`，9 个文件。核心三件是 `bot-manager.ts` / `lark-adapter.ts` / `forwarder.ts`：

**入门 + 解密验签**（`bot-manager.ts` + `lark-adapter.ts`）：proxy 从 `bot_config` 读 `channel='lark'` 的 bot，按 `init_type` 分 http / websocket。http bot 注册两条飞书专属路径 `/webhook/{bot}/event` 和 `/webhook/{bot}/card`，挂飞书 SDK 的 `EventDispatcher` / `CardActionHandler`；`adaptHono` 用 bot 的 `encrypt_key` + `verification_token` 做 AES 解密 + SDK 验签，并应 `url_verification` challenge。websocket bot 起 `Lark.WSClient` 长连接直连飞书。**proxy 必须持有每个飞书 bot 的四件套凭据，import 飞书 SDK，知道 12 个飞书事件类型名**——这些全是平台知识，正是用户要颠覆的「入口懂飞书」。

**抽 chat_id + 查 lane + 转发**（`forwarder.ts`，载荷代码，逐字引用）：

```ts
private async doForward(eventType, botName, params): Promise<void> {
    const chatId = this.extractChatId(params);
    const lane =
        (chatId ? await this.laneResolver.resolve('chat', chatId) : null) ??
        (await this.laneResolver.resolve('bot', botName));
    // ...
    const headers = { 'X-App-Name': botName, 'x-trace-id': traceId, Authorization: `Bearer ${secret}` };
    if (lane && lane !== 'prod') { headers['x-ctx-lane'] = lane; }
    await fetch(`${CHANNEL_SERVER_BASE}/api/internal/lark-event`, {
        method: 'POST', headers, body: JSON.stringify({ event_type: eventType, params }),
    });
}
// extractChatId 钻飞书事件结构：message.chat_id → p.chat_id → context.open_chat_id
```

四个实证要点：① 决策依据是 **chat_id（钻飞书消息体）优先、bot_name 兜底**，这就是飞书字段泄漏到分流层、无法平台无关的根；② 转发体是 **`{event_type, params}`（飞书 SDK 产物），不是原始字节**；③ 透传 header 是 **`x-ctx-lane`**（不是 `x-lane`），且**只在 `lane && lane !== 'prod'` 时才带**——prod 流量不带 lane header，由 sidecar 默认打到 prod 实例；④ 转发是 **fire-and-forget**（`forward()` 不 await，SDK handler 立即 return `{}`），proxy 不等 channel-server 处理结果。

### 1.3 lane_routing 真实表结构 + 数据来源（源码实证，纠正旧文档）

**数据来源是 PostgreSQL `lane_routing` 表，channel-proxy 直接连 PG 查，不经过 lite-registry。** （CLAUDE.md「泳道路由」段把 lane_routing 描述为来自 lite-registry，那是另一套机制——lite-registry 的 `GET /v1/routes` 是给 **lane-sidecar / api-gateway** 做「服务名→泳道实例」网络层改写用的服务发现表，与 channel-proxy 查的业务维度 `lane_routing` 表是两回事，别混。）

真实表结构从 `lane-resolver.ts` 的查询 + `admin.ts` 的 upsert 反推：

```sql
-- lane-resolver.ts 读：
SELECT lane_name FROM lane_routing WHERE route_type = $1 AND route_key = $2 AND is_active = true
-- admin.ts 写（upsert）：
INSERT INTO lane_routing (route_type, route_key, lane_name, is_active) VALUES ($1,$2,$3,true)
  ON CONFLICT (route_type, route_key) WHERE is_active = true DO UPDATE SET lane_name = $3
```

| 列 | 取值 | 含义 |
|---|---|---|
| `route_type` | `'bot'` \| `'chat'` | 绑定维度 |
| `route_key` | bot_name（route_type=bot）或 chat_id（route_type=chat） | 绑的具体对象 |
| `lane_name` | lane（如 `ppe-foo`，`prod` 表示默认） | 路由目标泳道 |
| `is_active` | bool | 软删除标记 |

决策缓存：`LaneResolver` 内存缓存，**TTL 30s**（`lane-resolver.ts: TTL = 30_000`），cache key 是 `${routeType}:${routeKey}`。

> **实证缺口（如实标注）**：本仓库的 channel-proxy 源码完整可读，上面的表结构、维度、TTL、决策优先级都是**真实源码实证**。但 `lane_routing` 表的**真实样例数据、各 lane 当前有多少绑定、bot 维度 vs chat 维度的实际使用比例**需连真库查——本环境 ops-db（`.claude/skills/ops-db/query.py`）走 `$PAAS_API/dashboard/api/ops/db-query`，实测返回 HTTP 500（开发机到该 API 此刻不通），无法取样例数据。这列为 §9 Task 1 的实现前必补项。

### 1.4 绑定怎么写：admin + /ops bind

`admin.ts` 暴露 `/api/lark/lane-bindings`（X-API-Key 鉴权），GET 列绑定 / POST upsert / DELETE 软删，写完即 `laneResolver.clearCache()`。`/ops bind bot dev <lane>`（CLAUDE.md）最终落到的就是 `route_type='bot', route_key=<botName>, lane_name=<lane>` 这一行。外部经 api-gateway 的 `default-channel-proxy-lark` 规则（`/api/lark/` → channel-proxy）访问。**这套绑定管理面随 channel-proxy 取消必须一起迁走，否则 `/ops bind` 和 dashboard 绑定管理会断——迁移方案见 §5.6、独立 task 见 §9 Task 6。**

### 1.5 x-ctx-lane 链路现在贯穿哪些服务

`x-ctx-lane` 是「入口算出的 lane」往下游传播的载体：

```
channel-proxy  ── 算出 lane(非 prod)，set header x-ctx-lane ──▶
api-gateway/sidecar ── 据 x-ctx-lane 把 channel-server 改写到 channel-server-{lane} ──▶
channel-server ── 读 x-ctx-lane 注入到 context ──▶
agent-service  ── 读 context.lane，出站请求由 sidecar 按 lane 改写服务名 ──▶
MQ 队列        ── 队列名带 lane 后缀，按 lane 隔离消费
```

证据：`forwarder.ts`（set x-ctx-lane）、`docs/service-topology.md` §五（sidecar 读 x-ctx-lane 改写服务名 + api-gateway 盖 lane header）、§三（channel-server 注入 x-lane 到 context）。

### 1.6 MQ 队列怎么用 + 怎么按 lane 隔离

跨服务异步全靠 RabbitMQ（`docs/service-topology.md` §四）。与本设计相关的队列：

| 队列 | 生产者 | 消费者 | 干什么 |
|---|---|---|---|
| `chat_request` | channel-server | agent-service | 请赤尾回这条消息 |
| `chat_response` | agent-service | chat-response-worker | 赤尾的回复，发飞书 |
| `recall` | agent-service | recall-worker | 撤回 |
| `vectorize` | channel-server | vectorize-worker | 消息向量化 |

**所有队列都带泳道后缀 `xxx_<lane>`，泳道队列有 10s TTL，过期后消息降级回 prod 队列**（`docs/service-topology.md` §四末），这保证未部署泳道的服务自动 fallback 到线上。`make deploy LANE=ppe-xxx` 起的 lane channel-server / 各 worker 消费的是带自己 lane 后缀的队列，所以只处理属于自己 lane 的消息——这是现状已经验证可用的 lane 隔离手段。

一镜像多服务（CLAUDE.md / topology §七）：channel-server 镜像产出 `channel-server`（HTTP）、`recall-worker`、`chat-response-worker` 三个独立 Deployment；agent-service 镜像产出 `agent-service` + `vectorize-worker`。

### 1.7 泳道决策依据小结

现状「这条消息走哪个 lane」的算法（`forwarder.ts`）：

```
chat_id 命中 lane_routing(route_type=chat)  >  bot_name 命中 lane_routing(route_type=bot)  >  prod(默认)
```

chat 维度依赖钻飞书消息体抠 chat_id——这正是「入口分流」绑死飞书的地方。

---

## 2. 新模型：统一处理层之后分流

### 2.1 核心转变

| 维度 | 现状（入口分流） | 新模型（处理层之后分流） |
|---|---|---|
| 分流发生在哪 | webhook 进门第一跳（channel-proxy） | prod channel-server 把消息翻成统一概念之后 |
| 分流依据 | 飞书原始字段（钻 chat_id）+ bot_name | 平台无关统一概念的全局 ID（本期 Bot 维度；Conversation 维度待身份迁移后重建） |
| 分流是否平台无关 | 否，必须抠飞书消息体 | 是，只看统一概念 |
| 跨 lane 投递方式 | HTTP 同步转发（fire-and-forget）到 `channel-server-{lane}` | MQ 异步投递到目标 lane 的消费者 |
| webhook 入口 | 独立 channel-proxy 服务（解密验签 + 抽 chat_id + 查 lane + 转发） | 取消 channel-proxy；api-gateway 直达 prod channel-server，飞书入站逻辑收进 plugins/lark |

为什么这个转变成立（用户原话提炼）：**「统一转成统一用户体系后再去划分泳道」**。一旦消息被插件翻译成 `InboundMessage`，泳道决策就拿到了全局 User / Conversation / Bot ID 这些跨平台稳定标识，再去飞书消息体抠 chat_id 就成了多余。分流逻辑因此天然平台无关，未来接企微 / web 渠道时**分流代码零改动**。

### 2.2 新数据流（mermaid）

```mermaid
flowchart TD
  FS[飞书 / 未来其他平台] -->|webhook| GW[api-gateway<br/>webhook 规则直转, 不查 lane]
  GW -->|固定打到 prod| CSPROD[prod channel-server]

  subgraph CSPROD_PIPE[prod channel-server 入站处理管线]
    VERIFY[飞书插件 verify + parse<br/>解密验签 → InboundMessage] --> IDENT[IdentityResolver<br/>渠道内 ID → 全局 ID]
    IDENT --> UC[统一概念<br/>Conversation / User / Bot 全局 ID]
    UC --> LANEDEC[泳道决策 resolveLane<br/>查 lane 路由数据, 只看统一概念]
  end
  CSPROD --> VERIFY

  LANEDEC -->|lane == prod| LOCAL[prod channel-server 本地继续处理<br/>不发跨 lane MQ]
  LANEDEC -->|lane != prod| DISPATCH[投递到 lane 入站队列<br/>inbound_lane.{lane}]

  DISPATCH --> MQ[(RabbitMQ)]
  MQ --> CSLANE[lane channel-server<br/>消费 inbound_lane.{lane}]

  LOCAL --> PIPEPROD[现有处理: 规则引擎 → 存储 → chat_request_prod<br/>→ agent-service → chat-response-worker → 回复]
  CSLANE --> PIPELANE[现有处理: 规则引擎 → 存储 → chat_request_{lane}<br/>→ agent-service-{lane} → chat-response-worker → 回复]

  PIPEPROD --> FS
  PIPELANE --> FS
```

关键点：泳道决策**只有一个点**（prod channel-server 管线内，IdentityResolver 之后）。决策之后要么本地继续（prod 消息），要么跨 lane 投递一次（非 prod 消息）。投递之后，目标 lane 的 channel-server 走的是**和现状完全一样的 per-lane 处理管线**（规则引擎 → chat_request_{lane} → ... → 回复），新模型**不动这条已验证的下游链路**。

出站（回复 / 卡片 / 撤回）在哪个实例发生：**入站分流只解决「消息投到哪个 lane 处理」，出站由处理它的那个 lane 自己的实例直接发飞书，不回 prod 出口。** 非 prod 消息被投到 `inbound_lane.{lane}` 后，由该 lane 的 channel-server / chat-response-worker / recall-worker 走自己 lane 的 `chat_request_{lane}` → `chat_response_{lane}` / `recall_{lane}` 管线，回复 / 卡片 / 撤回**从 lane 实例直接发回飞书**。这是现状已有的能力——每个 lane 部署都带自己的 chat-response-worker / recall-worker，且持飞书出站凭据（出站凭据由 channel-server 侧持有，§5.2 迁移后 prod 和 lane 实例各自持有），出站路径沿用现状、不经 prod 中转。`inbound_lane.{lane}` 只换了「入站消息怎么到 lane」，出站这一段完全不变。

---

## 3. 泳道决策点设计

### 3.1 决策放在哪一步

放在 prod channel-server 入站契约链里、**`IdentityResolver` 把渠道内 ID 换成全局 ID 之后、规则引擎/存储/发 chat_request 之前**。

为什么是这个点：早一步（插件 parse 内 / IdentityResolver 前）还只有渠道内 ID（飞书 chat_id），决策就会被迫碰飞书字段、退回旧入口分流；晚一步（已经发了 prod 的 chat_request / 已存库）就已经在 prod lane 干活了，再分流要回滚副作用。IdentityResolver 之后是「拿到全局 ID、还没产生任何 lane 副作用」的唯一窗口。

现状契约链顺序（`docs/service-topology.md` §三）是 `adapter.parse → AddressingPolicy → IdentityResolver → 规则引擎 → 存储 → 发 chat_request`。决策点插在 IdentityResolver 和规则引擎之间：

```
webhook raw
  → 飞书插件 verify + parse  → InboundMessage（渠道内 ID）
  → IdentityResolver         → 全局 Conversation / User / Bot ID
  → 【泳道决策】resolveLane(全局概念) → lane
  → 分叉：
       lane == prod  → 规则引擎 → 存储 → chat_request_prod（现状链路）
       lane != prod  → 投 inbound_lane.{lane} 队列，prod 这边到此为止
```

### 3.1a 铁律：分流点之前禁止任何持久化 / 外呼副作用

**这是新模型最致命的正确性约束：对要走非 prod lane 的消息，prod channel-server 在分流决策点之前不得产生任何持久化或外呼副作用（落库、presence、外呼飞书、ack 业务语义、向量化等）。** 否则 lane 消息会先在 prod 库里落一份、写一次状态，lane channel-server 再落一份 = 双写双处理，prod 库被 lane 流量污染、状态错乱。

为什么这条不是空话——现状真实风险（源码实证，`apps/channel-server/src/infrastructure/integrations/lark/events/handlers.ts` + `apps/channel-server/src/api/routes/internal-lark.route.ts`）：现在飞书入站事件抵达 channel-server 后，`handleMessageReceive` 里**在钉死的渠道契约链（`runInboundContractChain`，含 IdentityResolver、即未来的分流决策点）之前**就已经触发了多个飞书副作用——

- 图片管线：`message.allowDownloadResource()` 命中时，逐个 `imageKey` 调 `toolClient.post('/api/image-pipeline/process', ...)`（外呼 tool-service 下载/压缩图片、落 TOS），发生在契约链之前；
- `bot_chat_presence` 在线状态写库：`handleMessageReceive` 里对 `BotChatPresence` 的 `upsert`（注释标「飞书 native 渠道专属副作用，先做」），同样在契约链之前；机器人入群/退群事件（`handleChatRobotAdd` / `handleChatRobotRemove`）也直接写 `bot_chat_presence`；
- `internal-lark.route.ts` 的 `insertEvent(params)`：MongoDB 事件审计落库（fire-and-forget），发生在 handler 之前；
- 路由层立即 `c.json({ ok: true })` 的 ack。

这些副作用现在全部跑在 IdentityResolver / 分流决策点之前。新模型若原样保留，非 prod lane 的消息就会在 prod channel-server 上跑图片管线、写 presence、写 MongoDB 审计——lane 消息在 prod 留痕、状态被 lane 流量串改。

**落地口径**（二选一或组合，Task 4/Task 2 实现时按副作用逐项定）：要么把这些副作用**推迟到分流决策点之后、且只在 prod 分支做**（lane 分支只投 MQ，副作用由 lane channel-server 消费后在自己 lane 重做）；要么明确逐项裁定「哪些副作用对要走 lane 的消息在 prod 发生是被禁止的」并在决策点前跳过。图片管线这类「下游可能需要其产物」的副作用尤其要想清楚：它当前排在契约链之前，若推迟到分流后，prod 分支和 lane 分支都要各自重做一次（lane 消息的图片处理在 lane channel-server 做）。**这要求一次「副作用相对分流点的位置审计」**——把 webhook 到达后、分流决策前发生的每一个副作用列出来，逐项判定其对 lane 消息是否允许在 prod 发生，不允许的前移到决策点之后（见 §9 Task 4 的副作用前移审计子项）。唯一允许在分流点之前发生的，是无业务持久化、无跨 lane 污染的纯协议动作（验签解密、challenge 应答、传输层 ack）。

### 3.2 用什么统一概念查什么数据

决策函数签名（概念，非最终代码）：`resolveLane(channel, botGlobalId) -> lane`，**只读全局统一字段，绝不接触飞书原始 body**。

**本期泳道分流只做 bot 维度**（基于全局 Bot 概念），决策优先级：

```
bot 维度命中  >  prod(默认)
```

为什么本期只做 bot 维度：现状的 chat 维度 route_key 是飞书裸 chat_id，要平台无关就得换成全局 Conversation ID，而全局 Conversation ID 来自身份迁移（channel-layer-redesign 线）。身份迁移尚未完成前，conversation 维度无法用全局 ID 表达，硬切就会被迫回填飞书裸 chat_id 或退回碰飞书字段。**所以本期不做 chat/conversation 维度细粒度绑定，也不回填现有 chat 裸 id 绑定**；等身份迁移完成后，再基于全局 Conversation ID 重建 conversation 维度绑定（§9 backlog）。bot 维度本就不依赖 chat_id，平台无关，开箱即用。

lane_routing 表的语义随之收敛：本期只用 `route_type='bot'`，`route_key` 存 bot 标识（全局 Bot 概念）。现存 chat 维度绑定本期**不迁移、不回填、不参与决策**，留待身份迁移后重建。（user 维同理：现状没有 user 维，本期也不为「未来可能按 user 分流」预先加——按 `feedback_over_abstraction_for_future`，等真实需求来再加。）

> **平台无关红线**：决策函数入参里**不允许出现** `chat_id`/`open_id`/`event`/`sender` 这类飞书字段名。本期决策只认全局 Bot 概念；飞书插件 + IdentityResolver 负责把渠道内标识映射成全局 ID（bot 标识本期即用，`chat_id → 全局 Conversation ID` 等映射待身份迁移后服务 conversation 维度重建）。决策层只认全局 ID。这正是新模型相对旧 channel-proxy-redesign 的本质进步——旧设计还在「入口抽 routing key」，注定要碰平台字段。

### 3.3 缓存

沿用现状的内存缓存思路（`LaneResolver` 已验证，TTL 30s），缓存对象由 prod channel-server 持有，决策走本地缓存查询，不为每条消息打 DB。

**主动失效能力不能丢。** 30s TTL + bot 维度路由意味着「改了某 bot 的 lane 绑定后，最多 30s 内仍可能按旧 lane 分流」的窗口。现状 channel-proxy 的 admin 写绑定后立即调 `laneResolver.clearCache()` 主动失效（`admin.ts`），把这个窗口压到接近零。迁移到 channel-server 后，**绑定变更（admin 改绑定 / `/ops bind`）必须同样主动失效 prod channel-server 的 lane 决策缓存**——这个 clearCache 能力随 admin API（lane-bindings）一起从 channel-proxy 迁到 channel-server（§5.6），管理面与决策读取方同进程、clearCache 本进程直接调用，迁移时不能丢，否则改绑定后要干等 30s 才生效。admin API 迁移与缓存失效是独立一条 task（§9 Task 6），其验收里要断言「绑定变更后清缓存生效」。

---

## 4. MQ 分发设计（队列 + 防双跑）

### 4.1 新增什么队列：入站分发队列 `inbound_lane.{lane}`

新模型只需要**一类新队列**：**入站 lane 分发队列**，命名 `inbound_lane.{lane}`。

- prod channel-server 算出 `lane != prod` 时，把 **`InboundMessage`（已平台无关、已带全局 ID 的统一消息，不是飞书原始 body）** 投到 `inbound_lane.{lane}`。
- 目标 lane 的 channel-server 起一个消费者订阅**且只订阅** `inbound_lane.{自己的lane}`，消费到就接着走自己 lane 的处理管线（规则引擎 → 存储 → chat_request_{lane} → ...）。

投递「已解析的统一消息」而不是飞书原始 body 是关键：跨 lane 边界传的是平台无关结构，lane channel-server 不需要再认飞书、不需要重新验签 parse、不需要持飞书凭据。verify/parse/身份解析只在 prod channel-server 做一次。

### 4.2 prod 自己的消息不发 MQ

`lane == prod` 时，prod channel-server **本地直接继续处理**（接着走规则引擎链路），不发 `inbound_lane.prod`。理由：prod 是绝大多数流量，多一跳 MQ 纯属浪费 + 增加丢消息面；消息已经在 prod channel-server 手里，它本就是处理 prod 流量的进程。所以 `inbound_lane.*` 队列**只为非 prod lane 存在**。

### 4.3 lane channel-server 怎么只消费自己 lane

靠**队列名带 lane** 实现，和现状 `chat_request_{lane}` / `vectorize_{lane}` 隔离机制同构：`channel-server-ppe-foo` 这个 Deployment 的入站消费者只消费 `inbound_lane.ppe-foo`，物理上看不到别的 lane 的消息。直接复用现状已跑通的隔离范式。

### 4.4 防双跑 / 防重复消费（重点，吸取 ppe 双跑教训）

参考 `feedback_ppe_lane_cron_pollution`（部带循环的服务到 ppe-* 双跑污染 prod 的事故教训），从设计上堵死双跑：

1. **入站分发只有一个生产者**：只有 prod channel-server 算 lane + 发 `inbound_lane.{lane}`。lane channel-server **绝不**自己再算 lane、再分发——它只消费、只处理。生产者单点，从源头杜绝「两个进程都在分发」。

2. **入站分发只有一个消费者群**：每个 `inbound_lane.{lane}` 只被对应 lane 的 channel-server 消费；prod channel-server **不消费任何 `inbound_lane.*`**（prod 走本地，§4.2）。

3. **下游 per-lane 队列维持现状隔离**：`chat_request_{lane}` / `vectorize_{lane}` 等已按 lane 后缀隔离 + 10s TTL fallback，新模型不碰。lane channel-server 消费到 `inbound_lane.{lane}` 后，发的是 `chat_request_{lane}`（自己 lane 的），不会串到 prod。

4. **过渡期最危险的双跑场景**：cutover 期间如果 channel-proxy 旧的「入口分流」还活着（仍查 lane、仍直接 HTTP 打到 lane channel-server），同时 prod channel-server 新的「处理层分流」也上了，**同一条消息会被分流两次**（旧 proxy 直接打到 lane channel-server 的 HTTP 入口，新 prod channel-server 又投 MQ 给同一个 lane → 重复处理 / 重复回复）。**必须保证旧入口分流和新处理层分流不同时生效**——见 §7 cutover 的「旧入口必须先关」。

5. **幂等兜底（去重边界要到 `event_type + globalMessageId + lane`，单靠现状手段覆盖不够）**：跨 lane 投递引入 MQ at-least-once，消息可能重复投递。但**现状的两个幂等手段覆盖范围有盲区，照搬不够**——必须先看清它们各防什么、漏什么：

   - **`storeMessage` 的 `ON CONFLICT DO NOTHING`**：只防「**重复入库 / 重复向量化**」。它在冲突时只是不再写那一行，但**冲突之后 handler 仍可能继续往下 publish `chat_request`**——也就是说它防不住「同一条消息被处理两遍、回复两遍」。
   - **Redis `make_reply:<messageId>` 60s 锁**：防「**重复回复**」，但只在 60s 窗口内有效。它**兜不住「publish 后未 ack、超过 60s 才被 MQ 重投」**这类路径（锁早过期了，重投会再触发一次回复），也**兜不住卡片回调 / 群成员等非 message_id 主链路的事件**（这些事件没有可作 key 的 message_id）。

   新模型把跨 lane 投递改成 MQ at-least-once 后，上面这些盲区会被**放大**（MQ 重投比现状 HTTP fire-and-forget 更常见）。

   **定论**：入站去重边界明确到 **`event_type + globalMessageId + lane`** 级别——即「某类事件 + 全局 Message ID + 目标 lane」三元组唯一确定一次入站处理，重复投递的同一三元组直接判为已处理、跳过。实现上**或**引入一个「入站事件处理已完成」级别的持久化幂等标记（DB 幂等记录 / outbox 表，按这个三元组建唯一键），不只依赖 `storeMessage` 的行级冲突 + Redis 短锁。（全局 Message ID 来源：现状 channel-server 处理消息已有 message_id 维度，见 commit `43ac3fa` global message_id；channel-layer-redesign 的 Identity 模型里 Message 是全局 UUIDv7。卡片 / 成员等非 message_id 事件如何取稳定的 `globalMessageId` 维度作 key，由 Task 3 实现时按事件类型定，事件矩阵 §5.5 是依据。）

   **文档需说明「重复投递时哪些副作用保证不再执行」**：命中已处理三元组时，**不重复入库、不重复向量化、不重复 publish `chat_request`、不重复回复、不重复触发图片管线 / presence 等副作用**——即整条入站处理对同一三元组是幂等的，而不只是「库里不多一行」。这条要在 §9 Task 3 验收里显式验证。

### 4.5 与现有 MQ 链路的关系：复用，不重叠

现有 `chat_request_{lane}` / `vectorize_{lane}` / `recall_{lane}` / `chat_response_{lane}` 是「**某个 lane 内部的处理流水线**」。新的 `inbound_lane.{lane}` 是「**把消息从 prod 决策点搬到目标 lane 入口**」——两者职责正交、不重叠：前者是 lane 内流水线，后者是 lane 间投递。新模型**只加 `inbound_lane.{lane}` 这一类新队列，下游全部复用现状**，不重写、不重叠。

### 4.6 `inbound_lane.{lane}` 队列语义：fail-closed，绝不复用现状 10s fallback

**这条是 §4.1/§4.3 没说清的一个致命语义缺口，必须钉死。** `inbound_lane.{lane}` 的 TTL / fallback 语义和现状所有 lane 队列的默认行为**正好相反**，照搬现状 helper 会出错。

① **现状 lane 队列的默认行为是「10s 后降级回 prod」**：现状 TS RabbitMQ helper 对所有非 prod lane 队列（`chat_request_{lane}` / `vectorize_{lane}` / `recall_{lane}` / `chat_response_{lane}`）默认配 10s TTL + dead-letter 回 prod routing key（§1.6 / §4.4 point 3，源自 `docs/service-topology.md` §四）。这个语义是给「**lane 服务没部署时，消息自动降级回 prod 处理**」用的——对 lane 内流水线队列是对的：lane 没起来，就让 prod 兜底处理这条消息，不丢。

② **但 `inbound_lane.{lane}` 绝不能用这个语义。** `inbound_lane.{lane}` 里装的是「**已经在 prod 决策点算定该走这个非 prod lane**」的入站消息。如果它也套 10s 回 prod：要么 10s 后这条消息被降级回 prod routing key，**让本该在 lane 处理的消息跑到 prod 处理**（prod 库被 lane 流量污染 + 行为错乱，正是 §3.1a 极力避免的双写双处理）；要么因为 `inbound_lane` 这一类**根本不存在 prod base queue**（§4.2 prod 消息不发 MQ，所以没有 `inbound_lane.prod` 消费者），dead-letter 无处可投而**直接丢失**。两种结局都是错的。

③ **定论：`inbound_lane.{lane}` 一律 fail-closed。** 目标 lane 消费者不在（lane 没部署 / 没起消费者）时，消息**留在队列里等消费者上线，或进 DLQ 告警，绝不自动降级回 prod**。「lane 没部署就让 prod 兜底」这个对 lane 内流水线成立的降级假设，对「跨 lane 入站分发」**不成立**——一条已被判定走非 prod lane 的消息，宁可堆积/告警也不能偷偷在 prod 处理。

④ **实现约束**：`inbound_lane.*` 队列**不套用**现状 helper 给 lane 队列的默认 10s TTL + dead-letter-回-prod 配置——它要么不设 TTL（消息常驻等消费者），要么 dead-letter 到独立 DLQ + 告警，而不是回 prod routing key。这条要在 §9 Task 3 的验收口径里显式验证（见该 task「验证 `inbound_lane` 队列无 10s 回 prod 行为、lane 消费者缺席时消息不落 prod」）。

---

## 5. channel-proxy 取消：webhook 直达 prod channel-server

**定论：取消 channel-proxy 独立服务。** 飞书 webhook 由 **api-gateway 直接打到 prod channel-server**，prod channel-server 解密验签 → 翻成统一体系 → 算 lane → 发 MQ → lane channel-server。这条路里那个独立的 channel-proxy 薄服务**没了**，它原来「接 webhook + 决定打到哪个 lane」的角色直接由 **prod channel-server** 承担。

用户拍板原话：「保留成一个进程，但是做成 **prod channel-server → MQ → lane channel-server**」——这里的「一个进程」指的是分流这件事收敛成 prod channel-server 内部的一段逻辑（§2/§4 已覆盖），不是再保留 channel-proxy 这个独立服务。所以 channel-proxy 服务下线删除，不保留退化版。

### 5.1 webhook 入口怎么走

现状飞书 webhook 已经走 api-gateway（飞书后台回调地址指向 api-gateway，`default-channel-proxy-webhook` 规则 `/webhook/` → `channel-proxy:3003`，源码实证 `apps/paas-engine/internal/service/gateway_rule_seed.go`）。取消 channel-proxy 只需把这条 gateway 规则的 target 从 `channel-proxy:3003` 改成 `channel-server:3000`（走 `/ops gateway upsert` 配置 + `explain` 预览，回滚改回 target 或用 `snapshots`/`rollback`，秒级），**飞书后台回调地址不用改**。cutover 期间 channel-proxy 进程暂留作 gateway 回滚 target；等 prod 稳定后，作为最后一步删除 channel-proxy 的 Deployment、Service、源码，并删除 / 定型 `default-channel-proxy-webhook` / `default-channel-proxy-lark` 这两条旧规则（确认不再有任何规则指向 channel-proxy）。删除顺序见 §7.2。

### 5.2 飞书入站逻辑（验签/解密/challenge）去哪了

channel-proxy 原来用飞书 SDK 做的解密验签、`url_verification` challenge 应答、事件解析，**全部归 prod channel-server 的 plugins/lark 入站**。该入站已实现 `InboundAdapter` 契约的 `handleHandshake`（应 challenge）/ `verify`（验签）/ `parse`（解析成 `InboundMessage`），见 `apps/channel-server/src/plugins/lark/inbound.ts`。

**bot 标识从 URL path 来，解密验签依赖它——先后顺序要澄清。** webhook 到达时载荷还没解密验签，那怎么在验签前知道这是哪个 bot、用哪套凭据解密？答案是 **bot 标识不在加密载荷里，而在 URL path 里**：现状 channel-proxy 就是按 `/webhook/{bot}/event` 和 `/webhook/{bot}/card`（每个 bot 一条回调路径，`bot-manager.ts: registerHttpBot`）从 path 明文拿到 bot_name，再取该 bot 的 `encrypt_key` / `verification_token` 做解密验签。新入口**保留 path 里的 `{bot}`**（沿用 `/webhook/{bot}/event`、`/webhook/{bot}/card`，或等价的 per-bot path），channel-server 据 path 里的 bot 取凭据。

所以完整顺序是：**bot 标识（path，明文）→ 取该 bot 凭据 → 解密 + 验签 → parse 成 InboundMessage → IdentityResolver → 泳道决策**。泳道决策在解密解析**之后**，不在验签前——决策只看全局概念，不存在「验签前先决策」的鸡生蛋。但解密确实依赖「先从 path 知道是哪个 bot」，这是个真实先后依赖：**api-gateway 直达 channel-server 必须保留 per-bot 的 path 路由信息**（`/webhook/{bot}/...` 这层 path 结构不能在 gateway 转发时被抹掉或归并成单一路径），否则 channel-server 拿不到 bot 标识就无法取凭据解密。飞书 bot 凭据（`encrypt_key` / `verification_token` / `app_id` / `app_secret`）随之从 channel-proxy 迁到 channel-server 侧（§9 Task 4）。

但有一处实证缺口要补：现状 `inbound.ts` 的 `verify` 是恒 `true`，注释写明「解密验签在 channel-proxy 入口完成、事件抵达本服务时已解密验证」——也就是说**飞书 SDK 的 AES 解密 + token 验签现在仍在 channel-proxy，不在 plugins/lark**。取消 channel-proxy 后，这部分解密验签逻辑要真正落到 channel-server 侧（让 `verify` 不再恒真、补上解密），不是已经现成的。前提还包括：channel-server 要有一个「收原始飞书 webhook 字节」的统一入口，把原始 body 喂给 plugins/lark 入站——现状 channel-server 收的是 channel-proxy 转过来的 `{event_type, params}`（SDK 产物），不是原始字节。**新建这个原始 webhook 入口 + 把解密验签落到 channel-server 侧，是一个 task**（§9 Task 4，gateway 切流 / 删 proxy 已拆成 Task 7 / Task 8）。验签所需的飞书 bot 凭据（`encrypt_key` / `verification_token` 等）随之从 channel-proxy 迁到 channel-server 侧。

### 5.4 websocket 长连 bot（init_type=websocket）的去向

channel-proxy 不只接 http webhook——它还按 `init_type` 给 `websocket` bot 起 `Lark.WSClient` 长连接直连飞书（`bot-manager.ts: startWebSocketBot`，websocket bot 不走 `/webhook/{bot}` 回调，是 proxy 主动跟飞书建长连收事件）。取消 channel-proxy 后，http webhook 由 api-gateway 直达 channel-server 覆盖，但 **websocket 长连这条接入路径没有自动承接者**。

**实证（ops-db 查 `bot_config` init_type 分布）：当前有 1 个 active 的 `init_type=websocket` bot、5 个 `http` bot。** 也就是说 ws 接入**不是空的**，取消 channel-proxy 必须为这个 ws bot 安排去向，不能当不存在。

**定论：ws 长连接入起在 channel-server 进程内。** channel-server 的 plugins/lark 在进程内起 `Lark.WSClient` 长连接直连飞书，收到事件后喂进同一条 plugins/lark 入站契约链（和 http webhook 殊途同归，解密验签 / parse / IdentityResolver / 泳道决策都复用同一套）。不单拎独立常驻进程——ws 接入和 http webhook 共用一套入站逻辑、共处同一进程，部署单元最少、入站代码只有一份。

诚实写出代价：**ws 长连接起在 channel-server 进程内，channel-server 重启（部署、滚更）会断开长连接**，必须有自动重连——重连未恢复期间 ws bot 的事件会丢。落地时 ws 承载要做到「channel-server 重启后 WSClient 自动重连、重连后不丢消息」（验收口径在 §9 Task 4 的 ws 承接子项体现）。

### 5.5 飞书事件矩阵：每类事件删 proxy 后的去向

§5.1/§5.2 + §9 Task 4 主要讲「原始 webhook → InboundMessage」这条**普通消息**主链路，但 channel-proxy 现在接的**不止普通消息**——删 proxy 后，这些非普通消息事件要么没人接、要么无法按 lane 决策、要么默认全在 prod 执行。这一节把**每类飞书事件删 proxy 后的去向**列清，避免漏接。

**现状查证（源码实证）**：channel-proxy 的 `bot-manager.ts: REGISTERED_EVENT_TYPES` 给每个 bot 的 `EventDispatcher` 注册了 **12 类事件**（http bot 走 `/webhook/{bot}/event`，card 走 `/webhook/{bot}/card`；ws bot 同样这套 dispatcher 经 `WSClient`）。但下游 channel-server 现状只在 `internal-lark.route.ts: EVENT_DISPATCH` 真正派发了其中 **5 类**（receive / recalled / bot.added / bot.deleted / card.action.trigger），其余 **7 类**（用户成员变更 ×3、reaction ×2、chat.updated、p2p_chat_entered）当前**收到即 ack 丢弃、不处理**（route 里命中 `if (!handler)` 分支直接 `ok:true`）。

事件矩阵（删 proxy 后所有事件都进 prod channel-server 的新原始 webhook 入口；「是否参与 lane 分流」指是否经分流决策点投 `inbound_lane.{lane}`）：

| 飞书事件 | 含义 | 现状 channel-server 处理 | 删 proxy 后由谁接 | 是否参与 lane 分流 | 备注 |
|---|---|---|---|---|---|
| `im.message.receive_v1` | 收到普通消息 | `handleMessageReceive` → 契约链 | prod channel-server 新入口 → 契约链 | **是**（走 `inbound_lane` 分流，主链路） | 本期 bot 维度分流的核心对象（§3/§4） |
| `im.message.recalled_v1` | 消息撤回 | `handleMessageRecall` → 契约链 | prod channel-server 新入口 → 契约链 | **prod-only（本期）** | 本期不参与 lane 分流、统一 prod 处理；后续若需让撤回跟随原消息 lane 再评估（Backlog） |
| `card.action.trigger` | 卡片回调（按钮/交互） | `handleCardAction`，源码注释明示「**不进 MQ、不走 lane 分发**，同步返回 toast/card 更新给飞书」 | prod channel-server 新入口，同步处理 | **否（prod-only，按现状语义）** | 卡片回调要同步应答飞书，走 MQ 异步分流会破坏同步语义；现状即 prod-only 同步处理 |
| `im.chat.member.bot.added_v1` | bot 入群 | `handleChatRobotAdd`，只 upsert `bot_chat_presence`，源码注释「**不进 MQ、不走 lane 分发**」 | prod channel-server 新入口 | **否（prod-only，按现状语义）** | 只维护 presence，无对话语义 |
| `im.chat.member.bot.deleted_v1` | bot 退群 | `handleChatRobotRemove`，delete `bot_chat_presence`，「**不进 MQ、不走 lane 分发**」 | prod channel-server 新入口 | **否（prod-only，按现状语义）** | 同上 |
| `im.chat.member.user.added_v1` | 用户入群 | 现状 ack 丢弃（未注册 handler） | prod channel-server 新入口 | **prod-only（本期）** | 本期不参与 lane 分流、统一 prod 处理（现状即未处理，至少维持 ack 语义）；后续若需可评估参与分流（Backlog） |
| `im.chat.member.user.deleted_v1` | 用户退群 | 现状 ack 丢弃 | prod channel-server 新入口 | **prod-only（本期）** | 同上 |
| `im.chat.member.user.withdrawn_v1` | 用户被移出 | 现状 ack 丢弃 | prod channel-server 新入口 | **prod-only（本期）** | 同上 |
| `im.message.reaction.created_v1` | 加表情回应 | 现状 ack 丢弃 | prod channel-server 新入口 | **prod-only（本期）** | 同上 |
| `im.message.reaction.deleted_v1` | 撤表情回应 | 现状 ack 丢弃 | prod channel-server 新入口 | **prod-only（本期）** | 同上 |
| `im.chat.updated_v1` | 群信息变更 | 现状 ack 丢弃 | prod channel-server 新入口 | **prod-only（本期）** | 同上 |
| `im.chat.access_event.bot_p2p_chat_entered_v1` | 进入与 bot 的单聊 | 现状 ack 丢弃 | prod channel-server 新入口 | **prod-only（本期）** | 同上 |
| ws 长连消息（init_type=websocket） | ws bot 收到的事件（事件类型同上 12 类） | proxy 经 `WSClient` 收，喂同一 dispatcher | channel-server 起 `WSClient` 长连，喂同一入站契约链（§5.4） | 同各事件类型对应行 | 当前 1 个 active ws bot（§5.4 ops-db 实证）；ws 只是接入方式不同，事件本身的 lane 去向同上各行 |

**两条要点**：① 删 proxy 后**所有 12 类事件都必须有 channel-server 新入口承接**——不能只接现状已派发的 5 类，否则现状「ack 丢弃」的 7 类会因为没有任何入口而连 ack 都做不了（飞书会重推/告警）。Task 4 的「原始 webhook 入口」要覆盖全部 12 类的接收 + 至少维持现状「ack 丢弃」语义，**不能比现状少接**。② **本期只有普通消息（`im.message.receive_v1`）参与 lane 分流，其余所有飞书事件一律 prod-only（不参与泳道分流）**——上表里 `card.action.trigger` / bot 入退群按现状源码语义本就 prod-only（注释已写「不走 lane 分发」）；撤回 / 用户成员变更 / reaction / chat.updated / p2p_chat_entered 这批非普通消息事件，本期也定论为 prod-only、统一在 prod 处理，不参与泳道分流。后续若有让部分事件参与分流的真实需要，再单独评估（Backlog）。

### 5.6 lane_routing 管理面（admin API）必须随 proxy 一起迁走

删 proxy 不只是删 webhook 入口——`lane_routing` 的**绑定管理 API 现在也在 channel-proxy 里**，不一起迁走会**直接让 `/ops bind` 和 dashboard 的绑定管理断掉**。

**现状查证（源码实证 `apps/channel-proxy/src/admin.ts`）**：channel-proxy 的 `registerAdminRoutes` 暴露 `/api/lark/lane-bindings`（`x-api-key` 鉴权）——`GET` 列活跃绑定、`POST` upsert（`INSERT ... ON CONFLICT ... DO UPDATE`）、`DELETE` 软删（`is_active=false`），且 **POST / DELETE 写完都立即 `laneResolver.clearCache()`** 主动失效本进程的 lane 决策缓存。外部经 api-gateway 的 `default-channel-proxy-lark` 规则（`/api/lark/` → channel-proxy）访问；`/ops bind` 和 dashboard 的调用链是 **`/ops bind` / dashboard → monitor-dashboard → `/api/lark/lane-bindings`（后端在 channel-proxy）**。

**定论：admin API 随 proxy 取消，迁到 channel-server。** 理由——`lane_routing` 是**业务路由表**，而本设计里「读 `lane_routing` 算 lane」的消费者已经从 channel-proxy 搬到了 **prod channel-server**（§3、§7.1「lane_routing 消费方」行）。绑定管理（写 `lane_routing`）+ 缓存失效（清 prod channel-server 的 `LaneResolver` 缓存，§3.3）和这个读取方放在同一个服务里，才能让「改绑定 → 立即清自己进程的缓存」这条主动失效链路成立。若 admin API 留在别处（如 paas-engine），它写完 DB 后**无法直接清 prod channel-server 进程内的决策缓存**，又得引入一条跨服务的缓存失效通知，徒增复杂度。paas-engine 虽是 dynamic-config / config-bundle / gateway-rule 这些管理面的统一后端，但 lane_routing 的读取方在 channel-server，管理面同处 channel-server 才能本进程直接失效缓存，链路最简——所以落 channel-server，不放 paas-engine。

**迁移后调用链怎么变**：`/ops bind` / dashboard → monitor-dashboard → **channel-server 的 `/api/lark/lane-bindings`**。monitor-dashboard 这一跳的目标地址要随之更新；api-gateway 的 `default-channel-proxy-lark` 规则要么改 target 指向 channel-server、要么删除换成指向 channel-server 的新规则（§7.2 清理步骤里一并处理，确认不再有规则指向 channel-proxy）。

**缓存失效机制**：迁到 channel-server 后，`POST` / `DELETE` 绑定写完**必须同样主动清 prod channel-server 的 `LaneResolver` 决策缓存**（和 §3.3 已写的「绑定变更必须主动失效」呼应、和现状 `admin.ts` 的 `clearCache()` 一一对应），不能把这个能力随 proxy 一起删掉，否则改绑定后要干等 30s TTL 才生效。这条要在迁移 task 的验收里断言。

### 5.3 取舍

- 代价：丢掉 channel-proxy 当初拆出来的「稳定接入层」隔离——webhook 接入直接落到「重、更新频繁会重启丢消息」的 channel-server 上。这个风险靠 channel-server 自身的部署节奏 + MQ 下游缓冲 + 全局 Message ID 幂等（§4.4）兜，cutover 阶段重点观察 webhook 丢失率。
- 收益：少一个服务、少一个 Deployment、少一套飞书凭据分发；飞书入站逻辑只在 plugins/lark 一处存在，不再 proxy 和 channel-server 各持一份飞书知识；入口统一收敛到 api-gateway，符合项目「外部暴露走 api-gateway 动态规则」的既定方向。

---

## 6. x-ctx-lane 链路怎么改

现状 `x-ctx-lane` 是「入口算出的 lane 一路透传给下游」。新模型下 lane 在 prod channel-server 才算出，channel-proxy 又已取消，所以入口这一跳的 `x-ctx-lane` 注入彻底消失，lane 改在 prod channel-server 的决策点之后产生：

- api-gateway 转 webhook 到 prod channel-server 时**不带任何 lane header**（它不算 lane），prod channel-server 由 sidecar 默认打到 prod 实例。
- prod channel-server 算出 lane 后：
  - `lane == prod`：本地处理，context.lane = prod。
  - `lane != prod`：把 lane 写进**投到 `inbound_lane.{lane}` 的消息信封**（不是 HTTP header，跨 lane 是 MQ 不是 HTTP），lane channel-server 消费时从信封读出 lane 注入自己的 context。
- 从 lane channel-server 往下（agent-service / 各队列）**沿用现状**：context.lane 驱动 sidecar 改写 `{app}-{lane}` + 发 `*_{lane}` 队列。这一段不改。

净效果：`x-ctx-lane` 作为「入口 HTTP header 跨服务透传」的角色随 channel-proxy 一起消失，取而代之的是「prod channel-server 算 lane → 写进 MQ 消息信封 → lane channel-server 读出注入 context」。lane channel-server 内部及其下游的 lane 传播逻辑保持原样。

---

## 7. Cutover 影响清单 + 回滚顺序

参考 `project_pr228_cutover_failure`（多 channel cutover 翻 4 次的教训：复合 cutover 必须先列影响清单和回滚顺序，线上状态要用证据不用想象）和 `feedback_compound_cutover_plan_first`。这次是「分流位置搬家 + 新队列 + 进程职责变更」的复合 cutover，必须分阶段、可回滚。

### 7.0 最高风险项：webhook 入口切换（单列）

**这次复合 cutover 里唯一的「全局唯一、外部依赖」风险点是 webhook 入口切换，单列在最前。** 好消息是查证后它比想象的轻——现状飞书 webhook **已经走 api-gateway**（飞书 → api-gateway 的 `default-channel-proxy-webhook` 规则 `/webhook/` → `channel-proxy:3003`，源码实证 `apps/paas-engine/internal/service/gateway_rule_seed.go`），**飞书后台回调地址指向的是 api-gateway，不是 channel-proxy**。

所以取消 channel-proxy 的入口切换 = **改 api-gateway 那条规则的 target（`channel-proxy:3003` → `channel-server:3000`），飞书后台回调地址完全不用动**。这把 codex 担心的「飞书后台手工改、回滚分钟级、依赖外部控制台」直接消解：

- 切换走 `/ops gateway upsert`，先 `/ops gateway explain` 预览命中结果，确认 `/webhook/` 落到 channel-server；
- 回滚是改回同一条 gateway 规则的 target（或 `/ops gateway snapshots` + `/ops gateway rollback`），**秒级、纯集群内操作、不碰飞书后台**；
- **cutover 期间 channel-proxy 不立即删**——留着作为 gateway 回滚 target，规则一改回就能让流量重新落到 channel-proxy。等 channel-server 新入口在 prod 稳定后，再单独删 channel-proxy（§7.2 第 5 步 / §9 Task 8「稳定后删 proxy」为最后一步）。

仅有的残余风险是 channel-server 的新原始 webhook 入口本身必须先就绪且验签解密正确（§5.2 / Task 4），切 target 之前已在 coe/ppe 灰度验过。但因为飞书后台回调地址不变、gateway 规则可秒级回滚、proxy 暂留，这一步从「分钟级外部回滚」降级成「秒级集群内回滚」。

### 7.1 爆炸半径清单

| 影响项 | 会不会变 | 说明 |
|---|---|---|
| 飞书后台回调地址 | **不用改** | 现状飞书后台回调地址指向 api-gateway（webhook 已走 gateway，§7.0 实证）。入口切换只改 gateway 规则 target，飞书后台零操作。回滚不依赖外部控制台 |
| api-gateway 规则 | **要改** | `default-channel-proxy-webhook` 的 target 从 `channel-proxy:3003` 改成 `channel-server:3000`，**且强制 target lane=prod、清掉外部传入的 `x-ctx-lane` / `x-lane`**（gateway 默认透传请求 lane，不清则外部可伪造 lane 绕过决策点，§6 / Task 7）；`default-channel-proxy-lark`（lane-bindings 入口）随 admin API 迁移改 target 指向 channel-server 或换新规则（Task 6/7）；走 `/ops gateway upsert` + `explain` 预览 + `snapshots`/`rollback` 秒级回滚 |
| channel-server 原始 webhook 入口 | **新增** | 新建「收原始飞书 webhook 字节」的统一入口，保留 per-bot path（从 path 取 bot 标识 → 解密验签），接 plugins/lark 入站（§5.2 / Task 4）；飞书 bot 凭据从 channel-proxy 迁到 channel-server 侧 |
| 飞书事件矩阵覆盖 | **要补** | 删 proxy 后 channel-proxy 现状注册的 12 类事件都要有 channel-server 入口接、不比现状少接，各事件 lane 去向按矩阵落实（§5.5 / Task 5）|
| lane_routing admin API（lane-bindings）| **要迁** | 绑定管理 + 缓存失效现在在 channel-proxy，随 proxy 取消迁到 channel-server（读写同源、缓存失效本进程直接做），否则 `/ops bind` / dashboard 绑定管理断（§5.6 / Task 6）|
| 分流点之前的副作用 | **要审计 + 前移** | 现状图片管线 / presence 写库 / MongoDB 审计 / ack 都在契约链（含 IdentityResolver = 分流决策点）之前发生（§3.1a 实证）；非 prod lane 消息若原样保留会在 prod 双写双处理。必须逐项审计并把禁止项前移到决策点之后（Task 4）|
| websocket 长连 bot | **要承接** | 当前有 1 个 active `init_type=websocket` bot；channel-proxy 删除后 ws 长连接入起在 channel-server 进程内的 plugins/lark（起 WSClient），channel-server 重启需自动重连不丢消息（§5.4 / Task 4）|
| lane_routing 数据维度 | 收敛到 bot 维度 | 本期只用 `route_type='bot'`；现存 chat 维度绑定不迁移、不回填、不参与决策（§3.2），留待身份迁移后基于全局 Conversation ID 重建 |
| lane_routing 消费方 | 变 | channel-proxy 不再查 lane_routing（服务已删）；改由 prod channel-server 查 |
| MQ 拓扑 | **加** `inbound_lane.{lane}` 一类新队列 | 下游 `*_{lane}` 队列不动 |
| channel-server 进程 | prod 加「算 lane + 分发」逻辑；lane 加「消费 inbound_lane」消费者 | 一镜像多服务，部署 channel-server 要同步 release recall-worker / chat-response-worker |
| channel-proxy 进程 | **暂留 → 最后删** | cutover 期间暂留作 gateway 回滚 target；prod 稳定后才删 Deployment / Service / 源码（无退化版、无兼容层）|
| 旧入口分流 vs 新处理层分流 | **绝不能同时生效** | §4.4 双跑红线 |

### 7.2 推荐 cutover 顺序（每步可独立验证 + 可回滚）

1. **先建新队列基建 + channel-server 原始 webhook 入口**：在 coe-* 独立 lane 把 `inbound_lane.{lane}` 队列 + lane channel-server 的入站消费者跑起来，并把 channel-server「收原始飞书 webhook 字节 → plugins/lark 验签解析」的入口接通，单测分发 / 消费 / 去重 + 验签 challenge。此步不碰 prod、不碰 channel-proxy，零线上影响。
2. **prod channel-server 加「算 lane + 分发」逻辑，但默认旁路**：上线决策代码（只看 bot 维度），用动态配置 flag 控制「是否启用处理层分流」，默认关（仍由 channel-proxy 入口分流）。此步 prod channel-server 重新部署，但行为不变。
3. **灰度切换**：在 coe-/ppe- lane 打开 flag，绑 dev bot，端到端验证「webhook → api-gateway 直达 prod channel-server → prod 验签解析 → 算 bot lane → MQ → lane channel-server → 回复」全链路。
4. **切 webhook 入口到 prod channel-server + 开新分流（同一时刻、不可重叠）**：把 api-gateway 的 `default-channel-proxy-webhook` 规则 target 从 `channel-proxy:3003` 改成 `channel-server:3000`（`/ops gateway upsert` + `explain` 预览），**该规则同时强制 target lane=prod、清掉请求里外部传入的 `x-ctx-lane` / `x-lane`**（gateway 默认透传请求 lane，不清则外部可伪造 lane header 绕过「prod channel-server 才算 lane」的设计，§6 / Task 7）、prod channel-server 分流 flag=on，**同一批切**。**飞书后台回调地址不动**（它本就指 api-gateway，§7.0）。这是双跑红线时刻——target 一旦切到 channel-server，channel-proxy 就收不到 webhook、不再分流，新 MQ 分发接管；必须确认 gateway target 已切、flag=on 严格同时生效，二者之间不留「proxy 仍在接流但新分流已开」的窗口。**channel-proxy 此步暂不删**——留作 gateway 回滚 target（改回 target 即瞬时退回旧入口分流）。
5. **观察 prod 稳定后**，清理：把 api-gateway `default-channel-proxy-webhook` / `default-channel-proxy-lark` 这两条旧规则删除或定型，确认不再有任何规则指向 channel-proxy，再删除 channel-proxy 的 Deployment / Service / 源码（无退化版）。**删 proxy 是整个 cutover 的最后一步**，删之前 gateway 回滚通路一直在。lane_routing chat 维度数据本期不迁移（保留原样、不参与决策），等身份迁移完成后由 conversation 维度重建项处理。

### 7.3 回滚顺序（与上线逆序）

- 回滚第 4 步：把 api-gateway `default-channel-proxy-webhook` 规则 target 改回 `channel-proxy:3003`（`/ops gateway upsert` 或 `snapshots`/`rollback`）+ prod channel-server flag=off，**秒级、纯集群内、飞书后台零操作**地退回现状入口分流。**前提**：① 第 5 步删除 channel-proxy 前不要动手，第 4 步只是把 gateway target 切走、channel-proxy 进程仍在跑，可随时被规则重新指回；② 第 2 步的 flag 旁路设计让 prod channel-server 始终保留「不分流、原样处理」的能力，回滚才是翻 flag + 改 gateway target 而不是回滚部署。**第 5 步删 channel-proxy 之后，第 4 步不再可瞬时回滚**——这是「确认 prod 稳定」才把删 proxy 留到最后的原因。
- `inbound_lane.{lane}` 队列即使没人发也无害，可留待下次。
- 部署 = 杀 Pod = 中断异步任务（CLAUDE.md 铁律）：cutover 各步部署前确认没有正在跑的 rebuild / afterthought，或明确告知会中断。

### 7.4 最大风险

**第 4 步的双跑窗口**：旧入口分流（channel-proxy 仍接 webhook 并查 lane）和新处理层分流若有任何时刻重叠，同一条消息被分两次 → lane channel-server 收到两份 → 重复回复 / 重复落库。注意 webhook 入口切换本身的回滚已降级成「改 gateway target、秒级、不碰飞书后台」（§7.0），但双跑窗口风险仍在——它来自「gateway target 切了 + flag 还没开」或「flag 开了 + target 还没切」这种步骤间错位，不是飞书后台问题。缓解：① 严格同批切（gateway target 切到 channel-server + flag=on，二者之间不留窗口，确保 channel-proxy 不再接流的同时新分流已接管）；② §4.4 的全局 Message ID 幂等去重兜底；③ 先在 coe lane 把这个切换演练一遍再上 prod。

---

## 8. 设计取舍小结（给读者一眼看懂）

最大的取舍是**「多一跳 MQ + 收掉独立接入层，换分流彻底平台无关」**。现状入口分流是同步 HTTP、零额外跳数、且有 channel-proxy 这个独立稳定接入层挡在前面，但代价是分流层必须懂飞书（钻 chat_id、持凭据、import SDK），且 webhook 直接打到目标 lane。新模型让 webhook 经 api-gateway 先落 prod channel-server、分流推迟到统一概念之后，换来分流彻底平台无关（接新渠道分流零改动）+ 飞书入站逻辑收口到 plugins/lark 一处 + 少一个服务，代价有两笔：① 非 prod 流量多一跳 MQ + 引入 at-least-once 重复（用全局 Message ID 幂等兜）；② 取消 channel-proxy 后丢掉独立接入层的稳定性隔离，webhook 直接落到频繁重启的 channel-server（§5.3，靠部署节奏 + MQ 缓冲 + 幂等兜，cutover 阶段重点观察 webhook 丢失率）。对一个「prod 占绝大多数、lane 只是测试/灰度少量流量」的系统，MQ 这笔代价划算——prod 流量根本不过 MQ（§4.2），只有少量 lane 流量吃这一跳；接入稳定性这笔代价用上面三道兜底控制。

---

## 9. 粗颗粒 task 清单（目标 + 产出 + 验收口径）

> 只写目标 / 产出 / 验收口径，不写代码、文件行号、实现步骤——实现细节动手时才生成。
> 本期只做 bot 维度分流 + 取消 channel-proxy。Task 1 是阻塞前置（补实证）。原「取消 channel-proxy」的大 Task 4 颗粒度过粗、且和 §7 回滚策略（删 proxy 必须放到最后）冲突，**拆成 Task 4-8**：原始 webhook 入口 + 真验签（Task 4）/ 事件矩阵覆盖（Task 5）/ admin API 迁移（Task 6）/ gateway 切流强制 prod（Task 7）/ 稳定后删 proxy（Task 8，cutover 最后一步）。依赖关系：Task 2 / Task 3 / Task 4 可在 Task 1 后并行（决策逻辑 vs 队列基建 vs webhook 入口，不碰同一文件）；Task 5 依赖 Task 4 的入口；Task 6 独立可并行；Task 7 依赖 Task 4-6 就绪；Task 8 是删除、必须最后；Task 9（x-ctx-lane）/ Task 10（cutover）串行收口。**删 proxy（Task 8）与 §7.2 cutover 第 5 步、§7 回滚策略严格一致：删之前 gateway 回滚通路一直在。**

**Task 1：补齐 lane_routing 真实数据 + bot 维度绑定现状（阻塞前置）**
- 目标：在能连真库的环境用 ops-db 只读查清 `lane_routing` 当前样例数据——bot 维度当前有多少活跃绑定、都绑到哪些 lane；现存 chat 维度绑定有多少（本期不参与决策，但要确认放着不动不会被新决策误读）；确认 `/ops bind` 实际写入路径与 admin 一致。
- 产出：一段「lane_routing 当前绑定全貌（bot 维度活跃量 + 残留 chat 维度量）」的事实记录，回填本文 §1.3 实证缺口。
- 验收：§3.2 的「本期只按 bot 维度决策、chat 维度留着不动」决策是基于真实绑定量、确认不漏不误读。

**Task 2：泳道决策点（平台无关 resolveLane，本期 bot 维度）**
- 目标：在 prod channel-server 入站契约链的 IdentityResolver 之后落一个泳道决策点，入参只有全局统一概念（channel / 全局 Bot ID），产出 lane，优先级 bot > prod，沿用 30s 缓存。
- 产出：一个平台无关的 `resolveLane` 能力 + 缓存的路由数据读取（只读 `route_type='bot'`）。
- 验收：单测覆盖 bot 命中 + 默认 prod 两档；用例断言决策函数签名 / 实现里不出现任何飞书字段名（平台无关红线可测）；绑定变更后清缓存生效。

**Task 3：入站 lane 分发 MQ 机制（含 fail-closed 队列语义 + 三元组幂等）**
- 目标：prod channel-server 算出 `lane != prod` 时把已解析的 InboundMessage 投到 `inbound_lane.{lane}`；lane channel-server 起消费者只消费自己 lane；prod 消息本地处理不发 MQ；`inbound_lane.{lane}` 队列 **fail-closed**（§4.6：lane 消费者缺席时消息留队列 / 进 DLQ 告警，**不复用现状 10s 回 prod fallback**）；消费侧按 **`event_type + globalMessageId + lane`** 三元组幂等（§4.4 point 5）。
- 产出：入站分发生产者 + lane 入站消费者 + fail-closed 队列配置（无 10s 回 prod）+ 三元组级幂等去重。
- 验收：coe lane 端到端——prod 发一条非 prod 消息，只有目标 lane 消费一次（防双跑可测）；**验证 `inbound_lane` 队列无 10s 回 prod 行为、lane 消费者缺席时消息不落 prod**（§4.6 fail-closed 可测）；**重投同一 `(event_type, globalMessageId, lane)` 不重复处理、不重复回复、不重复触发副作用**（不重复入库 / 向量化 / publish chat_request / 图片管线 / presence，§4.4 point 5 可测）；prod 消息不进 `inbound_lane.*`。

**Task 4：channel-server 原始 webhook 入口 + 真验签解密 + 凭据迁移 + 副作用前移审计 + ws 承接**
- 目标：① 新建 channel-server「收原始飞书 webhook 字节」的统一入口，**保留 per-bot path**（沿用 `/webhook/{bot}/event`、`/webhook/{bot}/card`，从 path 明文取 bot 标识 → 取凭据 → 解密验签），接 plugins/lark 入站（`handleHandshake` / `verify` / `parse`）；把现在还在 channel-proxy 的飞书 SDK 解密验签**真正落到 channel-server 侧**（plugins/lark 的 `verify` 不再恒真、补上 AES 解密），飞书 bot 凭据（`encrypt_key` / `verification_token` / `app_id` / `app_secret`）迁到 channel-server 侧。② **副作用相对分流点的位置审计与前移**（§3.1a 铁律）：把 webhook 到达后、分流决策前发生的每个副作用（图片管线 `/api/image-pipeline/process` 外呼、`bot_chat_presence` 写库、MongoDB `insertEvent` 审计、ack）逐项列出并裁定其对要走非 prod lane 的消息是否允许在 prod 发生，不允许的前移到决策点之后（或挪到 lane 分支重做），保证 lane 消息不在 prod 留痕 / 串状态。③ **websocket 长连 bot 承接（起在 channel-server 进程内）**：当前有 1 个 active `init_type=websocket` bot，ws 接入起在 channel-server 进程内的 plugins/lark（进程内起 `Lark.WSClient` 长连接，不单拎独立常驻进程），事件喂进同一条入站契约链；代价是 channel-server 重启（部署 / 滚更）会断长连接，必须自动重连、重连后不丢消息（§5.4）。
- 产出：channel-server per-bot 原始 webhook 入口 + plugins/lark 真解密验签接通 + 飞书凭据迁移 + 副作用前移 + plugins/lark 进程内 `Lark.WSClient` 长连承接（带自动重连）。本 task **不删 channel-proxy、不切 gateway**（那是 Task 7/8）。
- 验收：飞书 `url_verification` challenge 应答正常；伪造/篡改的 webhook 被 `verify` 拒绝（`verify` 不再恒真）；合法 webhook 经 per-bot path 直达 channel-server、解密解析成 InboundMessage 正常；非 prod lane 的消息在 prod channel-server 分流点之前不产生持久化 / 外呼副作用（图片管线 / presence / 审计落库不在 prod 对 lane 消息发生）；ws bot 的事件经 channel-server 进程内 WSClient 入口正常进契约链；**channel-server 重启后 WSClient 自动重连、重连后不丢消息**。此步在 coe/ppe 灰度验过、不碰 prod、不碰 channel-proxy。

**Task 5：事件矩阵覆盖（删 proxy 后所有飞书事件都有 channel-server 入口接）**
- 目标：按 §5.5 事件矩阵，让 channel-server 新入口承接 channel-proxy 现状注册的**全部 12 类**飞书事件——不能只接现状已派发的 5 类（receive / recalled / bot.added / bot.deleted / card.action.trigger），现状「ack 丢弃」的 7 类（用户成员变更 ×3、reaction ×2、chat.updated、p2p_chat_entered）也要有入口至少维持现状 ack 语义，**不比现状少接**；按矩阵落实各事件的 lane 去向——**本期只有普通消息（`im.message.receive_v1`）走 `inbound_lane` 分流，其余所有飞书事件一律 prod-only（不参与泳道分流）**：卡片回调 / bot 入退群按现状源码语义 prod-only，撤回 / 用户成员变更 ×3 / reaction ×2 / chat.updated / p2p_chat_entered 本期定论也是 prod-only。
- 产出：channel-server 新入口对 12 类事件的完整接收 + 按矩阵的 lane 去向落实（仅普通消息走分流，其余 prod-only / 维持 ack 丢弃）。
- 验收：12 类事件经新入口都不报「无入口」错；普通消息走 `inbound_lane` 分流；其余所有事件 prod-only（卡片回调 / bot 入退群在 prod 同步处理不进 MQ，撤回 / 用户成员变更 / reaction / chat.updated / p2p_chat_entered 不投 `inbound_lane.*`）；现状 ack 丢弃的事件仍正常 ack。

**Task 6：lane_routing 管理面（admin API）迁移 + 缓存失效（§5.6）**
- 目标：把现状在 channel-proxy 的 `/api/lark/lane-bindings`（`x-api-key` 鉴权，GET 列 / POST upsert / DELETE 软删，写完 `laneResolver.clearCache()`）随 proxy 取消**迁到 channel-server**（与读 `lane_routing` 的 prod channel-server 同处，缓存失效本进程直接做，链路最简，§5.6）；更新 `/ops bind` / dashboard 的调用链（monitor-dashboard → channel-server）；绑定变更后主动清 prod channel-server 的 `LaneResolver` 决策缓存（§3.3 呼应）。
- 产出：channel-server 的 lane-bindings admin API（GET/POST/DELETE，沿用 `x-api-key`）+ 绑定写后本进程直接清 `LaneResolver` 缓存 + monitor-dashboard 调用目标更新。
- 验收：`/ops bind` / dashboard 改绑定经 channel-server 落 `lane_routing` 成功；绑定变更后 prod channel-server 决策缓存被主动失效、新绑定接近即时生效（不等 30s TTL）；channel-proxy 删除后绑定管理不断（Task 8 删 proxy 后回归验证一次）。

**Task 7：gateway 切流到 channel-server + webhook 规则强制 prod / 清外部 x-ctx-lane**
- 目标：把 api-gateway `default-channel-proxy-webhook` 规则 target 从 `channel-proxy:3003` 改成 `channel-server:3000`（per-bot path 保留、不抹路径，§5.2），`default-channel-proxy-lark`（lane-bindings 入口）随 Task 6 改 target 指向 channel-server 或换新规则；走 `/ops gateway upsert` + `explain` 预览，`snapshots`/`rollback` 秒级回滚。**webhook 规则必须强制 target lane=prod、清掉请求里外部传入的 `x-ctx-lane` / `x-lane` header**——api-gateway 默认会透传请求 lane，若不清，外部可伪造 `x-ctx-lane` 让 webhook 绕过「prod channel-server 才算 lane」的设计直接打到任意 lane 实例（呼应 §6：lane 只能由 prod channel-server 决策点产生，入口一律不带 lane）。此 task **不删 channel-proxy**（proxy 暂留作回滚 target，删除是 Task 8）。
- 产出：更新后的 gateway webhook 规则（target=channel-server、强制 prod、剥离外部 lane header）+ lane-bindings 规则 target 更新 + explain 预览证据 + 回滚预案。
- 验收：`/ops gateway explain` 显示 `/webhook/` 落到 channel-server、per-bot path 保留；带伪造 `x-ctx-lane` 的 webhook 请求被强制按 prod 处理（lane header 被清、不绕过决策点）；规则可 `snapshots`/`rollback` 秒级回滚到指向 channel-proxy；飞书后台回调地址不变（§7.0）。

**Task 8：稳定后删除 channel-proxy（cutover 最后一步）**
- 目标：prod 观察稳定后（§7.2 第 5 步），删除 channel-proxy 的 Deployment / Service / 源码（无退化版、无兼容层、无 re-export），并删除 / 定型 `default-channel-proxy-webhook` / `default-channel-proxy-lark` 两条旧规则，确认不再有任何规则指向 channel-proxy。**删除前 gateway 回滚通路一直在；删除后第 7 步不再可瞬时回滚（§7.3）——这是把删 proxy 留到最后的原因。**
- 产出：channel-proxy 服务 + 源码删除 + 旧 gateway 规则清理。
- 验收：`grep` 全仓零 channel-proxy 引用残留（含 Dockerfile / Makefile / 构建配置）；`/ops gateway explain` 确认无规则指向 channel-proxy；Task 6 的绑定管理在删 proxy 后仍正常（回归验证）；prod webhook 链路无中断。

**Task 9：x-ctx-lane 链路改造**
- 目标：入口不再注入 x-ctx-lane（channel-proxy 已删，api-gateway 转 webhook 不带 lane header、且 webhook 规则强制清外部传入的 `x-ctx-lane` / `x-lane`，Task 7）；lane 在 prod channel-server 决策点之后产生；跨 lane 时 lane 写进 MQ 消息信封而非 HTTP header；lane channel-server 从信封读出注入 context；下游 sidecar 改写 / `*_{lane}` 队列逻辑不变。
- 产出：新的 lane 传播路径（决策点 → 消息信封 → lane context）。
- 验收：lane channel-server context.lane 正确、其下游 agent-service 仍按该 lane 路由、发的是 `*_{lane}` 队列；prod 路径 context.lane=prod；外部伪造的 lane header 不影响决策（被 Task 7 gateway 规则剥离）。

**Task 10：分阶段 cutover + 旁路 flag**
- 目标：按 §7.2 顺序上线，prod channel-server 带「是否启用处理层分流」动态 flag（默认旁路 = 现状行为），先 coe/ppe 灰度，最后「gateway webhook target 切到 channel-server（强制 prod、清外部 lane header，Task 7）+ flag=on」**同批切、不留窗口**；channel-proxy 暂留作 gateway 回滚 target，保留「改回 gateway 规则 target + flag 翻转」即回滚的能力；prod 稳定后才执行 Task 8 删 channel-proxy（删之前回滚通路一直在）。
- 产出：cutover 执行 + flag 控制 + 回滚预案。
- 验收：flag=off 时行为等同现状（回滚可测）；切换那一刻无双跑（gateway target 切到 channel-server + flag=on 二者之间不留「proxy 仍接流但新分流已开」窗口，同一消息只被分流一次，coe 演练 + prod 观察均有证据）；部署前确认无在跑异步任务；切后 bot 维度路由仍命中。

**Backlog（本期不做，留待身份迁移完成后）：conversation 维度绑定重建**
- 触发条件：channel-layer-redesign 身份迁移完成、全局 Conversation ID 可用之后。
- 目标：基于全局 Conversation ID 重建 conversation 维度的细粒度泳道绑定，把 `resolveLane` 优先级升级回 conversation > bot > prod，lane_routing 用 `route_type='conversation'` 存全局 Conversation ID。
- 说明：本期**不回填**现存 chat 维度的飞书裸 chat_id 绑定，这些绑定到时按全局 Conversation ID 重新建立，不做裸 id → 全局 id 的回填迁移。

**Backlog（本期不做）：非普通消息事件参与泳道分流**
- 触发条件：出现让部分非普通消息事件随对应 lane 处理的真实需要（如撤回跟随原消息所在 lane）。
- 目标：评估并实现把部分飞书事件（撤回 / 用户成员变更 / reaction / chat.updated / p2p_chat_entered 等）纳入 `inbound_lane` 分流。
- 说明：本期这些事件一律 prod-only（§5.5 定论），不参与泳道分流；本项只在有真实需要时再单独评估，不为假设预留。

---

## 附：规范遵循自查

- **零兼容层**：channel-proxy 直接删除（Deployment / Service / 源码全删），无退化版、无 re-export / 无旧函数别名 / 无 deprecated wrapper（§5、Task 8）。
- **平台无关核心**：泳道决策只看全局统一概念，决策层禁止出现飞书字段名（§3.2 红线、Task 2 可测验收）。
- **命名不空泛**：新队列 `inbound_lane.{lane}`（说清是「入站 lane 分发」）、决策能力 `resolveLane`（动词 + 领域名词），不用 XxxService/Manager/Handler。
- **防双跑**：单生产者 + 单消费者群 + `event_type + globalMessageId + lane` 三元组幂等 + cutover 不重叠（§4.4，吸取 ppe 双跑教训）。
- **入站队列 fail-closed**：`inbound_lane.{lane}` 绝不复用现状 lane 队列的 10s 回 prod fallback——lane 消费者缺席时消息留队列 / 进 DLQ 告警、绝不自动落 prod（§4.6，Task 3 可测验收）。
- **幂等覆盖到三元组**：现状 `storeMessage ON CONFLICT` + Redis 60s 锁有盲区（publish 后未 ack 超 60s 重投 / 非 message_id 事件），去重边界提到 `event_type + globalMessageId + lane`、命中已处理时所有副作用不再执行（§4.4 point 5，Task 3 可测验收）。
- **事件矩阵全覆盖**：删 proxy 后 channel-proxy 现状注册的 12 类飞书事件都有 channel-server 入口接、不比现状少接，各事件 lane 去向按矩阵落实（§5.5、Task 5）。
- **admin API 不掉链**：lane_routing 绑定管理 API（lane-bindings）+ 缓存失效随 proxy 取消迁到 channel-server（读写同源、本进程直接清缓存），`/ops bind` / dashboard 不断（§5.6、Task 6 独立 task）。
- **不过度抽象**：本期分流维度只做 bot 一维（chat/conversation 维度待身份迁移后重建、user 维不预加），不为未来预先加维（§3.2，对齐 `feedback_over_abstraction_for_future`）。
- **分流点前禁副作用**：对非 prod lane 消息，分流决策点之前禁止任何持久化 / 外呼副作用，否则 prod 双写双处理（§3.1a 铁律，含现状图片管线 / presence / 审计落库前移审计，Task 4 落地）。
- **入口切换可秒级回滚**：webhook 已走 api-gateway，cutover 只改 gateway 规则 target、飞书后台不动、proxy 暂留作回滚 target、prod 稳定后才删 proxy（§7.0 / §7.2-7.3）。
- **webhook 强制 prod、清外部 lane header**：gateway webhook 规则强制 target lane=prod、剥离外部 `x-ctx-lane` / `x-lane`，防伪造 lane 绕过决策点（§6 / §7 / Task 7）。

## 决策记录（原待拍板点，已全部拍定）

三个待拍板点用户已全部拍定，本设计定稿、可进入实现：

- **websocket 长连 bot 的承载方式（§5.4 / Task 4）**：**定论——起在 channel-server 进程内**。channel-server 的 plugins/lark 进程内起 `Lark.WSClient` 长连接（不单拎独立常驻进程），事件喂进同一条入站契约链。代价：channel-server 重启会断长连，必须自动重连、重连后不丢消息（验收口径见 Task 4）。
- **lane_routing admin API（lane-bindings）迁到哪个服务（§5.6 / Task 6）**：**定论——迁到 channel-server**。与读 `lane_routing` 的 prod channel-server 同处，写绑定后本进程直接清 `LaneResolver` 缓存、链路最简；不放 paas-engine（避免跨服务清缓存通知）。
- **非普通消息事件该不该按 lane 分流（§5.5 / Task 5）**：**定论——本期一律 prod-only**。本期只有普通消息（`im.message.receive_v1`）走 `inbound_lane` 分流；卡片回调 / bot 入退群 / 撤回 / 用户成员变更 ×3 / reaction ×2 / chat.updated / p2p_chat_entered 等其余所有飞书事件统一在 prod 处理、不参与泳道分流。后续若有让部分事件参与分流的真实需要，再单独评估（Backlog）。
