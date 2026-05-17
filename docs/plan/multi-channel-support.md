# 多渠道接入改造：把飞书专属入口抽象为平台无关的 channel 框架，QQ 官方机器人首个落地

## 这份文档解决什么问题

现在整条消息链路是写死飞书的。事件处理器直接绑定飞书的事件名（`im.message.receive_v1` 这种），回复链路深度依赖飞书 SDK，更麻烦的是飞书的身份体系渗透得到处都是——飞书的 `union_id` 出现在 7 张表里、其中 5 张拿它当主键，飞书的 `chat_id` / `message_id` / `root_message_id` 被当成全局唯一的裸 ID 直接用，连 Qdrant 向量库里都按这些 ID 过滤。群聊里"这条消息是不是在叫机器人"的判断，是靠飞书的 `robot_union_id` 反查飞书 mention 结构来做的。

结果就是：系统里没有任何"渠道"这一层抽象，每接一个新平台都要把核心链路再改一遍。我们要接 QQ，所以这次要把飞书从"唯一实现"降级成"第一个 adapter"，把渠道这层抽象建出来。

## 做完之后是什么样

1. lark-proxy / lark-server 里的飞书专属逻辑被重写成一套平台无关的 channel 抽象，这套抽象有四层契约：入站事件、内部消息模型、出站回复、bot 命中判断。飞书变成这套抽象下的一个 adapter。改造完飞书 dev bot 的端到端对话行为跟改造前完全一致（要有回归证据，不是"看着没问题"）。

2. 用户、会话（chat）、消息（message）这三类身份全部做成"按平台隔离"的：存在三套 `(platform, 平台内的ID) → 全局内部ID` 的映射表，原来 7 张表里的飞书身份字段加上 Qdrant 向量库，全部一刀切迁移到全局 ID，所有读写这些字段的调用方都改完，不留任何 fallback 或兼容代码。

3. "这条消息要不要触发 bot 回复"这个判断有了一套平台无关的契约。基于它，QQ 官方机器人的 dev bot 在私聊和群聊两种场景下，纯文本消息能端到端收发，赤尾能在 QQ 上正常对话。

4. bot_config 表支持多平台：加一个 platform 列，平台各自的凭据放进一个 JSONB 列，飞书现有的凭据迁进去。

## 这次明确不做的事

- QQ 的图片、富文本、表情、分段流式回复都不做，这期只做纯文本。这些留给后续迭代。
- 个人 QQ（OneBot / NapCat 那条路）不做，只做 QQ 开放平台的官方机器人。
- 微信、Discord 等其他平台这次不实际接入。框架要为它们留好扩展口子，但不写实现。
- agent-service 里的 AI 链路（vectorize / recall / memory / safety）逻辑不动。已经确认过这些链路只是把身份字段透传下去，并不消费身份的语义，所以身份迁移不影响它们的逻辑。

## 关键设计决策

**决策一：重写 lark-* 成 channel-*，而不是保留飞书链路再并排加一条 QQ 链路。**

项目的重构规范明令禁止任何兼容层 / 过渡层。如果保留飞书链路、另起一条 QQ 链路并行，飞书的耦合就永远清不掉，两条链路长期重复。所以选择把飞书专属层彻底重写成平台无关的 channel 抽象，飞书作为第一个 adapter。这个改名的爆炸半径已经评估过，是中等程度——影响目录名、Dockerfile、Makefile 的 SIBLINGS 配置、服务间调用的默认 URL、PaaS 应用注册、ConfigBundle，这些都是可控的硬编码点。

**决策二：用户、会话、消息这三类身份全部平台作用域化，而不是只迁移用户身份。**

最初只打算迁移用户身份。但补查代码后发现，`chat_id` / `message_id` / `root_message_id` 在 SQL 查询和 Qdrant 向量库里都被当成全局唯一的裸 ID 在用。而 QQ 的群 ID 和飞书的 chat_id 是两个完全独立的命名空间——如果只迁用户身份、会话和消息身份还是裸 ID，QQ 接进来之后会话维度和回复链路会直接被击穿（不同平台的会话 ID 撞车）。所以三类身份必须一起做平台隔离。

**决策三：一刀切停机迁移，回滚靠快照恢复，而不是靠映射表反查。**

灰度双写、读时 fallback 这类方案会引入过渡代码，违反"禁止兼容层"的硬规范，所以选一刀切停机迁移、原地重写。这里有个重要澄清：映射表只能把旧 ID 翻译成新 ID，它没法恢复已经被重写的主键、外键、索引这些结构，也没法恢复 Qdrant 里已经改掉的向量。所以回滚不是"用映射表反查回去"，而是"迁移前对 DB 和 Qdrant 做全量快照，出问题就用快照恢复"。这是 restore，不是 rollback，spec 里要把这点说死。停写期间需要一个 drain gate（下面"数据与部署影响"会讲）。

**决策四：把"消息是否触发 bot"的判断契约平台化，从原来的非目标提升为核心。**

最初以为这块不受影响。补查发现：群聊里靠 `NeedRobotMention` 判断，它又依赖 `robot_union_id` 去反查飞书的 mention 结构，而且判断不通过的群消息是被静默丢弃的、连日志都没有。QQ 群聊要能纯文本端到端打通，就必须有一套平台无关的"这条消息是不是在叫 bot"契约，外加一个平台无关的 bot 身份概念。否则 QQ 群里发的消息会被现有规则链直接吞掉，查都查不到。

**决策五：channel 抽象切成四层契约。**

入站事件 adapter、平台无关的内部消息模型、出站回复 adapter、bot 命中契约。代码里的飞书耦合恰好就集中在这四个地方，而 agent-service 这条中间链路本身对消息来自哪个平台是无感知的，所以这四层契约能把平台差异完全收敛在 adapter 内部，中间链路不用动。

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
- 契约层 `chat_dataflow.py` 的 user_id 字段：要加 platform。

**bot 命中链路，需要平台化的地方：**

- lark-server `core/rules/engine.ts:29-82`：规则引擎，规则不过会静默 break（这就是消息被吞掉的地方）。
- lark-server `core/rules/rule.ts:84`：`NeedRobotMention` 的定义是 `hasMention(botUnionId) || isP2P()`。
- lark-server `core/models/message.ts:139`：`hasMention` 去查飞书的 mention 结构。
- lark-server `mention-utils.ts:6`：从飞书事件里提取 union_id。
- lark-server `bot-var.ts:21`：读 `robot_union_id`。

**确认不受影响的：** vectorize / recall / memory / safety / chat_node 这些链路只是透传身份字段、不消费其语义；lane_routing 的 route_key 用的是 bot_name 不是身份；RabbitMQ 的队列拓扑不变；LaneRouter 和 lite-registry 已经走环境变量了。

## 数据与部署影响

**Schema 变更（要走 `/ops-db submit`，属于破坏性变更，必须在 coe-* 独立泳道做）：** 新建用户、会话、消息三套 `(platform, 平台内ID) → 全局ID` 的映射表；上面 7 张表的身份字段和主键体系一刀切重写；Qdrant 向量库里的 chat_id / root_id 同步重写；bot_config 加 platform 列和 credentials JSONB 列，把飞书凭据迁进去。

**Drain gate（停写窗口）：** 迁移之前必须先关掉 webhook 入口、清空或妥善处置 RabbitMQ 和 outbox 里的队列、把一镜像产出的多个服务（channel-server / recall-worker / chat-response-worker，以及 agent-service / vectorize-worker）同步切换之后，才恢复写入。否则在没有 fallback 的情况下，新旧契约会在队列重放时互相污染。部署前还要确认没有正在跑的 rebuild / afterthought 后台任务。

**回滚：** 迁移前对 DB 和 Qdrant 做全量快照。失败就用快照恢复，这是 restore 不是代码层面的 rollback。

**PaaS 应用改名：** lark-proxy / lark-server 改成 channel-proxy / channel-server，需要在 PaaS Engine 注册新应用、迁移 ConfigBundle、下线旧应用。这是高风险部署项，上线前要单独跟用户确认。

**其他：** 没有 Langfuse prompt 变更。QQ dev bot 测试前要先在 QQ 开放平台建一个测试机器人并配好 HTTPS 回调地址。

## 任务清单

任务之间的依赖关系：T1 是 T2、T4、T5、T6 的前置；T4 是 T6 的前置；T3 是独立的可以并行；T5 放在 T2 之后做，避免飞书 adapter 还没稳定就迁移、回归噪声分不清是谁的问题。

**T1. channel 四层契约 + 平台作用域身份模型（这是地基，其他任务都依赖它）**

目标是定义并落地平台无关的四层契约（入站事件、内部消息模型、出站回复、bot 命中），让内部消息模型和 ChatTrigger / ChatResponseSegment 带上 platform，把用户、会话、消息三类身份的平台作用域抽象建出来。产出是这四层接口、带 platform 的内部消息和契约模型、三类身份映射的领域模型（这一步不含真正的数据迁移）。验收标准：契约层单测能覆盖"相同的平台内 chat ID 在不同平台下不会串成同一个会话""bot 命中契约在群聊和私聊两条路径都返回正确结果"；agent-service 中间链路不感知具体平台也能编译通过。

**T2. 飞书 adapter（行为不变，回归验证）**

把现有飞书的入站、出站、bot 命中逻辑收敛成 T1 契约下的第一个 adapter，飞书行为零变化。产出是飞书 adapter（事件解析、回复发送、mention 转 bot 命中），飞书专有的命名只能出现在 adapter 内部。验收标准：飞书 dev bot 绑到 coe 泳道，私聊、群聊、@bot、不 @bot 各个场景跟改造前完全一致，要有实际收发的日志或截图为证。

**T3. lark-* 改名成 channel-*（独立验收面，可并行）**

把目录名、包名、Dockerfile、Makefile 的 SIBLINGS、服务间调用的默认 URL、PaaS 应用名、ConfigBundle 全部迁到 channel-*。产出是改名后能正常构建部署的 channel-proxy / channel-server。验收标准：业务代码里没有旧服务名的调用入口；PaaS 和 ConfigBundle 都指向 channel-*；coe 泳道部署后飞书回归通过；飞书 adapter 内部的飞书专有命名保留、没被误改。

**T4. bot_config 多平台化**

bot_config 加 platform 列和 credentials JSONB 列，飞书凭据迁进 JSONB、旧的独立列清掉，bot 加载链路按 platform 分发。产出是 bot_config 的 schema 变更、凭据迁移、多平台加载链路。验收标准：coe 泳道里飞书 bot 仍能正常加载和收发；飞书凭据已经在 JSONB 里、旧列已删；新增一条 platform=qq 的记录能被加载链路识别。

**T5. 三类身份一刀切迁移 + 快照回滚 + drain gate**

建用户、会话、消息三套映射表，停机原地重写那 7 张表加 Qdrant，飞书侧的 `(lark, 原飞书ID)` 进映射表，把"调用方覆盖"里所有需要改的调用方都改完，不留 fallback。产出是三套映射表、带 drain gate 的迁移过程、DB 和 Qdrant 的快照与恢复预案、调用方改造。验收标准：coe 泳道迁移后，飞书用户和会话的历史查询结果跟迁移前一致；新写入用的是全局 ID；Qdrant 按新 ID 检索正确；快照恢复演练成功；代码里没有双读或 fallback 路径。

**T6. QQ 官方机器人 adapter（私聊 + 群聊纯文本）**

实现 QQ adapter：webhook 接入、Ed25519 验签、回调地址校验、事件转成内部消息模型、AccessToken 鉴权和自动刷新、对接平台无关的 bot 命中契约、纯文本回复，覆盖私聊和群聊。产出是 QQ 的入站、出站、bot 命中 adapter，接 T1 的契约，凭据走 T4 的 JSONB。验收标准：QQ 开放平台的测试机器人绑到 coe 泳道，私聊和群聊各发纯文本、群聊里 @bot 和不 @bot 各发一条，赤尾按 bot 命中契约该回的回、不该回的不回，要有端到端日志为证。
