# PR #228 多渠道改造全局 Review（T1 → T5-5c 完整全貌）

> 这份文档是给项目 owner 的一次性 review，目的是让你不用逐个翻 commit，就能搞清楚整个多渠道
> 改造 PR 从头到尾做了什么、为什么这么设计、改了哪些文件的哪一段、验证到了什么程度、还有什么
> 没做。已有的 `docs/plan/multi-channel-T5c-readside-review.md` 只覆盖最后一刀（读取侧），这份
> 文档把 T1 到 T5-5c 的脉络整条讲清楚，T5-5c 那一章已自包含、不需要再翻那份文档。

---

## 一、这个 PR 整体在解决什么问题

改造前，整条消息链路是写死飞书的：飞书的 `union_id` 出现在 7 张表里、其中 5 张拿它当主键，
飞书的 `chat_id` / `message_id` / `root_message_id` 被当成全局唯一的裸 ID 直接存进
`conversation_messages`、直接写进 Qdrant 向量库的 filter；群聊里"这条消息是不是在叫机器人"
靠飞书的 `robot_union_id` 反查飞书 mention 结构，判定不过的消息被静默丢弃，连日志都没有。
系统里根本没有"渠道"这一层抽象，每接一个新平台都要把核心链路再改一遍。

这个 PR 做的事，一句话概括：**把"只支持飞书、身份就是飞书裸 ID"演变成"多渠道、身份是渠道
无关的全局 internal ULID + `(channel, channel 内 ID) → 全局 ID` 映射"。** 飞书从"唯一实现"
降级成"第一个 adapter"，渠道这层抽象（入站事件 / 内部消息模型 / 出站回复 / bot 命中判断
四层契约）被建出来，三类身份（用户 / 会话 / 消息）全部 channel 作用域化，规则引擎 `runRules`
从飞书专属重写成平台无关的统一引擎。QQ 是用来验证这套框架的第一个新渠道（QQ adapter 本身是
T6、尚未在本 PR 落地）。整个迁移是一刀切停机式的——没有灰度双写、没有读时 fallback、没有
任何兼容层，回滚靠迁移前的 DB / Qdrant 全量快照恢复，而不是靠映射表反查。

---

## 二、PR 范围：实际 commit 与提交状态

`git log --oneline main..feat/multi-channel-support` 的原文（从新到旧）：

```
0229a5d feat(channels): T5-5b platform-agnostic runRules + wire new abstraction
62e4902 docs(plan): finalize T5-5b design (platform-agnostic runRules)
04a15f4 feat(channels): T5 identity mapping tables and DB IdentityResolver
ff1d893 refactor(services): rename lark-* services to channel-*
a14b957 feat(bot-config): multi-channel bot_config with credentials JSONB
9083da1 feat(channels): Feishu adapter on T1 contracts
9c446ea docs(plan): clarify multi-channel spec scope and acceptance
00f7657 feat(channels): T1 channel abstraction contracts + scoped identity model
9cd37a6 docs(plan): generalize channel abstraction — drop IM-specific assumptions, QQ as first verifier
7ffd1f2 docs(plan): add architecture design — four channel contracts, identity mapping, adapter responsibilities
f7037a6 docs(plan): rewrite multi-channel spec in readable prose (content unchanged)
6453604 docs(plan): add multi-channel support spec (channel abstraction + QQ first landing)
```

去掉 6 个纯 `docs(plan)` commit（spec 起草与多轮演进），实际改代码的是 6 个 commit，对应
任务编号：

- `00f7657` = **T1**（四层契约 + 作用域身份模型）
- `9083da1` = **T2**（飞书 adapter，行为不变回归）
- `a14b957` = **T4**（bot_config 多 channel 化）
- `ff1d893` = **T3**（lark-* 改名 channel-*，部署面，227 文件、+193/-193 几乎纯重命名）
- `04a15f4` = **T5-5a**（三类身份映射表 + DB IdentityResolver）
- `0229a5d` = **T5-5b**（平台无关 runRules + 契约链接进真实链路）

**提交状态：T1 / T2 / T3 / T4 / T5-5a / T5-5b 这 6 个 commit 已提交并 push，构成 PR #228，
目前在人工 review 中。剩下三批改动——T5-5c（读取侧切全局 ID 零 fallback）、5b 入站链路
重排（修 5b commit 既有的入站顺序错误）、`memory.ts` fail-loud 死分支修复——全部还在
同一个工作区未提交**，没有 commit、没有 push、没有 apply DDL。这三批逻辑上耦合（重排
为 fail-loud 创造了真实分支、fail-loud 删除让那个分支可达、5c 与它们共用 storeMessage），
所以放在一起 review。

工作区未提交文件清单（按 `git status` 核对，分两类）：

生产代码（修改 13 个）：
- `apps/agent-service/app/data/models.py`（ConversationMessage 加 `username` 列）
- `apps/agent-service/app/data/queries/messages.py`（4 个读取 query 切 username 列）
- `apps/agent-service/app/agent/tools/history.py`（渲染侧按行级 / role 取名）
- `apps/agent-service/app/chat/_context_messages.py`（群上下文 `_speaker_of` 按 role 派生，
  codex 第二轮必改 3）
- `apps/channel-server/src/infrastructure/dal/entities/conversation-message.ts`（TypeORM 实体加列）
- `packages/ts-shared/src/entities/conversation-message.ts`（共享包实体加列）
- `apps/channel-server/src/types/chat.ts`（ChatMessage 加可选 `username`）
- `apps/channel-server/src/infrastructure/integrations/memory.ts`（storeMessage INSERT 带
  username **且** 删内部吞错 try/catch = fail-loud 死分支修复）
- `apps/channel-server/src/infrastructure/integrations/lark/events/handlers.ts`（入站补
  username **且** 5b 入站链路重排：resolve→runRules→store→抢锁→savePending+publish）
- `apps/channel-server/src/core/rules/engine.ts`（5b 重排：PendingChatTrigger / 终态字段 /
  per-handler 作用域捕获）
- `apps/channel-server/src/core/rules/rule.ts`（5b 重排：Handler 加可选第二参 ctx）
- `apps/channel-server/src/core/services/ai/reply.ts`（5b 重排：makeTextReply 只登记意图
  不发 MQ / 不取锁 / 不落 pending）
- `apps/channel-server/src/core/channels/inbound-pipeline.ts`（新增 `globalReplyToId`：
  reply_message_id 全局化）
- `apps/channel-server/src/workers/chat-response-worker.ts`（assistant 行补 username）
- `apps/monitor-dashboard/src/routes/messages.ts`（读取侧删 lark_user JOIN、改读 username 列）

（`memory.ts` 与 `handlers.ts` 各承载两批改动——username 写入 + fail-loud / username
写入 + 5b 重排——是这次 review 最需要拆开看的两个文件。）

测试代码（修改 4 个 + 新增 10 个）：
- 修改：`apps/agent-service/tests/unit/agent/tools/test_history.py`、
  `apps/agent-service/tests/unit/life/test_proactive.py`、
  `apps/channel-server/src/core/models/message-content.test.ts`、
  `apps/channel-server/src/core/channels/inbound-pipeline.test.ts`
- 新增（`??`）：`apps/agent-service/tests/unit/data/test_messages_username.py`、
  `apps/agent-service/tests/unit/data/test_messages_quick_search_global_id.py`、
  `apps/agent-service/tests/unit/chat/test_context_messages_speaker.py`、
  `apps/channel-server/src/infrastructure/integrations/lark/events/handlers.username.test.ts`、
  `apps/channel-server/src/infrastructure/integrations/memory.username.test.ts`、
  `apps/monitor-dashboard/src/routes/messages.p2pname.test.ts`、
  `apps/channel-server/src/core/rules/engine.pending-trigger.test.ts`、
  `apps/channel-server/src/core/rules/engine.pending-scope.test.ts`、
  `apps/channel-server/src/core/services/ai/reply.pending-trigger.test.ts`、
  `apps/channel-server/src/infrastructure/integrations/lark/events/handlers.inbound-order.test.ts`、
  `apps/channel-server/src/infrastructure/integrations/lark/events/handlers.multibot-pending.test.ts`、
  `apps/channel-server/src/infrastructure/integrations/lark/events/handlers.store-semantic.test.ts`、
  `apps/channel-server/src/infrastructure/integrations/memory.failloud.test.ts`、
  `apps/channel-server/src/core/channels/inbound-pipeline.real-lark.test.ts`

---

## 三、分章详解

每章统一结构：解决什么问题 → 关键设计决策 → 改了什么 → 验证状态 → 风险与未决。

### T1 — channel 四层契约 + channel 作用域身份模型（commit `00f7657`）

**解决什么问题。** 整个改造的地基。在碰任何飞书逻辑之前，先把"渠道"这层抽象的契约形态
定下来：消息怎么进来、内部长什么样、回复怎么出去、怎么判断要不要回，外加身份怎么从
channel 内 ID 翻译成全局 ID。这一步不含真正的数据迁移，只立契约和领域模型。

**关键设计决策。** 对应 spec 决策五前半段：渠道抽象切成四层契约，且契约里**刻意不允许出现
IM 专有假设**——`@`、私聊 / 群聊二元、回复树这些不能作为强制语义，判断一个字段能否进契约
的唯一标准是"换一个非 IM 的纯 HTTP 问答入口它还讲不讲得通"。所以契约里用
`conversation_scope`（字符串，常见 `direct`/`group` 但不强制二元）取代 `chat_type` 二元枚举，
用 `addressing_hints`（通用"这条冲谁来"线索）取代 `raw_mentions`，用可选 `ThreadRef` 收回复树。

**改了什么。**
- `apps/channel-server/src/core/channels/contracts.ts`（T1 落地 157 行，T2 扩到 246 行）：
  `ContentItem` 联合类型（text/image/audio/file/sticker/unsupported）、`InboundMessage`、
  `InboundAdapter`、`OutboundAdapter`、`AddressingPolicy` 五个核心契约。这里有几个值得注意的
  设计：契约三的 `AddressingDecision` 刻意不是裸 boolean 而是 `{ respond, reason }`，配套
  `enforceDecision`（`contracts.ts:141`）——`respond=false` 但 `reason` 为空字符串时**直接
  抛错**，把 spec 反复强调的"静默丢弃"在边界炸掉；契约四引入中心化的 `deliver()` 函数
  （`contracts.ts:104`），把"有回复锚点走 reply、没有退化为 send"的退化逻辑收成唯一一处，
  各 adapter 不再各自实现退化；还有 `assertValidInboundMessage`（`contracts.ts:160`）作为
  入站运行时守卫，adapter 产出形状不对的消息会在入站边界炸而不是流到下游。
- `apps/channel-server/src/core/channels/identity-resolver.ts`（70 行）：T1 的
  `InMemoryIdentityResolver` + `IdentityNotFoundError`，定义 `resolve`（正查）/ `toChannel`
  （反查）契约。
- `apps/agent-service/app/domain/chat_dataflow.py`、`app/nodes/chat_node.py`：契约层
  `user_id` 字段带上 channel 维度，保证 agent-service 中间链路能编译通过且不感知具体 channel。

**验证状态：已验证（单测）。** `contracts.test.ts`（200 行）、`identity-resolver.test.ts`
（83 行）覆盖 spec 点名的头号验收项：相同的 channel 内 chat ID 在不同 channel 下不串成同一
会话、AddressingPolicy 在 direct/group 两条路径返回正确结果、那个假想纯 HTTP 问答 channel
能只靠三件套接入不改核心。agent-service 侧 `test_chat_dataflow.py` / `test_chat_node.py` /
`test_route_chat_node.py` 覆盖契约层带 channel 后中间链路仍能跑。本会话重跑 agent-service
相关单测全绿（见 T5-5c 验证），channel-server 套件 171 pass（见 T5-5c 验证）。

**风险与未决。** 契约本身是纯定义、风险低。唯一要留意的是 contracts 里那些守卫（空 reason
抛错、形状不对抛错）是 fail-loud 设计，部署后若飞书 adapter 产出了不合规形状会直接炸——
这是刻意的，但意味着 T2 飞书 adapter 的转换必须严格守住契约，否则线上会以异常形式暴露而非
静默。这正是 T2 单测要逐类型钉死的原因。

### T2 — 飞书 adapter，行为不变回归（commit `9083da1`）

**解决什么问题。** 把现有飞书的入站解析、出站发送、bot 命中逻辑收敛成 T1 契约下的第一个
adapter，飞书行为零变化。飞书专有的命名（`im.message.receive_v1` / `union_id` / challenge /
`verification_token`）从此只允许出现在 adapter 这一个文件内部。

**关键设计决策。** 对应 spec 决策五里的内容模型扩展决策：通用 `ContentItem` 本期就必须能
忠实承载飞书现有 `MessageTransferer` 产出的**全部**内容类型（text / image / post 富文本 /
sticker / media / file / audio / 合并转发 / 分享名片 / unsupported 占位），绝不能因为引入
抽象就把"飞书发图 @ 赤尾、赤尾看图聊天"这类既有能力砍掉——非文字消息返回 null 当成"没收到"
是 spec 明令禁止的严重回归。内容模型的承载范围由所有 adapter 里能力最全的飞书决定，QQ 这期
只产出 `Text` 是 QQ 自身的范围限制、不是内容模型的限制。

**改了什么。**
- `apps/channel-server/src/core/channels/lark/lark-adapter.ts`（335 行）：`LarkInboundAdapter`
  / `LarkOutboundAdapter` / `LarkAddressingPolicy` 三件套。几个关键点：`parse` 被刻意做成
  **同步纯转换、零 I/O**（`lark-adapter.ts:54-60` 注释解释为什么——现状 `MessageTransferer`
  之所以 async 只因为它额外调了走身份/DB 的 `Message.fromEvent`，那一步不属于 parse 职责，
  留给下游）；`verify` 恒返回 true 并在注释里说明为何安全（飞书 token+encrypt_key 校验在
  channel-proxy 入口已完成，事件到本服务时已解密验证过）；内容映射口径要求与现状
  `MessageTransferer` / `MessageContentUtils` 逐字一致。
- `contracts.ts` 在本 commit 从 157 行扩到 246 行（补齐 `deliver` / 守卫等）。

**验证状态：已验证（单测，代码侧）。** `lark-adapter.test.ts`（446 行）覆盖飞书全部消息
类型的转换、与现状行为一致、任何非文字消息不得被丢弃。**这里有一个 owner 必须知道的边界：
T2 的飞书 adapter 在本 commit 还没接进真实链路（接线是 T5-5b 的事）**，没有真实飞书流量
经过新 adapter，所以 T2 阶段不做 coe 端到端，验收口径明确放在代码侧。真实链路端到端的飞书
行为零变化回归（私聊 / 群聊 / @bot / 不 @bot / 图片富文本 sticker 语音文件等）依赖接线，
spec 已把它统一并入 T5（5e coe）验收，T2 不做。

**风险与未决。** adapter 单测断言"转换结果与现状一致"，但单测断言的是测试数据下的等价，
真实飞书事件的全字段覆盖只能靠 coe 真机回归证明。"飞书逐场景零变化"这条硬约束在代码侧已
钉，真机层面是悬而未决的，必须并入 5e。

### T3 — lark-* 改名 channel-*（commit `ff1d893`）

**解决什么问题。** 对应 spec 决策一：项目重构规范明令禁止任何兼容层 / 过渡层。如果保留飞书
链路、另起一条 QQ 链路并行，飞书耦合永远清不掉、两条链路长期重复。所以选择把飞书专属层
彻底重写成平台无关抽象、飞书作为第一个 adapter——这就要求服务名也从飞书专属的 `lark-proxy`
/ `lark-server` 改成渠道无关的 `channel-proxy` / `channel-server`。

**关键设计决策。** 这是一次几乎纯机械的重命名（diff-stat 227 文件、+193/-193，绝大多数是
`apps/{lark-proxy => channel-proxy}` / `apps/{lark-server => channel-server}` 的路径
rename，内容 0 改动），但爆炸半径覆盖部署面：目录名、Dockerfile、Makefile 的 SIBLINGS、
服务间调用默认 URL、PaaS 应用注册、ConfigBundle、镜像与多 Deployment 映射
（channel-server 镜像产出 channel-server / recall-worker / chat-response-worker 三个独立
Deployment；agent-service 产出 agent-service / vectorize-worker）。

**改了什么。** 路径 rename 为主，外加散落的硬编码改名：`Makefile`、`README.md`、
`apps/api-gateway/config/routes.yaml`、`infra/k8s/` 下若干 yaml、`docs/` 若干文档、
`bun.lock` / `package.json`（workspace 改名导致 lockfile 作用域前缀整行变——这是已知会
让 git diff 行级看着像依赖漂移的现象，实际是改名）。验证已确认 `apps/` 下不再有 `lark-*`
目录残留，旧 `apps/lark-server/src` / `apps/lark-proxy/tests` 等老文件已在 diff 里删除而非
保留 shim。

**验证状态：rename 本身可机械验证（无残留旧目录，已 grep 确认）；构建 / coe 回归未做。**
spec T3 验收要求"coe 泳道部署后飞书回归通过、PaaS 和 ConfigBundle 都指向 channel-*"——
这部分属于部署侧验收，本 PR 阶段没有部署，未验证。

**风险与未决。** spec 明确把"PaaS 应用改名（注册新应用、迁移 ConfigBundle、下线旧应用）"
标为**高风险部署项，上线前要单独跟用户确认**。代码侧改名干净，但部署侧的 PaaS 应用注册 +
ConfigBundle 迁移是上线时才执行的、未演练的高风险动作。

### T4 — bot_config 多 channel 化（commit `a14b957`）

**解决什么问题。** bot_config 是跨服务共享表，原来飞书的五件套凭据（app_id / app_secret /
encrypt_key / verification_token / robot_union_id）散在独立列里、写死飞书。要支持多 channel，
得加 `channel` 列、把各 channel 自己的凭据收进一个不约束形状的 `credentials` JSONB 列。

**关键设计决策。** 对应 spec 决策一爆炸半径里的 bot_config 部分，外加 spec "bot_config 多
channel 化后的形态"那节：飞书凭据全部迁进 `credentials` JSONB、旧的五个独立列直接删（不留
双形态，项目硬规范禁止兼容层）。bot 加载链路读到一条记录后按 `channel` 解析出该 channel 的
三件套，框架不解释 `credentials` 里的具体字段（那是各 adapter 的事）。`getBotAppId` /
`getBotUnionId` 等签名保持不变，只改内部从 JSONB 取值——调用方无感。

**改了什么。**
- `apps/channel-server/src/infrastructure/dal/entities/bot-config.ts`：实体加 channel +
  credentials JSONB、删五个旧列。
- `apps/channel-server/src/core/services/bot/lark-credentials.ts`（58 行，新增）+
  `bot-var.ts`：从 `credentials` JSONB 取飞书凭据，运行期拒绝空字符串（length===0 即抛错）。
- `apps/channel-server/src/core/channels/channel-registry.ts`（116 行，新增）：bot 加载
  按 `channel` 分发的唯一入口。飞书是已落地 adapter；**QQ 是占位三件套——能被识别、加载
  链路不挂，但占位的任何方法被真正调用就明确抛 `not implemented`**（`channel-registry.ts:31`
  `notImplemented`），这正是"禁止静默丢弃"的体现：宁可在边界炸，绝不无声吞一条 QQ 消息。
- `apps/channel-server/src/core/services/bot/multi-bot-manager.ts`：接 channel-registry，
  新增 `getChannelTriple(botName)`。
- 跨服务调用方全部迁到读 JSONB：`apps/tool-service/app/infrastructure/lark_client.py` +
  `app/orm/bot_config.py`、`apps/channel-proxy/src/bot-manager.ts`、agent-service
  `app/data/queries/persona.py`。persona.py 这处尤其要注意一个跨 channel 命名空间陷阱
  （commit message + 代码注释都点了）：mention → persona 路由的 `MENTIONED_PERSONAS_SQL`
  改成读 `credentials->>'app_id'` 后**必须限定 `channel = 'lark'`**——QQ 的 credentials
  同样有 app_id，不加 channel 约束，飞书 mention 传进一个恰好等于某 QQ bot app_id 的值时
  会误命中 QQ persona。加 `channel='lark'` 恢复与旧飞书裸 app_id 列等价的语义。
- `docs/plan/multi-channel-T4-bot_config-migration.sql`（72 行）：迁移 SQL 产物，单事务
  （加列 → 回填 → 校验 → 删旧列），校验用 `NULLIF(x,'') IS NULL` 把空字符串也挡在迁移期
  （与运行期 invariant 对齐，凭据缺失/空宁可迁移期炸）。**此 SQL 是产物，不在本 PR 执行。**

**验证状态：已验证（单测）。** `lark-credentials.test.ts`（50 行）、`bot-var.test.ts`
（54 行）、`channel-registry.test.ts`（77 行）、`tool-service` 的
`test_lark_client_credentials.py`（53 行）、agent-service 的
`test_persona_bot_config_credentials.py`（36 行）覆盖凭据从 JSONB 取、签名不变、未知
channel fail-closed、persona 路由限定 lark。**注意：本会话重跑 channel-server 套件，
`bot-var.test.ts` 的 3 个用例 fail（见全局未决项汇总的"3 个 bot-var.test.ts 失败是基线
既存"一条，是 `bun mock.module` 进程级污染导致的基线既存失败，非本次引入）。** schema 变更（加列 / 删旧列 / 凭据回填）属破坏性变更，必须
在 coe-* 独立泳道做，本 PR 未 apply。

**风险与未决。** SQL 未 apply 是个硬前置——`lark-credentials.ts` 运行期就从 JSONB 取且拒
空字符串，DDL 没 apply 时 channel-server 一启动就会因为旧列不存在 / credentials 为空而炸。
T4 部署强依赖 T4 SQL 先在 coe apply 并通过完整性校验。

### T5-5a — 三类身份映射表 + DB IdentityResolver（commit `04a15f4`）

**解决什么问题。** 对应 spec 决策二：用户、会话、消息三类身份全部 channel 作用域化（不只
迁用户）。补查发现 `chat_id` / `message_id` / `root_message_id` 在 SQL 和 Qdrant 里都被
当全局唯一裸 ID 用，而 QQ 的群 ID 和飞书的 chat_id 是两个独立命名空间——只迁用户身份的话，
QQ 接进来会话维度和回复链路会被不同平台的会话 ID 撞车击穿。所以三类身份必须一起做隔离。
5a 是数据落地：建三张映射表 + DB 版 IdentityResolver。

**关键设计决策。**
- 三张结构相同的表 `identity_user` / `identity_chat` / `identity_message`，
  `(channel, channel_*_id) → internal_*_id`，复合唯一约束保证同一 channel 内同一外部 ID
  只映射一个全局 ID。`internal_*_id` 是主键、用 **ULID**（26 位 Crockford base32，时间
  前缀单调递增、做主键索引比随机 UUID 友好，128bit 熵保证跨 channel 不撞）。
- **并发首次出现的收敛交给 PG 引擎在单条 SQL 内做**，不用应用层 check-then-insert-catch：
  写路径是 `INSERT ... ON CONFLICT ON CONSTRAINT uq_identity_*_channel DO NOTHING` 配一个
  CTE，`DO NOTHING` 不 RETURNING 时用 `UNION ALL ... SELECT ... LIMIT 1` 回取已存在行，
  保证恒返回收敛后的 internal id（一条语句、事务安全、不依赖隔离级别）。
- 把 forward-key 复合唯一约束冲突（已被 ON CONFLICT 吸收）和 internal 主键 ULID 冲突
  （冒到应用层的 23505）严格区分：前者不该冒上来，后者翻译成 `PrimaryKeyConflictError`
  让 resolver 换 ULID 重试。**约束名 `uq_identity_*_channel` 被 SQL `ON CONFLICT ON
  CONSTRAINT` 按名引用，是 DDL / TypeORM 实体 / identity-store.ts 三处必须严格一致的契约
  锚点，不可改名。**

**改了什么。**
- `apps/channel-server/src/core/channels/db-identity-resolver.ts`（184 行）：DB 版
  resolver，依赖一个 `IdentityStore` 抽象（单测走内存版 FakeIdentityStore，不连真实 DB）。
- `apps/channel-server/src/infrastructure/dal/repositories/identity-store.ts`（152 行）：
  生产运行时 `TypeOrmIdentityStore`，承载上面那段单 SQL upsert + 23505 约束名区分逻辑。
- `apps/channel-server/src/core/channels/identity-resolver-contract.ts`（113 行）：**可
  复用契约测试套件**。把"对任意 IdentityResolver 实现都必须成立"的不变量（resolve 幂等、
  跨 channel 不串、三类命名空间独立、全局唯一、toChannel 正反一致、反查不到必报错、并发
  resolve 同一 key 不产生重复 ID 等）抽出来，让 T1 的 InMemory 版和 T5 的 DB 版跑同一组
  断言，防止 DB 版偷偷放宽语义。
- `apps/channel-server/src/infrastructure/dal/entities/identity-mapping.ts`（62 行）+
  `entities/index.ts` + `ormconfig.ts`：三个 TypeORM 实体注册。
- `docs/plan/multi-channel-T5-identity-mapping-tables.sql`（60 行）：建表 SQL 产物，单事务，
  **不在本 PR 执行**（属于 coe-* 破坏性变更）。

**验证状态：已验证（单测）。** `db-identity-resolver.test.ts`（192 行）+ 共享契约套件
覆盖上述全部不变量，包括"并发 8 次 resolve 同一 (chat,qq,race-1) 收敛成同一个全局 ID 且
反查一致"这条 spec 头号验收点。本会话 channel-server 套件整体 171 pass（含这些）。
**真实 PG 行为（ON CONFLICT 收敛、约束名匹配、ULID 主键冲突重试）未连真实 DB 验证**——
单测走内存 FakeIdentityStore，PG 层只能 coe 真机证明。

**风险与未决。** 这是整个迁移最重的一刀的"代码侧"，但**表还没建、历史飞书 ID 还没回填、
7 张业务表和 Qdrant 还没原地重写**——这些都是 5e（coe cutover）的事。5a 单独看是"代码
就绪、数据未动"的状态。约束名三处一致是个脆弱契约，改任意一处忘了另两处，运行期 ON
CONFLICT 会按错约束名报错。

### T5-5b — 平台无关 runRules + 契约链接进真实链路（commit `0229a5d`）

**解决什么问题。** 5a 把映射表和 resolver 备好了，5b 是真正把整套渠道抽象接进真实消息
链路，并把规则引擎 `runRules` 从飞书专属重写成平台无关的统一引擎。这是设计上翻转最大的
一步。

**关键设计决策（对应 spec 决策四 + 决策五的翻转，最值得 owner 细看）。**

第一，**决策五的翻转**：原本设想"channel-server 里那套 `runRules` 保持飞书 native 不动、
新抽象只旁挂"。实践证明不成立——规则引擎还吃飞书专属富对象的话，新的渠道命中判断对飞书
只是个并行摆设；而 QQ 这类新渠道消息根本进不了飞书专属的 `runRules`，"要不要回"彻底没着
落。所以推翻"中间 runRules 不动"，改成：`runRules` 是**一套平台无关的统一引擎、所有渠道
走同一套**，消息一进来由 adapter 归一化成平台无关的 `RuleMessage` 再往下走。

第二，**首版渠道范围收紧**：经 codex 实查确认，原以为可跨渠道复用的"帮助 / 指令 / 复读 /
余额 / 撤回 / 水群 / 发图 / 表情包"等指令实际全部深度绑死飞书（引用飞书模板 / SDK / 卡片
/ union_id / admin 判定）。所以**只有 persona 文本主链路（用户找赤尾聊天）真正平台无关、
默认全平台；其余所有指令首版一律显式声明 `channels: ['lark']`，内部逻辑一行不动、飞书行为
零变化**。`runRules` 分发时按当前消息 channel 过滤，QQ 消息自然 miss 掉 lark-only 指令。
不为非飞书渠道写防御性降级——飞书专属能力落到不支持的渠道就让它自然 fail。

第三，**决策四：runRules 单一终态出口模型**。`runRules` 原来规则不过会静默 break、连日志
都没有，消息消失查都查不到。重写成：每一条进 `runRules` 的消息无论走哪条退出路径，都必须
收敛到一个唯一、明确、可查的 `RuleTerminalState`（kind ∈ `blocked` / `responded` /
`handler_error` / `rule_error` / `no_match`）。所有退出点都 `return` 一个终态（类型系统
强制覆盖每条路径），黑名单挡掉 / channel 过滤跳过 / 规则不通过 / botRole 不匹配 / handler
抛异常 / fallthrough 走完——每一条退出和每一次跳过都进 `skipped[]` 留痕，函数末尾
`logTerminalState` 落一条可查日志。`engine.ts` 注释逐路径标了"退出路径 1~7"。

第四，**钉死的契约链顺序**（spec 整体分层，不可调换）：
`adapter.parse → AddressingPolicy.decide + enforceDecision（前置总闸）→
IdentityResolver.resolve（换全局 ID）→ runRules → storeMessage（写全局 ID）→ 发 MQ`。
理由是后面几步彼此强依赖：`AddressingPolicy` 是不依赖 DB 的纯判定，必须前置在 resolve
之前——即使后面 resolve 的 DB 挂了，这一步也已先产出"为什么不回"的可查结论；`resolve`
又必须前置在 runRules / 存库 / 发 MQ 之前——黑名单规则要靠全局 ID 才查得到、存库和发 MQ
必须带全局 ID 不能漏裸 ID 下去。

第五，**fail-loud 铁律及其边界**。契约链（parse / decide / resolve）任一步失败，该消息必
fail-loud——不写库、不发 MQ、记可查错误日志，**绝不退回飞书裸 ID 继续往下走**（那会把
"渠道化失败"伪装成"一条正常飞书消息"，污染数据且丧失可查性）。但 fail-loud 的边界划得很
清楚：它**只约束契约相关的写入与下发**，不约束飞书 native 那些跟新身份契约毫无关系、本来
就用飞书裸 ID 的既有副作用（识图管线、`bot_chat_presence` upsert、MessageTransferer）——
这些副作用在现状里就排在契约链之前执行，按"飞书行为零变化"必须原样、原位置（仍在契约链
之前）继续跑。**绝不能为了迁就 fail-loud 把这些副作用挪到契约成功之后**，否则 5b 这个
DB 映射表还没 apply 的不可部署中间态，飞书识图 / 在线状态全部不执行 = 飞书整体回归。

**改了什么。**
- `apps/channel-server/src/core/rules/engine.ts`（重写，340 行）：`runRulesWith`（可注入
  内核，依赖全从参数进、单测纯跑）+ `runRules`（真实链路入口，组装依赖、落终态日志）+
  `RuleTerminalState` / 七条退出路径 + `ruleSupportsChannel` channel 过滤 + `chatRules`
  数组（10 条 `channels:['lark']` 指令 + 末尾 1 条不声明 channels 的 persona 主链路
  `makeTextReply`）。
- `apps/channel-server/src/core/rules/rule-message.ts`（126 行，新增）：`RuleMessage`
  平台无关统一视图 + `LarkRuleContext` 旁挂飞书强绑上下文 + `buildLarkRuleMessage`
  （飞书侧从 Message 富对象派生、平台无关字段全委托 Message 现有等价方法，飞书逐场景行为
  零变化）+ `requireLarkContext`（lark-only handler 取回 Message，缺 context 直接
  fail-loud，绝不静默降级）。
- `apps/channel-server/src/core/rules/rule.ts`（重写，120 行）：规则谓词改吃 RuleMessage，
  `NeedRobotMention` 等改用 `setBotIdentityResolver` 注入的 botIdentity。
- `apps/channel-server/src/core/channels/inbound-pipeline.ts`（132 行，新增）：
  `runInboundContractChain`——把 parse → decide+enforce → resolve 三契约按钉死顺序串起来，
  产出"是否响应 + 全局 internal_*_id"，契约链任一失败返回 `ok:false` 让调用方 fail-loud。
- `apps/channel-server/src/core/channels/outbound-pipeline.ts`（61 行，新增）：
  `reverseResolveForLark`——出站把全局 ID 经 `toChannel` 反查回飞书裸 ID，内含
  `channel==='lark'` 边界断言（T6 接 QQ 后误把非飞书全局 ID 喂飞书富文本发送器会在此炸）。
- `apps/channel-server/src/infrastructure/integrations/lark/events/handlers.ts`
  （入站接线，本 PR 内又做了一次入站链路重排，见下方"5b 入站链路重排"专门小节）。
  改造后形态：飞书 native 副作用（`bot_chat_presence` upsert + 识图管线）保留原位置不动
  （`handlers.ts:84-115`，在契约链之前）→ 装配三件套（装不出 fail-loud，`handlers.ts:124-134`）
  → `runInboundContractChain`（`handlers.ts:136-149`）→ fail-loud 处理（parsed_null 跳过 /
  contract_chain_error 不写不发不退裸 ID，`handlers.ts:151-167`）→ `buildLarkRuleMessage`
  派生平台无关消息（`handlers.ts:172-183`）→ `runRules` → `storeMessage` 写全局 ID →
  拿到去重锁才发 MQ。
- `apps/channel-server/src/workers/chat-response-worker.ts`（+69 行）：出站接线。
  `reverseResolveForLark` 反查回飞书裸 ID 喂现状飞书富文本出站路径（sendPost/replyPost
  + 识图 + markdown→post 全部保留不动，不塞进纯文本 OutboundAdapter 否则丢图片丢格式），
  bot 自己的回复消息也正查分配全局 internal_message_id 后落库（assistant 行身份列同样全局）。
- `apps/channel-server/src/core/services/ai/reply.ts`（重写）+ admin/balance、
  command-handler、delete-message、gen-history、repeat-message、media/meme、media/send-photo
  等指令：按 RuleMessage + LarkRuleContext 接口适配，**内部业务逻辑不重新设计**，飞书逐
  场景行为零变化。
- 旧旁挂 pipeline scaffolding 已删除（重写到目标、无 compat shim）。

#### 5b 入站链路重排（owner 必须细看：原 5b commit 的入站顺序与 5b 定稿要求不符，本 PR 内修复）

写完 5b 之后的复盘里发现一个**5b commit `0229a5d` 自带的链路顺序错误**（git 证据已确认
不是 5c 或本批新引入）：5b 接线把入站顺序写成了 `resolve → storeMessage → runRules`，
也就是先存库再跑规则；但 5b 定稿要求的钉死顺序是
`resolve → runRules → 存库 → 发 MQ`。两者差在"runRules 与 storeMessage 谁先"，以及更要命
的——发 ChatTrigger 到 MQ 这一步当时藏在 `runRules` 内部的 persona handler 里（早于
storeMessage 完成）。后果是下游 agent-service `chat_node.py` 的
`find_message_content(message_id)` 强依赖"这条消息已落库"，MQ 比 storeMessage 先到时它会
读空、直接 emit "未找到相关消息记录"短路，整条对话哑掉。这是 5b 既有缺陷，经与用户确认
**在本 PR 内修复**，不单独立项。

修复的核心是把"发 ChatTrigger 到 MQ"从 `runRules` 内部抽出来、推迟到 storeMessage 成功
之后，且做法不破坏单一终态出口与多 bot 去重语义：

- **发 MQ 的唯一一处**原本在 `reply.ts` 的 persona handler `makeTextReply` 内（全代码库
  发 ChatTrigger 仅此一处）。现在 `makeTextReply`（`reply.ts:83-128`）在 runRules 阶段
  **不再实际 publish、不取去重锁、不落 agent_responses pending 行**，只做平台无关纯预备
  （生成 session_id、构造 chat.request 载荷、把 pending 行落库逻辑包成 `savePending` 闭包），
  通过 handler 新增的可选第二参 `ctx.registerPendingChatTrigger`（`reply.ts:116-121`）把
  "待发意图"（payload / lane / dedupeKey / savePending 闭包）登记回引擎。`AgentResponse`
  仓储逻辑仍只在 `reply.ts` 一处（闭包内），不泄漏到 handlers。
- **引擎 `engine.ts`**：新增 `PendingChatTrigger` 接口（`engine.ts:55`）、`RuleHandlerContext`
  handler 第二参契约（`engine.ts:67`）、`RuleTerminalState.pendingChatTrigger` 唯一终态
  字段（`engine.ts:85`）。每个命中 handler 在自己的执行作用域内单独捕获一个本次专属的
  `scopedPending` + `scopedCtx`（`engine.ts:236-242`），handler 只能写自己这次的 pending；
  终态只绑定"产生该终态的那个 handler"本次注册的 pending（`engine.ts:263`、`274`），
  handler 抛错则该次 pending 随作用域丢弃（失败路径绝不发 MQ）。这是 codex 建议 1 采纳的
  结果——去掉了原本"循环结束用最新 pending 回填"那种容易把靠后 handler 的 pending 错绑给
  前一个终态的防御写法，改成 per-handler 作用域捕获，与决策四单一终态出口同构、并发安全。
  `rule.ts` 的 `Handler` 类型相应加上可选第二参 `ctx?: RuleHandlerContext`（向后兼容，
  其余 handler 不声明此参即可，`rule.ts:16-22`）。
- **接线点 `handlers.ts`**：顺序硬钉成 `resolve(契约链) → runRules → storeMessage(无条件) →
  抢去重锁 → 拿到锁才 savePending + publish`（`handlers.ts:201-276`）。`storeMessage` 无
  条件执行、不看 terminal kind——非 @bot 群消息复读照常入库，飞书逐场景零变化；只有
  `terminal.pendingChatTrigger` 存在（仅 persona 文本主链路命中）才会走后面的取锁 / 发 MQ。
  去重锁 `setNx` 从原来 `makeTextReply` 内部移到了 `handlers.ts` publish 紧邻处
  （`handlers.ts:252`），抢到锁才 `savePending()` 再 `publish`（`handlers.ts:262-269`）——
  这把锁后移避免"拿锁后 storeMessage 失败导致锁空占 60s"，把 pending 行落库也压到抢锁
  之后（codex 必改 2：否则多 bot 同群时未抢锁的 bot 也会写一条永不完成的孤儿 pending 行）。
- **顺带**：`inbound-pipeline.ts` 新增 `globalReplyToId`（`inbound-pipeline.ts:56-59`、
  `119-136`），把飞书"回复某条消息"锚点 `parentMessageId` 与 root 一样经
  `IdentityResolver.resolve` 翻成全局 internal_message_id；`handlers.ts` 的 `storeMessage`
  改用 `reply_message_id: chain.globalReplyToId`（`handlers.ts:230`）。这一步把原本标为
  P0 的 reply_message_id 仍写裸 ID 一并补掉——见全局未决项汇总，已从"未决"改为"已决/完成"。

**这次重排没有真机端到端验证（也无法验证）**：5b 仍是不可部署中间态（映射表未 apply、
契约链必然失败），重排后的真机端到端验证统一并入 5e（coe）。代码侧靠 TDD 钉死。

**验证状态：已验证（单测，本会话实跑）；不可部署中间态，真机验证未做也做不了。**
`engine.test.ts`（单一终态出口的每条退出路径都留下唯一可查终态记录）+
`rule-message.test.ts`、`inbound-pipeline.test.ts`、`outbound-pipeline.test.ts`、
`reply.test.ts` 覆盖契约链顺序、fail-loud、反查边界断言。本次重排额外新增多组真链路
测试（见 PR228 第二节文件清单与"codex T3 三轮评审与整改"一节）：
`engine.pending-trigger.test.ts` / `engine.pending-scope.test.ts`（pending 登记 + per-handler
作用域捕获）、`reply.pending-trigger.test.ts`（makeTextReply 只登记不发）、
`handlers.inbound-order.test.ts`（resolve→runRules→store→publish 顺序 + 非 @bot 群消息
照常入库）、`handlers.multibot-pending.test.ts`（多 bot 并发只有抢锁者 savePending+publish、
其余不留孤儿 pending）、`handlers.store-semantic.test.ts`（storeMessage 成功语义 =
message_id 可回查）、`inbound-pipeline.real-lark.test.ts`（真实 lark 链路、消除原 mock 盲区）。
本会话 channel-server 全套件 **201 pass / 3 fail**（3 fail 全是 `bot-var.test.ts`，T4
bot_config 既存基线失败、本批零新增）；`bunx tsc --noEmit` 零新增错误。

**风险与未决。** 这是全 PR 风险最集中的一步。代码侧改动巨大，飞书逐场景零变化只在单测
层面钉了，真机回归（私聊 / 群聊 / @bot / 不 @bot / 非文字消息存库 / 图片进识图 / 看图
聊天 / 本次重排后的入站顺序与多 bot 去重）全部悬而未决、必须并入 5e。本次重排引入的
入站顺序（store 先于 publish、锁与 publish 紧邻）只在 TDD 单测层面证明，**真机端到端
（含 agent-service `find_message_content` 不再读空）属未验证、并入 coe 5e**。
另：本会话 `bunx tsc --noEmit` 在 `chat-response-worker.ts` 报了 2 个**与本次身份 / 重排
改动无关的既存 TS 错误**（一个 Buffer/ArrayBufferView 类型不兼容、一个
`{ image_key? }.data` 属性不存在，都在 5b 已提交代码里、非本批引入），如实记录在此供
owner 知晓，属 5b 已提交代码的既存类型债。

### T5-5c — 读取侧切全局 ID 零 fallback（工作区未提交）

**解决什么问题。** 5b 写入端已经写全局 ULID 了（IdentityResolver 在 storeMessage 之前 resolve
飞书裸 ID 成全局 ULID）。问题是：写入端一旦写全局 ULID，**读取端原来那套靠飞书 ID 形状取
数据的逻辑就全坏**。最典型的是查用户名——读取端原来
`JOIN lark_user ON conversation_messages.user_id = lark_user.union_id` 拿显示名，现在
`user_id` 是全局 ULID、不再等于任何 `union_id`，JOIN 全 miss，线上消息列表和 agent 看到
的历史里人名会全错或全空。5c 就是把所有"靠飞书 ID 形状读数据"的下游读取方切成读新全局
口径，**零 fallback**（不再 `COALESCE` 回旧字段兜底——兜底会掩盖问题，且全局化后兜底值
本身也是错的）。

**关键设计决策。** 给 `conversation_messages` 加一个冗余列 `username VARCHAR(100) NULL`，
写入端把发送者显示名一并落库，读取端直接读这列、不再 JOIN `lark_user`。选这条而不是
"读取时实时反查身份映射表"，是因为写入端拿名成本最低（入站链路本来就解析过 `LarkUser`）、
读取端零 JOIN 最简单可靠。`username` **刻意可空**：历史数据迁移前必然为空、写入端拉不到
名时也留空、**不写脏占位**（读取端按空处理）。

**改了什么。**

主题一，新增 `username` 冗余列（DDL 契约 + 双端实体声明，**DDL 本次不 apply**）：
- `apps/agent-service/app/data/models.py`：SQLAlchemy 实体加
  `username: Mapped[str | None] = mapped_column(String(100), nullable=True)`，紧跟
  `user_id` 之后并写了为什么 nullable 的注释。
- `apps/channel-server/src/infrastructure/dal/entities/conversation-message.ts` +
  `packages/ts-shared/src/entities/conversation-message.ts`：两份 TypeORM 实体同步加
  `@Column({ length: 100, nullable: true }) username?: string`（两份必须一致，否则
  channel-server 和别的 TS 服务对同一张表的认知会漂移）。

主题二，agent-service 读取侧切 username 列（核心刀，零 fallback），文件统一是
`apps/agent-service/app/data/queries/messages.py`：
- `find_username(user_id)`：从 `select(LarkUser.name).where(union_id == user_id)` 改成
  查该全局 user 最近一条非空 username 的消息行（`ConversationMessage.username`，过滤
  `is_not(None)`，`order_by create_time desc limit 1`），无 JOIN 无 COALESCE。签名
  `(user_id: str) -> str | None` 不变，调用方无感。
- `find_context_messages_for_anchors`：返回从
  `list[tuple[ConversationMessage, LarkUser]]` 改成
  `list[tuple[ConversationMessage, str | None]]`，删 `.join(LarkUser, ...)`，
  改 `select(ConversationMessage, ConversationMessage.username)`。**这是返回结构变更**，
  唯一消费方 history.py 已同步改。
- `find_messages_with_user_chat_persona_by_root` + `find_messages_with_user_chat_persona_in_chat`
  （两个 quick-search 同构 query）：`LarkUser.name.label("username")` +
  `.outerjoin(LarkUser, ...)` 改成 `ConversationMessage.username.label("username")`、
  删 outerjoin。返回 tuple 形状不变，只换 username 来源。这两个是"查用户名刀"的同类
  漏网项，本轮一并补修。

主题三，history.py 渲染侧按行级 / role 取名（codex T3 评审后修订）。这是 codex 拍出来
的、比单纯切 JOIN 更隐蔽的问题：`find_username` 改成"按全局 user_id 查该 user **最近一条**
非空 username"后，如果同一全局 user 在不同渠道用过不同显示名，`check_chat_history` 渲染
一段历史会把老消息的说话人显示成该 user 最新的名字——**跨消息 / 跨渠道串名**。正确做法是
读这条消息行**自己**的 username 冗余列（行级本意 = 发这条消息当时的发送者名）。
`apps/agent-service/app/agent/tools/history.py`：`check_chat_history` 非 assistant 行从
`await find_username(msg.user_id)` 改成 `msg.username or "?"`（直读本行冗余列），顶部
import 去掉 `find_username`；`search_group_history` 解包从 `for msg, user in rows` 改成
`for msg, username in rows`，渲染从 `user.name` 改成按 role 派生（assistant 行显示「我」、
user 行 `username or "?"`，与本文件 check_chat_history 风格一致，不再显示 botName）。

主题四，channel-server / monitor-dashboard 写入 + 读取对齐：
- 写入侧（给已有 `storeMessage` 调用补 `username` 字段）：
  `chat.ts` 的 `ChatMessage` 加可选 `username`；`memory.ts` 的 INSERT values 加
  `username: message.username ?? undefined`（没有就落 null，不抛不写脏占位）；
  `handlers.ts` 入站 user 消息 `storeMessage` 加 `username: message.senderInfo?.name`
  （来源是 `MessageBuilder.buildMetadataFromEvent` 已按 union_id 拉过的 LarkUser 行，
  不新造数据源，拉不到留空）；`chat-response-worker.ts` assistant 回复落库
  `username: botName || context.getBotName() || undefined`（用现成已解析值，不新造数据源，
  让列尽量非空一致，读取端按 role 派生显示）。
- `memory.ts storeMessage` 的 **fail-loud 死分支修复**（与 5b 入站重排配套，必须一起看）：
  原 `storeMessage`（`memory.ts`）整段包在一个 catch 里，DB 出错只 `console.error` 后返回
  void。5b 入站重排在 `handlers.ts` 新增了"storeMessage 失败 → fail-loud（不 savePending /
  不 publish）"的 try/catch；但 `storeMessage` 内部把真实 PG 故障吞成 void、正常返回，
  handlers 那段 fail-loud 对真实 DB 故障就是个**永远进不去的死分支**。修法是删掉
  `storeMessage` 内吞错的 try/catch（`memory.ts:26` 起，函数体不再包 try），让真实故障
  自然上抛、handlers 的 fail-loud 才生效。去重不受影响——`.orIgnore()` 的 `ON CONFLICT
  DO NOTHING` 由 PG 在 SQL 层吃掉冲突、`execute()` 正常返回（identifiers 为空、行已存在），
  根本不走任何错误路径。全覆盖核实 `storeMessage` 真实调用方只有 2 处：入站 `handlers.ts`
  （正是上面新加的 fail-loud）、出站 `chat-response-worker.ts`（worker 自身已有 try/catch
  兜底，且飞书消息在 storeMessage 落库前就已发出，落库失败不影响用户已收到的回复）。
- 读取侧 `apps/monitor-dashboard/src/routes/messages.ts`：`/api/messages` 主查询从
  `CASE ... COALESCE(lu.name, msg.user_id) END` 改成
  `CASE WHEN role='assistant' THEN '赤尾' ELSE msg.username END` 并删
  `.leftJoin('lark_user', 'lu', 'msg.user_id = lu.union_id')`；p2p 会话取名子查询抽成
  模块级常量 `P2P_NAME_SQL`，从 `LEFT JOIN lark_user + COALESCE(lu.name, cm.user_id)`
  改成直读 `cm.username` 并**加 `AND cm.username IS NOT NULL` 过滤**——这条过滤是关键：
  查询用 `DISTINCT ON (cm.chat_id) ... ORDER BY cm.chat_id, cm.create_time DESC` 取每个
  会话最新一条 user 消息的名字，如果最新那行 username 恰好为空（写入端拉不到名时会留空），
  DISTINCT ON 会选中空行把本来更早一条可用的名字丢掉，加 `IS NOT NULL` 让它跳过空行落到
  最近一条有名字的消息上。

确认"不用改"的地方（经判断、不是遗漏，仅补契约测试钉死"ID 当不透明字符串透传、不对 ID
形状做假设"防回归）：`app/life/proactive.py` 的 `target_message_id.isdigit()` 分支本意是
"DB 自增 row id vs message_id"二选一、不是"飞书裸 ID vs 全局 ULID"判断，全局 ULID 非纯
数字走 `find_message_by_id` 那条、与原行为一致，跨会话拒绝靠字符串相等不受 ID 形状影响，
无需改；`history.py` 的 Qdrant chat_id filter 拿 chat_id 当不透明字符串做等值匹配、无
飞书 ID 形状假设，写读同一全局 chat_id 就自洽，无需改；`models.py` 的 message_id /
user_id / chat_id / root_message_id 都是 `String(100)`、本就不透明、全局 ULID 照样存得下，
列定义无需动。

调用方全覆盖核实：`find_context_messages_for_anchors`（返回结构变了）全仓 grep 确认唯一
消费方是 history.py 的 `search_group_history`、已同步改；`find_username`（语义变了签名没变）
范围外还有 `_timeline.py` 和 `app/nodes/memory_pipelines.py` 两处调用，看过它们要的本来
就是"该 user 最近的名"、正是新 `find_username` 行为、不受影响、本次刻意不动；
`find_group_members` 走 `LarkGroupMember.union_id` 群花名册、是合法独立路径、不是同一个
反模式、本次不动。

**验证状态：已验证（单测，本会话实跑）。** 本会话实跑结果：
- agent-service：`uv run pytest tests/unit/data/test_messages_username.py
  tests/unit/data/test_messages_quick_search_global_id.py
  tests/unit/agent/tools/test_history.py tests/unit/life/test_proactive.py -q`
  → **27 passed**。新增 `test_messages_username.py`（钉死 find_username 读
  conversation_messages.username 不走 lark_user、无行返回 None、新返回结构是
  `(ConversationMessage, str | None)`）、`test_messages_quick_search_global_id.py`
  （钉死两个 quick-search query 读 username 列不再 JOIN lark_user）。
- channel-server：`bun test` → **201 pass / 3 fail**（含本次 5b 入站重排 + fail-loud
  死分支修复新增的多组真链路测试；3 fail 是 `bot-var.test.ts`，T4 bot_config 既存基线
  失败、`bun mock.module` 进程级污染，本批零新增）。
- **全程用 mock、未连真实 DB / Qdrant**；DDL（`ALTER TABLE conversation_messages
  ADD COLUMN username`）本次未执行——实体声明只是契约，部署前必须先确认 DDL 就绪。
  入站 `senderInfo.name` 实际是否非空靠读代码证明不了，必须 coe 真机验证。出站
  chat-response-worker 的 PG 故障路径也无集成测试（见全局未决项），coe 需专项验证。

**风险与未决。** 见全局未决项：reply_message_id（已全局化完成）、/api/users + lark_user
表级处置 gap、senderInfo.name 非空 + DDL 未 apply + 整体一致性 + 出站 PG 故障路径专项
并入 5e。

---

## 四、codex T3 三轮评审与整改（交代 review 严谨度）

5c 读取侧 + 5b 入站重排这一批，前后过了 **三轮 codex T3 外部评审**，每轮的反馈都逐条
采纳或写理由驳回。把这一节单列出来，是想让 owner 看清楚"哪些是 codex 拍出来真改了、
哪些是 codex 误报被代码证据驳回、哪些是裁为既有隐患单独立项"，而不是笼统说"过了 review"。

**第一轮（"查用户名刀"）。** codex 指出 `find_username` 改成"按全局 user_id 查该 user
最近一条非空 username"后，`check_chat_history` 渲染一段历史会把老消息说话人显示成该 user
最新的名字（跨消息 / 跨渠道串名），且 `search_group_history` 的 assistant 行 username 列
本就为空、`username or '?'` 会全显 `'?'`。**已采纳并修订**：`check_chat_history` 改直读
本行 `msg.username`、`search_group_history` 按 role 派生说话人（assistant 显示「我」、
user 行读 username 列），monitor-dashboard 的 p2p 取名子查询加 `cm.username IS NOT NULL`
过滤。这些已落进变更主题三 / 主题四，是真改。

**第二轮（reply + 读取侧）。** 三条必改：

- **必改 1：入站链路顺序 `resolve → storeMessage → runRules` 与 5b 定稿要求的
  `resolve → runRules → 存库 → 发 MQ` 不符，且发 MQ 藏在 runRules 内部早于 storeMessage。**
  经 git 证据确认这是 5b commit `0229a5d` **既有**缺陷、非本批引入。**用户决策本 PR 内
  修复**——这就是 T5-5b 章节"5b 入站链路重排"那一整节做的事。
- **必改 2：proactive 合成行（`message_id=proactive_<ts>`、`user_id=__proactive__`）疑似
  5c 切全局后断链。** 经代码核查 **codex 误报、不成立**：proactive 行的 reply / root 指针
  取自库内已落好的 target 消息（5c 切全局后自动是全局 ULID），指针一致不断链；那条
  `proactive_<ts>` 只作回复链链尾出现、不会成断点；现有读取方显式靠 `PROACTIVE_USER_ID`
  哨兵 + `message_type` 识别兼容它。结论：proactive 合成行的 `message_id` / `user_id`
  **显式排除在全字段全局 ULID 契约外**，作为已知例外文档化（零代码改动），详见
  `docs/plan/multi-channel-T5c-readside-review.md` 的「契约已知例外：proactive 合成行」节。
- **必改 3：`_context_messages` 群上下文把 assistant 行渲染成"未知用户"。** **已 TDD 修**：
  `_context_messages.py` 新增 `_speaker_of(msg)` 按 role 派生（assistant 行返回「我」、
  user 行 `username or "未知用户"`），不再把赤尾历史发言渲染成占位词喂模型。

**第三轮（入站重排本身）。** 重排做完又过一轮，三条必改 + 两条建议：

- **必改 1：质疑非 @bot 群消息会被新顺序短路掉（不入库 / 不复读）。** 用代码证据 **驳回**：
  真实契约链对非 @bot 群消息返回 `ok:true / respond:false`、并不短路；`storeMessage` 在
  `handlers.ts` 是无条件执行的（不看 terminal kind），非 @bot 群消息照常入库，飞书复读
  零回归。为消除原 mock 盲区，新增了真实链路测试（`inbound-pipeline.real-lark.test.ts` /
  `handlers.inbound-order.test.ts` 钉死这条）。
- **必改 2：多 bot 同群时未抢锁的 bot 会留下永不完成的孤儿 pending 行。** 这是 **真回归、
  已修**：把 `savePending` 闭包随去重锁一起后移到 `handlers.ts` 抢到锁之后才调用，未抢锁
  的 bot 在 setNx 处就 return、不会 savePending。`handlers.multibot-pending.test.ts` 多 bot
  并发测试钉死。
- **必改 3：setNx 抢到锁后若 publish 失败，60s 内无补偿、消息丢失无重试。** 裁决为
  **既有隐患、非本次重排引入**：git 证据显示重排前后 setNx ↔ publish 的相对时序一致
  （重排前在 `makeTextReply` 内 setNx 紧接 publish，重排后在 `handlers.ts` 内 setNx 紧接
  publish），这个"锁非原子、无补偿"的窗口重排前就存在、本次没扩大。**单独记 backlog，
  不在本批扩大范围。**
- **建议 1（采纳）：per-handler 作用域捕获、去掉"循环结束用最新 pending 回填"。** 已采纳——
  `engine.ts` 每个命中 handler 在自己作用域内捕获本次专属 `scopedPending`，终态只绑产生
  它的那个 handler 本次注册的 pending，删掉了容易错绑的回填防御写法。
- **建议 2（采纳）：把 `storeMessage` 成功语义钉成"message_id 可回查"。** 已采纳——
  `handlers.store-semantic.test.ts` 钉死：`.orIgnore()` 的 ON CONFLICT 跳过是因为"行已
  存在"（message_id 仍可回查），故 fail-loud 后仍 publish 是安全的，不会把"行已存在"
  误判成"存库失败"。

一句话总结这三轮：codex 拍出的真问题（串名、_context_messages 占位词、孤儿 pending、
入站顺序）全改了；误报（proactive 断链、非 @bot 群消息短路）用代码证据驳回并补真链路
测试消除盲区；既有隐患（锁非原子无补偿）裁出来单独立 backlog、不在本批夹带扩大。

---

## 五、跨章节专题

### 专题 A：身份契约链路顺序为什么必须是 resolve → runRules → 存库 → 发 MQ

钉死的完整顺序是
`parse → AddressingPolicy.decide+enforce（前置总闸）→ IdentityResolver.resolve →
runRules → storeMessage → 发 MQ`，经 codex T1 评审定稿、不可调换，理由是强依赖链：

1. **`AddressingPolicy` 必须在 `resolve` 之前**。它是不依赖 DB 的纯判定。把它前置，意味着
   即使后面 resolve 依赖的 DB 出异常，这一步也已经先产出了"这条消息为什么不回"的可查结论。
   反过来如果先 resolve 再判定，DB 一挂，连"要不要回"都说不出来，fail-loud 的可查性就破了。
2. **`resolve` 必须在 `runRules` / 存库 / 发 MQ 之前**。黑名单等规则要靠全局 ID 才查得到；
   `storeMessage` 必须写全局 ID；发 MQ 的 ChatTrigger 必须带全局 ID。resolve 不先做，
   runRules 查不到黑名单、存库和发 MQ 会把裸 channel ID 漏到下游，fail-loud 也就无从成立。

一句话：先判定要不要回（不依赖 DB，DB 挂了也能解释原因），再换全局 ID（后续一切都依赖
它），再跑规则引擎、存库、发 MQ。前半段（parse → decide+enforce → resolve）在
`inbound-pipeline.ts` 的 `runInboundContractChain` 里以代码 + 注释钉死；后半段
（runRules → storeMessage → 发 MQ）在 `handlers.ts` 接线处钉死（`handlers.ts:201-276`）。
**注意**：5b commit `0229a5d` 当初接线把后半段写成了 `storeMessage → runRules`、且发 MQ
藏在 runRules 内部早于 storeMessage，与这里钉死的顺序不符——已在本 PR 内重排修复（见
T5-5b 章节"5b 入站链路重排"小节、第四节第三轮、全局未决项汇总"身份契约链路顺序"一条）。

### 专题 B：不可部署中间态（5b）与 5e 真机验证的关系

5b 把渠道抽象接进了真实链路，但**身份映射表（identity_user/chat/message）还没 apply、
飞书历史 ID 还没回填、7 张业务表和 Qdrant 还没原地重写**。这意味着 5b 阶段契约链里
`IdentityResolver.resolve` 没有真实映射表可查，契约链**必然失败**。所以 5b 不是一个可独立
部署、可独立 coe 验证的状态——它是一个明确的"代码就绪、数据未动"的中间态。spec 把这点
说死，并把验证口径明确切成两段：5b 阶段只做代码侧验证（fake/in-memory resolver 跑契约链
测试、单一终态出口测试、飞书逐场景行为回归用例），真正的端到端真机验证统一并入 **5e**——
也就是映射表已 apply、历史已回填、drain gate 走完之后那个状态，一次性做。换句话说，本 PR
里 T2/T3/T5-5b/T5-5c 所有"真机层面"的验收（飞书私聊/群聊/@bot/不@bot 零变化、非文字消息
存库、图片进识图、看图聊天、Qdrant 按新 ID 检索、快照恢复演练）都是悬而未决、全部欠在 5e。

### 专题 C：回滚 = 快照恢复（决策三），不是映射表反查

一个 owner 必须清楚的认知：迁移失败的回滚**不是**"用映射表把全局 ID 反查回飞书裸 ID"。
映射表只能把旧 ID 翻译成新 ID，它没法恢复已经被重写的主键、外键、索引这些结构，也没法
恢复 Qdrant 里已经改掉的向量。所以决策三定的是：迁移前对 DB 和 Qdrant 做**全量快照**，
出问题就用快照恢复。这是 restore，不是代码层面的 rollback。配套需要一个 drain gate（停写
窗口）：迁移前必须先关 webhook 入口、清空/妥善处置 RabbitMQ 和 outbox 队列、把一镜像产出
的多个服务（channel-server / recall-worker / chat-response-worker + agent-service /
vectorize-worker）同步切换之后，才恢复写入，否则没有 fallback 的情况下新旧契约会在队列
重放时互相污染。这套快照 + drain gate 流程是 5e 的内容，本 PR 未执行。

---

## 六、全局未决项汇总（按优先级，醒目）

### 已决 — reply_message_id 已全局化完成（原 P0，本 PR 内随入站重排一并修复）

原状：5b 入站接线 `storeMessage` 时 `reply_message_id` 写的还是飞书裸 `parentMessageId`，
没有 resolve 成全局 message_id，与全局 ULID 主键形状不一致。**本 PR 已修复**：
`inbound-pipeline.ts` 的 `runInboundContractChain` 新增 `globalReplyToId`
（`inbound-pipeline.ts:56-59`、`119-136`），把飞书"回复某条消息"锚点 `parentMessageId`
与 root 一样经 `IdentityResolver.resolve` 翻成全局 internal_message_id；无 parent 时
`undefined`（保持原"空就空"语义、不凭空造 id）；`handlers.ts` 的 `storeMessage` 改用
`reply_message_id: chain.globalReplyToId`（`handlers.ts:230`）。这件事原本标在 P0、待
owner 拍板"本次补否"——已随 5b 入站重排一并补掉，**不再是未决项**。

### P1 — /api/users 仍返回 lark_user.union_id AS user_id（spec gap）

`user_id` 全局化后，`/api/users` 接口里 `lark_user.union_id AS user_id` 的过滤会失效
（拿 union_id 当 user_id 对不上全局口径）。这跟"`lark_user` / `lark_group_member` 这两张
表本身全局化后怎么处置"是同一个 **spec 尚未定义的 gap**——spec 的调用方覆盖把这 7 张表
列为快照范围，但没定义这两张飞书身份表自身的全局化处置方案。本次范围外，列出来让 owner
知道它存在、需要 spec 层面补决策。

### P1 — 必改 3：去重锁非原子、publish 失败无补偿（既有隐患，单独 backlog，本批不扩大）

`handlers.ts` 抢到去重锁（`setNx`，60s TTL）后才 `savePending` + `publish`；若 publish
失败，60s 内同一 message_id 的其它 bot 因锁还在而静默跳过，这条消息既没发出去也没人补发，
等于丢。codex 第三轮拍出这点。**裁决为既有隐患、非本次重排引入**：git 证据显示重排前
（`makeTextReply` 内 setNx 紧接 publish）与重排后（`handlers.ts` 内 setNx 紧接 publish）
setNx ↔ publish 的相对时序一致，这个"锁非原子、无补偿"窗口重排前就在、本次没扩大。
**单独记 backlog（需 outbox / 锁释放补偿之类的设计），不在本 PR 夹带扩大范围。**

### P1 — 3 个 bot-var.test.ts 失败是基线既存

channel-server 套件本会话实跑 201 pass / 3 fail，3 个 fail 全是
`apps/channel-server/src/core/services/bot/bot-var.test.ts`（getBotAppId / getBotUnionId /
无 botName 抛错三个用例）。本会话已确认这是 pristine 基线就有的（属 T4 bot_config 改动
范围），根因是 `bun mock.module` 的进程级污染（mock 跨测试文件泄漏），**不是本批引入**，
已记为待办，本次不修。

### P1 — senderInfo.name 非空 + 映射表 DDL 未 apply + 整体一致性 + 出站 PG 故障路径，全部并入 5e 真机验证

四件事必须在 coe（5e）真机一次性验证：(1) `handlers.ts` 写入
`message.senderInfo?.name`，但"入站事件里 senderInfo.name 实际是否非空"靠读代码证明不了，
必须真机跑一遍看实际落库的 username；(2) 三套身份映射表的 `ALTER TABLE` / 建表 DDL 全部
未 apply，部署前必须先确认 DDL 就绪（T4 的 bot_config 迁移 SQL、T5 的映射表 SQL、5c 的
`conversation_messages.username` 列三处 DDL 都是产物、本 PR 一律未执行）；(3) 飞书逐场景
零变化的真机回归（私聊/群聊/@bot/不@bot、非文字消息存库、图片进识图、看图聊天）+ Qdrant
按新 ID 检索 + 快照恢复演练 + **本次入站重排后 agent-service `find_message_content` 不再
读空走"未找到记录"短路**，全部欠在 5e；(4) **出站 chat-response-worker 的 PG 故障路径
无集成测试**——fail-loud 死分支修复后，出站落库失败靠"worker 自身 try/catch 未改 + 飞书
消息在落库前已发出"的代码结构论证安全，但这条路径没有集成测试覆盖，coe 需专项验证。
**诚实披露：这是个靠代码结构论证、非测试证明的点，owner review 时要清楚它没有自动化兜底。**

### 已决 — proactive 合成行 id 不纳入全局 ULID 契约（codex 担忧经核查不成立，文档化为已知例外）

codex review 中提过一个担忧：`app/life/proactive.py` 往
`conversation_messages` 写的那条 proactive 合成行，`message_id` 是
`proactive_<ts>` 自造串、`user_id` 是 `__proactive__` 哨兵，跟"全字段
全局 internal ULID"契约不一致，疑似 5c 切全局后会断链。**经核查这个
担忧不成立——不是 bug**：proactive 行的 `reply_message_id` /
`root_message_id` 取自库内已落好的 target 消息（5c 切全局后自动是全局
ULID），指针一致、回复链不断；那条 `proactive_<ts>` 只会作为回复链链尾
出现、不会成为断点（飞书侧无此消息、没人能回复它）；现有读取方
（`get_unseen_messages` 用 `exclude_user_id=PROACTIVE_USER_ID`、
`get_recent_proactive_records` 用 `proactive_user_id=PROACTIVE_USER_ID`）
都显式靠 `PROACTIVE_USER_ID` 哨兵 + `message_type` 识别并兼容它。

**已决**：proactive 合成行的 `message_id` / `user_id`
**显式排除在全字段全局 ULID 契约之外**，作为已知例外，**零代码改动**；
reply / root 指针仍是全局 ULID（一致不断链）；唯一真实风险是认知层面
（新增读取方不知道这个哨兵可能误读），靠文档显式记下来兜住——详见
`docs/plan/multi-channel-T5c-readside-review.md` 的「契约已知例外：
proactive 合成行（已决策，零代码改动）」一节，那节已写明新增读取方
必须感知此例外。

### 已决 — 身份契约链路顺序：确认 5b 既有 → 本 PR 内重排修复完成

专题 A 钉死的链路顺序
`parse → AddressingPolicy.decide+enforce → IdentityResolver.resolve →
runRules → storeMessage → 发 MQ`：经 git 证据确认是 5b commit `0229a5d` **既有**的
顺序错误（原写成 `resolve → storeMessage → runRules`、发 MQ 还藏在 runRules 内部早于
storeMessage），用户决策**本 PR 内重排修复**，**已完成**——详见 T5-5b 章节"5b 入站
链路重排"小节与第四节"codex T3 三轮评审与整改"第三轮。真机端到端验证并入 5e。

### 诚实披露 — fail-loud 死分支删除改动未过 codex 第四轮（调用超时、零输出）

`memory.ts storeMessage` 删掉内部吞错 try/catch 这个改动，本想再过一轮 codex 第四轮
review，但 codex 调用**超时、零输出**，**这一点未被 codex 评审**。如实标注：前三轮已
充分覆盖入站链路与重排，这个删除是"删一段会吞掉真实 PG 故障的 catch、让故障自然上抛"，
属低风险删除（全覆盖核实 `storeMessage` 仅 2 个真实调用方、语义影响已在变更主题四论证），
但 owner 应清楚**它没有 codex 外部视角背书**，review 时请重点看这一处。

### P2 — chat-response-worker.ts 既存 TS 类型错误（5b 已提交代码，非本批引入）

本会话 `bunx tsc --noEmit` 在 `chat-response-worker.ts` 报 2 个类型错误（Buffer/
ArrayBufferView 不兼容、`{ image_key? }.data` 属性不存在），位于 5b 已提交代码、与本批
身份 / 重排改动无关。如实记录供 owner 知晓，属 5b 既存类型债，不影响本批读取侧改动。

---

## 七、本次 review 的边界声明

- **当前工作区状态**：T1 / T2 / T3 / T4 / T5-5a / T5-5b 这 6 个代码 commit + 6 个 docs
  commit 已提交并 push，构成 PR #228（人工 review 中）；T5-5c 读取侧 + 5b 入站链路重排 +
  `memory.ts` fail-loud 死分支修复这三批改动（生产文件 + 测试文件清单见第二节）全部在
  同一工作区未提交，没有 commit、没有 push。
- **没有 apply 任何 DDL**：T4 bot_config 迁移 SQL、T5 三套映射表建表 SQL、5c
  `conversation_messages.username` 列的 `ALTER TABLE`——全部是产物文件 / 实体声明，本 PR
  一律未执行。它们都是 coe-* 独立泳道的破坏性变更，由 owner 决策时执行。
- **没碰线上**：本次没有部署、没有绑 dev bot、没有连真实 DB / Qdrant。所有验证都是本地
  单测（agent-service `uv run pytest` 相关套件全绿、channel-server `bun test` 201 pass /
  3 fail——3 fail 是 T4 `bot-var.test.ts` 基线既存、本批零新增），真机一致性全部欠在 5e。
  5b 入站重排与 fail-loud 死分支修复均为不可部署中间态，无真机端到端验证（也做不了）。
- **没改 spec**：`docs/plan/multi-channel-support.md` 未改。已有的
  `docs/plan/multi-channel-T5c-readside-review.md` 本次**有更新**（补 5b 入站重排呼应、
  codex 三轮整改呼应、reply_message_id 已决、出站 PG 故障路径披露）。
