# 多渠道接入改造：把飞书专属入口抽象为通用 channel 接入框架（QQ 作为第一个验证 channel）

## 这份文档解决什么问题

现在整条消息链路是写死飞书的。事件处理器直接绑定飞书的事件名（`im.message.receive_v1` 这种），回复链路深度依赖飞书 SDK，更麻烦的是飞书的身份体系渗透得到处都是——飞书的 `union_id` 出现在 7 张表里、其中 5 张拿它当主键，飞书的 `chat_id` / `message_id` / `root_message_id` 被当成全局唯一的裸 ID 直接用，连 Qdrant 向量库里都按这些 ID 过滤。群聊里"这条消息是不是在叫机器人"的判断，是靠飞书的 `robot_union_id` 反查飞书 mention 结构来做的。

结果就是：系统里没有任何"渠道"这一层抽象，每接一个新平台都要把核心链路再改一遍。我们要接 QQ，所以这次要把飞书从"唯一实现"降级成"第一个 adapter"，把渠道这层抽象建出来。

## 做完之后是什么样

1. lark-proxy / lark-server 里的飞书专属逻辑被重写成一套平台无关的 channel 抽象，这套抽象有四层契约：入站事件、内部消息模型、出站回复、bot 命中判断。飞书变成这套抽象下的一个 adapter。改造完飞书 dev bot 的端到端对话行为跟改造前完全一致（要有回归证据，不是"看着没问题"）。

2. 用户、会话（chat）、消息（message）这三类身份全部做成"按 channel 隔离"的：存在三套 `(channel, channel 内的ID) → 全局内部ID` 的映射表，原来 7 张表里的飞书身份字段加上 Qdrant 向量库，全部一刀切迁移到全局 ID，所有读写这些字段的调用方都改完，不留任何 fallback 或兼容代码。

3. "这条消息要不要触发 bot 回复"这个判断有了一套 channel 无关的契约。QQ 是用来验证这套框架的第一个新 channel：QQ 官方机器人的 dev bot 在私聊和群聊两种场景下，纯文本消息能端到端收发，赤尾能在 QQ 上正常对话。

4. bot_config 表支持多 channel：加一个 channel 列，各 channel 自己的凭据放进一个 JSONB 列，飞书现有的凭据迁进去。

## 这次明确不做的事

- QQ 这个新渠道这期只做纯文本：QQ 的入站和出站都只处理纯文本，图片、富文本、表情、分段流式回复都不做，留给后续迭代。这句话只约束 QQ，**不约束飞书**——飞书现有的图片、富文本（post）、sticker、语音、文件、合并转发、分享名片这些非文字内容类型，以及"图片进识图管线、赤尾能看图聊天"这类既有能力，本期必须原样保留、零变化，绝不能因为引入新抽象就被砍掉。
- 个人 QQ（OneBot / NapCat 那条路）不做，只做 QQ 开放平台的官方机器人。
- 微信、Discord 等其他平台这次不实际接入。框架要为它们留好扩展口子，但不写实现。
- agent-service 里的 AI 链路（vectorize / recall / memory / safety）逻辑不动。已经确认过这些链路只是把身份字段透传下去，并不消费身份的语义，所以身份迁移不影响它们的逻辑。

## 关键设计决策

**决策一：重写 lark-* 成 channel-*，而不是保留飞书链路再并排加一条 QQ 链路。**

项目的重构规范明令禁止任何兼容层 / 过渡层。如果保留飞书链路、另起一条 QQ 链路并行，飞书的耦合就永远清不掉，两条链路长期重复。所以选择把飞书专属层彻底重写成平台无关的 channel 抽象，飞书作为第一个 adapter。这个改名的爆炸半径已经评估过，是中等程度——影响目录名、Dockerfile、Makefile 的 SIBLINGS 配置、服务间调用的默认 URL、PaaS 应用注册、ConfigBundle，这些都是可控的硬编码点。

**决策二：用户、会话、消息这三类身份全部 channel 作用域化，而不是只迁移用户身份。**

最初只打算迁移用户身份。但补查代码后发现，`chat_id` / `message_id` / `root_message_id` 在 SQL 查询和 Qdrant 向量库里都被当成全局唯一的裸 ID 在用。而 QQ 的群 ID 和飞书的 chat_id 是两个完全独立的命名空间——如果只迁用户身份、会话和消息身份还是裸 ID，QQ 接进来之后会话维度和回复链路会直接被击穿（不同平台的会话 ID 撞车）。所以三类身份必须一起做平台隔离。

**决策三：一刀切停机迁移，回滚靠快照恢复，而不是靠映射表反查。**

灰度双写、读时 fallback 这类方案会引入过渡代码，违反"禁止兼容层"的硬规范，所以选一刀切停机迁移、原地重写。这里有个重要澄清：映射表只能把旧 ID 翻译成新 ID，它没法恢复已经被重写的主键、外键、索引这些结构，也没法恢复 Qdrant 里已经改掉的向量。所以回滚不是"用映射表反查回去"，而是"迁移前对 DB 和 Qdrant 做全量快照，出问题就用快照恢复"。这是 restore，不是 rollback，spec 里要把这点说死。停写期间需要一个 drain gate（下面"数据与部署影响"会讲）。

**决策四：把"消息是否触发 bot"的判断契约平台化，从原来的非目标提升为核心。**

最初以为这块不受影响。补查发现：群聊里靠 `NeedRobotMention` 判断，它又依赖 `robot_union_id` 去反查飞书的 mention 结构，而且判断不通过的群消息是被静默丢弃的、连日志都没有。QQ 群聊要能纯文本端到端打通，就必须有一套平台无关的"这条消息是不是在叫 bot"契约，外加一个平台无关的 bot 身份概念。否则 QQ 群里发的消息会被现有规则链直接吞掉，查都查不到。

**决策五：channel 抽象切成四层契约。**

入站事件 adapter、平台无关的内部消息模型、出站回复 adapter、bot 命中契约。代码里的飞书耦合恰好就集中在这四个地方，而 agent-service 这条中间链路本身对消息来自哪个平台是无感知的，所以这四层契约能把平台差异完全收敛在 adapter 内部，中间链路不用动。

## 抽象设计

这一节把这套接入抽象具体画出来。先说清楚立场：这不是"飞书 + QQ 两个 IM 的最小公倍数"，而是一套通用的 channel 接入契约。飞书和 QQ 只是头两个落地的 adapter——飞书是行为不变的回归基线，QQ 是第一个用来验证框架的新 channel。下面的接口用类型签名草图表达，目的是把契约形态这个设计决策定下来；签名内部的实现逻辑是动手时通过 TDD 生成的，这里不预写。

### 核心立场：channel 是通用接入点，契约里不许出现 IM 专有假设

一个 channel 指的是任何"能把外部消息送进来、并能把回复送回去"的接入点。飞书、QQ 是 IM 类型的 channel，但一个纯 HTTP 问答端点、一个网页对话框、一个 Discord interaction 同样是 channel。所以契约里**不允许**把 `@`、私聊/群聊二元、消息回复树这些 IM 专有概念当成强制语义——它们只能作为某些 channel 各自 adapter 的内部细节存在。判断一个字段或概念该不该进契约，标准只有一条：换一个非 IM 的 channel，它是否还讲得通。讲不通的，就压到 adapter 里去。

### 整体分层

一条消息的完整路径：

```
channel 入站（webhook / 长连接 / 同步 HTTP 请求，由 adapter 内部封装）
  → channel-proxy：按入站标识查到它属于哪个 channel、哪个 bot，
                    查 lane_routing 决定路由到哪个泳道的 channel-server
  → channel-server：
      1. InboundAdapter[channel].verify(raw)          验签
      2. InboundAdapter[channel].parse(raw)           原始入站 → 通用 InboundMessage
      3. AddressingPolicy[channel].shouldRespond(...)  这条消息要不要 bot 响应
                                                       不响应：记可查日志后丢弃（禁止静默）
      4. IdentityResolver.resolve(InboundMessage)      channel 内 ID → 全局内部 ID
      5. 存 conversation_messages（全部用全局 ID）
      6. 发 ChatTrigger（带 channel + 全局 ID）到 RabbitMQ
  → agent-service：对消息来自哪个 channel 完全无感知，逻辑不动
  → ChatResponseSegment（带 channel + 全局 ID）回到 RabbitMQ
  → chat-response-worker：
      1. IdentityResolver.toChannel(全局 ID)           全局 ID → channel 内 ID（反查）
      2. OutboundAdapter[channel].reply/send(...)      纯文本回复发回 channel
```

`channel-proxy` 和 `channel-server` 这两个进程本身完全 channel 无关。所有 channel 差异都收敛在 `InboundAdapter` / `OutboundAdapter` / `AddressingPolicy` 这三个按 channel 分实现的东西里，加上一个 channel 无关的 `IdentityResolver` 负责 ID 翻译。

### 契约一：入站 adapter（InboundAdapter）

每个 channel 实现一份。职责是把这个 channel 的原始入站，变成通用的 `InboundMessage`。传输方式（webhook、长连接、同步请求-响应）是 adapter 内部的事，契约只认"收到一个原始入站、产出一个 InboundMessage"。

```
interface InboundAdapter {
  // 接入握手 / 回调校验。IM channel 常用（飞书 challenge、QQ webhook 验证），
  // 不需要握手的 channel 直接返回 null
  handleHandshake(raw): HandshakeResponse | null

  // 验签。没有签名机制的 channel 实现为恒 true（但要在 adapter 里说明为什么安全）
  verify(raw): boolean

  // 原始入站 → 通用消息。不是要处理的消息（如平台杂事件）返回 null
  parse(raw): InboundMessage | null
}
```

### 契约二：通用入站消息模型（InboundMessage）

所有 adapter 的 `parse` 都产出这同一个结构。身份字段一律带 `channel_` 前缀，表示是"channel 内的 ID"、还没翻译成全局 ID。这里刻意不放 `chat_type: p2p|group`、不放 `raw_mentions`、不放裸的回复字段，因为那些是 IM 假设：

```
InboundMessage {
  channel:              string            // "lark" / "qq" / 以后任意，不是写死的枚举
  bot_name:             string
  channel_message_id:   string            // channel 内消息 ID，channel 内唯一
  channel_chat_id:      string            // channel 内会话 ID
  channel_user_id:      string            // channel 内发送者 ID
  conversation_scope:   string            // 会话作用域。常见取值 "direct"/"group"，
                                          // 但不是强制二元；非 IM channel 可定义自己的。
                                          // adapter 负责把它映射到下游需要的 is_direct
  thread_ref:           ThreadRef | null  // 可选的线程/关联引用（IM 的回复树放这里），
                                          // 非 IM channel 不填
  addressing_hints:     AddressingHint[]  // channel 给的"这条冲谁来"的线索，
                                          // IM 里是 @ 列表，HTTP 入口可能为空或固定指向某 bot
  content:              ContentItem[]     // 统一内容；本期就要能承载飞书现有
                                          // MessageTransferer 产出的全部内容类型
  received_at:          int
}
```

几个关键点。`conversation_scope` 取代了原来的 `chat_type` 二元枚举——下游现有链路需要的 `is_direct` / 旧 `chat_type` 由 adapter 从 `conversation_scope` 映射出来，语义不丢，但契约本身不再假设"会话只有一对一和群两种"。`addressing_hints` 取代了 `raw_mentions`——它是"谁被这条消息指向"的通用线索，由 `AddressingPolicy` 解释，契约不假设它一定是 `@`。`thread_ref` 把回复/话题根收进一个可选结构，没有回复语义的 channel 直接不填。

关于 `ContentItem`：因为本期的硬约束是飞书行为零变化，而飞书现状下赤尾本来就在处理大量非文字消息（图片会进识图管线、赤尾能看图聊天），所以通用内容模型本期就必须能忠实承载飞书现有 `MessageTransferer` 产出的全部内容类型——text、image、post 富文本、sticker、media、file、audio、合并转发、分享名片，以及无法识别时的 unsupported 占位等。如果只定义 `Text`、让飞书 adapter 对非文字消息返回 null，飞书发图 @ 赤尾就会被彻底丢弃，这是把核心能力砍掉的严重回归，绝不允许。需要说明的是，QQ adapter 这期只产出 `Text`，那是 QQ 这个新渠道自身的范围限制（见"这次明确不做的事"），并不意味着通用内容模型只支持 text——内容模型的承载范围由所有 adapter 里能力最全的那个（飞书）决定。同时这里仍然守住"契约里不许出现 IM 专有假设"这个既有立场：内容模型描述的是"一条消息由哪些内容片段构成"这种通用结构，不是飞书专有概念，只是把它能承载的内容种类讲清楚而已。

### 契约三：是否需要 bot 响应（AddressingPolicy）

判定一条入站消息要不要触发 bot。这是这次从非目标提升为核心的契约（原来藏在飞书规则引擎里、不中还静默丢弃）。契约只规定输入输出，**不预设任何 IM 规则**：

```
interface AddressingPolicy {
  // bot_identity 由 bot_config 按 channel 取该 channel 的 bot 标识
  shouldRespond(msg: InboundMessage, bot_identity): boolean
}
```

框架不规定"私聊就直通、群聊就看 @"这种骨架——那是 IM channel 自己 adapter 里的实现选择。一个纯 HTTP 问答入口完全可以恒返回 true。常见 IM channel 的典型写法（`conversation_scope=="direct"` 直通、`group` 看 `addressing_hints` 里有没有命中 `bot_identity`）是参考，不是契约强制。**唯一强制的是：判定为不响应时，必须记一条可查日志再丢弃，禁止像现在这样静默 break**——否则 channel 接进来出问题根本查不到。

### 契约四：出站 adapter（OutboundAdapter）

每个 channel 实现一份。这期只要求纯文本。

```
interface OutboundAdapter {
  send(channel_chat_id, content):     channel_message_id  // 在会话里新发一条
  reply(thread_ref, content):         channel_message_id  // 在某线程/某条消息下回复；
                                                          // channel 不支持回复语义时退化为 send
}
```

鉴权由各 channel 的 adapter 自理（飞书用现有 SDK client，QQ 用 AccessToken 并在 adapter 内部自动刷新）。富文本、图片、分段流式这期不做，接口先不开这些方法。

### IdentityResolver + 三类身份映射

三张结构相同的映射表，把"channel 内 ID"翻译成 channel 无关的全局内部 ID：

```
identity_user     (channel, channel_user_id)    → internal_user_id    [(channel,channel_user_id) 唯一]
identity_chat     (channel, channel_chat_id)    → internal_chat_id
identity_message  (channel, channel_message_id) → internal_message_id
```

`internal_*_id` 是新分配的、channel 无关的全局字符串 ID（用 ULID 这类，保证不同 channel 不会撞）。飞书历史迁移时，对每个旧的 `union_id` / `chat_id` / `message_id`，以 `channel=lark`、`channel_*_id=原飞书ID` 插入映射表并分配全局 ID，再把"调用方覆盖"里那 7 张表的对应字段、加上 Qdrant 里的 chat_id / root_id 全部原地重写。`IdentityResolver` 是读写这三张表的唯一入口，正查（channel 内 → 全局，进站用）和反查（全局 → channel 内，出站用）都走它；进站遇到没见过的 channel 用户/会话要原子地分配并落映射，反查不到要明确报错而不是静默放过。

### bot_config 多 channel 化后的形态

```
bot_config {
  bot_name     (主键)
  channel      string   // "lark" / "qq" / ...，不是写死枚举
  persona_id
  bot_role     "persona" | "utility"
  is_active / is_dev
  credentials  JSONB    // 各 channel 自己的凭据结构，框架不约束形状
                        // lark: { app_id, app_secret, encrypt_key, verification_token, robot_union_id }
                        // qq:   { app_id, app_secret, bot_secret }
}
```

原来散在独立列里的飞书凭据全部迁进 `credentials` JSONB、旧列删掉（不留双形态）。bot 加载链路读到一条记录后，按 `channel` 选对应的 `InboundAdapter` / `OutboundAdapter` / `AddressingPolicy` 三件套，凭据从 `credentials` 取，框架不解释 `credentials` 里的具体字段——那是各 adapter 的事。

### 头两个 adapter 各自要实现什么

这张表是落地说明，不是契约的一部分。Ed25519、AccessToken、`im.message.receive_v1` 这些都是 adapter 内部细节，契约层不感知：

| 契约方法 | 飞书 adapter（回归基线） | QQ adapter（第一个新 channel） |
|---|---|---|
| handleHandshake | 飞书 challenge 应答 | QQ webhook 验证请求应答 |
| verify | verification_token + encrypt_key 校验 | Ed25519（botSecret 派生密钥对）验签 |
| parse | 飞书消息事件 → InboundMessage，scope 映射 p2p→direct | QQ 消息事件 → InboundMessage，C2C→direct |
| shouldRespond | direct 直通；group 看 addressing_hints 是否含 robot_union_id | direct 直通；group 看 QQ at 结构是否含 bot appid |
| send / reply | 现有飞书 SDK 发送 / 回复 | QQ OpenAPI 发送 + AccessToken 自刷新 |

agent-service、RabbitMQ 拓扑、lane_routing、IdentityResolver 都不在这张表里——它们 channel 无关，不需要每 channel 实现。

### 验证契约不被 IM 绑架：接第三个 channel 要做什么

拿一个跟 IM 形态差别很大的假想 channel 检验这套契约——一个纯 HTTP 同步问答入口（外部 POST 一个问题，HTTP 响应里同步拿到答案，没有 webhook、没有 @、没有群、没有回复树）。要接入它，只需要：

- 写一个 `InboundAdapter`：`handleHandshake` 返回 null（不需要握手），`verify` 按它自己的鉴权方式实现，`parse` 把 HTTP body 转成 `InboundMessage`——`conversation_scope` 填 `"direct"`，`addressing_hints` 留空，`thread_ref` 留 null。
- 写一个 `OutboundAdapter`：`send` / `reply` 把答案写回那次 HTTP 请求的响应（或它指定的回调），`reply` 直接退化成 `send`。
- 写一个 `AddressingPolicy`：`shouldRespond` 恒返回 true。

不用动 `channel-proxy` / `channel-server` 的主流程，不用动 `agent-service`，不用动身份映射层，不用碰飞书和 QQ 的 adapter。如果将来发现某个新 channel 接入时被迫去改核心或改别人的 adapter，那就说明这套契约的某个假设漏了，要回头修契约——这条是这套设计的验收底线。

## 调用方覆盖

下面这些是 grep 加 Explore 子 agent 实际查出来的，不是凭印象列的。

**含飞书身份字段、需要一刀切迁移并纳入快照范围的 7 张表：**

| 表 | 飞书身份字段 | 约束情况 |
|---|---|---|
| lark_user | union_id | 主键 |
| lark_group_member | chat_id, union_id | 复合主键 |
| lark_user_open_id | app_id, open_id, union_id | 复合主键 + 外键 |
| user_blacklist | union_id, blocked_by | 主键 + 普通字段 |
| user_group_binding | user_union_id, chat_id | 普通字段 |
| conversation_messages | user_id（存的是 union_id）, chat_id, message_id, root_id | message_id 是主键，其余普通字段 |
| bot_config | robot_union_id | bot 身份字段 |

**读写身份/会话语义、需要跟着改的调用方：**

- agent-service `app/data/queries/messages.py`：按 user_id 过滤、JOIN lark_user、JOIN 群成员、写主动消息、按 root_message_id 跨会话查（line 362、210）。
- agent-service `app/chat/quick_search.py:84`：按 chat_id 拉同一会话的消息。
- agent-service `app/agent/tools/history.py:163`：在 Qdrant 里按 chat_id 过滤做向量检索。
- agent-service `app/life/proactive.py:73`：用 chat_id 定位主动消息（proactive）的目标会话。
- agent-service `app/data/models.py:138`：message_id 当主键。
- lark-server `chat-response-worker.ts`：把 user_id 写回 conversation_messages。
- lark-server `bot-chat-presence.ts`：chat_id 当主键。
- monitor-dashboard `messages.ts:35,91`：SQL JOIN 用 union_id 关联。
- 契约层 `chat_dataflow.py` 的 user_id 字段：要加 channel。

**bot 命中链路，需要平台化的地方：**

- lark-server `core/rules/engine.ts:29-82`：规则引擎，规则不过会静默 break（这就是消息被吞掉的地方）。
- lark-server `core/rules/rule.ts:84`：`NeedRobotMention` 的定义是 `hasMention(botUnionId) || isP2P()`。
- lark-server `core/models/message.ts:139`：`hasMention` 去查飞书的 mention 结构。
- lark-server `mention-utils.ts:6`：从飞书事件里提取 union_id。
- lark-server `bot-var.ts:21`：读 `robot_union_id`。

**确认不受影响的：** vectorize / recall / memory / safety / chat_node 这些链路只是透传身份字段、不消费其语义；lane_routing 的 route_key 用的是 bot_name 不是身份；RabbitMQ 的队列拓扑不变；LaneRouter 和 lite-registry 已经走环境变量了。

## 数据与部署影响

**Schema 变更（要走 `/ops-db submit`，属于破坏性变更，必须在 coe-* 独立泳道做）：** 新建用户、会话、消息三套 `(channel, channel 内ID) → 全局ID` 的映射表；上面 7 张表的身份字段和主键体系一刀切重写；Qdrant 向量库里的 chat_id / root_id 同步重写；bot_config 加 channel 列和 credentials JSONB 列，把飞书凭据迁进去。

**Drain gate（停写窗口）：** 迁移之前必须先关掉 webhook 入口、清空或妥善处置 RabbitMQ 和 outbox 里的队列、把一镜像产出的多个服务（channel-server / recall-worker / chat-response-worker，以及 agent-service / vectorize-worker）同步切换之后，才恢复写入。否则在没有 fallback 的情况下，新旧契约会在队列重放时互相污染。部署前还要确认没有正在跑的 rebuild / afterthought 后台任务。

**回滚：** 迁移前对 DB 和 Qdrant 做全量快照。失败就用快照恢复，这是 restore 不是代码层面的 rollback。

**PaaS 应用改名：** lark-proxy / lark-server 改成 channel-proxy / channel-server，需要在 PaaS Engine 注册新应用、迁移 ConfigBundle、下线旧应用。这是高风险部署项，上线前要单独跟用户确认。

**其他：** 没有 Langfuse prompt 变更。QQ dev bot 测试前要先在 QQ 开放平台建一个测试机器人并配好 HTTPS 回调地址。

## 任务清单

任务之间的依赖关系：T1 是 T2、T4、T5、T6 的前置；T4 是 T6 的前置；T3 是独立的可以并行；T5 放在 T2 之后做，避免飞书 adapter 还没稳定就迁移、回归噪声分不清是谁的问题。

**T1. channel 四层契约 + channel 作用域身份模型（这是地基，其他任务都依赖它）**

目标是定义并落地 channel 无关的四层契约（入站、消息模型、出站、是否响应），让内部消息模型和 ChatTrigger / ChatResponseSegment 带上 channel，把用户、会话、消息三类身份的 channel 作用域抽象建出来。产出是这四层接口、带 channel 的内部消息和契约模型、三类身份映射的领域模型（这一步不含真正的数据迁移）。验收标准：契约层单测能覆盖"相同的 channel 内 chat ID 在不同 channel 下不会串成同一个会话""AddressingPolicy 在 direct 和 group 两条路径都返回正确结果""文档里那个假想的纯 HTTP 问答 channel 能只靠实现三件套接入、不改核心"；agent-service 中间链路不感知具体 channel 也能编译通过。

**T2. 飞书 adapter（行为不变，回归验证）**

把现有飞书的入站、出站、bot 命中逻辑收敛成 T1 契约下的第一个 adapter，飞书行为零变化。产出是飞书 adapter（事件解析、回复发送、mention 转 bot 命中），飞书专有的命名只能出现在 adapter 内部。这一步同时落实 T1 留下的内容模型扩展：让通用 `ContentItem` 真正能承载飞书全部内容类型，飞书 adapter 的 `parse` 据此完整转换。这个阶段 adapter 还没接进真实链路（接线属于 T5），不接线就没有真实飞书流量经过新 adapter，部署 coe 也证明不了 adapter 本身，所以 T2 的验收口径放在代码侧，不在本阶段做 coe 端到端。验收标准：飞书 adapter 三件套（事件解析、回复发送、mention 转 bot 命中）实现完整，飞书专有命名只出现在 adapter 内部；单测覆盖飞书现有的全部消息类型（text、image、post 富文本、sticker、media、file、audio、合并转发、分享名片、unsupported 占位等），转换结果与现状 `MessageTransferer` 行为一致，任何非文字消息都不得被丢弃（不得返回 null 当成"没收到消息"）；codex 双轮 review 的必改项全部消化，独立验证的约束守住。真实链路端到端的飞书行为零变化回归（私聊、群聊、@bot、不 @bot，以及图片/富文本/sticker/语音/文件等非文字消息）因为依赖接线、属于 T5 范畴，统一并入 T5 验收，T2 阶段不做。

**T3. lark-* 改名成 channel-*（独立验收面，可并行）**

把目录名、包名、Dockerfile、Makefile 的 SIBLINGS、服务间调用的默认 URL、PaaS 应用名、ConfigBundle 全部迁到 channel-*。产出是改名后能正常构建部署的 channel-proxy / channel-server。验收标准：业务代码里没有旧服务名的调用入口；PaaS 和 ConfigBundle 都指向 channel-*；coe 泳道部署后飞书回归通过；飞书 adapter 内部的飞书专有命名保留、没被误改。

**T4. bot_config 多平台化**

bot_config 加 channel 列和 credentials JSONB 列，飞书凭据迁进 JSONB、旧的独立列清掉，bot 加载链路按 channel 分发。产出是 bot_config 的 schema 变更、凭据迁移、多 channel 加载链路。验收标准：coe 泳道里飞书 bot 仍能正常加载和收发；飞书凭据已经在 JSONB 里、旧列已删；新增一条 channel=qq 的记录能被加载链路识别。

**T5. 三类身份一刀切迁移 + 快照回滚 + drain gate**

建用户、会话、消息三套映射表，停机原地重写那 7 张表加 Qdrant，飞书侧的 `(lark, 原飞书ID)` 进映射表，把"调用方覆盖"里所有需要改的调用方都改完，不留 fallback。产出是三套映射表、带 drain gate 的迁移过程、DB 和 Qdrant 的快照与恢复预案、调用方改造。接线边界：把新 channel 抽象接入真实链路时，飞书非文字消息既有的全部路径必须保持存活、零回归——非文字消息照常存库、图片照常进识图管线、赤尾照常能看图聊天，绝不能因为接入新抽象就把非文字消息的处理路径丢掉。验收标准：coe 泳道迁移后，飞书用户和会话的历史查询结果跟迁移前一致；新写入用的是全局 ID；Qdrant 按新 ID 检索正确；快照恢复演练成功；代码里没有双读或 fallback 路径。另外，因为 T5 完成了把新 channel 抽象接进真实链路，这里必须承接从 T2 并过来的那部分验收：在 coe 泳道绑飞书 dev bot 做一次飞书行为零变化的端到端回归，覆盖私聊、群聊、@bot、不 @bot 各个场景跟改造前完全一致，并且非文字消息（图片、富文本、sticker、语音、文件）的存库、图片进识图管线、赤尾看图聊天这三条路径全部零回归，要有实际收发的日志或截图为证。

**T6. QQ 官方机器人 adapter（私聊 + 群聊纯文本）**

实现 QQ adapter：webhook 接入、Ed25519 验签、回调地址校验、事件转成内部消息模型、AccessToken 鉴权和自动刷新、对接平台无关的 bot 命中契约、纯文本回复，覆盖私聊和群聊。产出是 QQ 的入站、出站、bot 命中 adapter，接 T1 的契约，凭据走 T4 的 JSONB。验收标准：QQ 开放平台的测试机器人绑到 coe 泳道，私聊和群聊各发纯文本、群聊里 @bot 和不 @bot 各发一条，赤尾按 bot 命中契约该回的回、不该回的不回，要有端到端日志为证。
