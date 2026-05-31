# B3: 出站收进 OutboundCapabilities 能力端口

把 channel-server 的**跨渠道出站链路**（chat-response-worker 发 AI 回复、recall-worker 撤回）从直接 import 飞书 SDK，改成走 `plugins/lark` 实现的 `OutboundCapabilities` 端口。飞书的富文本/图片/撤回翻译只在插件里出现一次。

## 背景（已验证的现状）

出站现状，按调用方分：

- **chat-response-worker**（`src/workers/chat-response-worker.ts`）：消费 chat_response 队列。当前流程：`reverseResolveForLark`（全局 id → 飞书裸 id）→ `resolveMentionsForGroup`（@用户名 → `<at>`，需飞书 chatId）→ `resolveImageReferences`（`@N.png` → 下载 TOS + `uploadImage` 上传飞书 → image_key，需全局 message_id 查 redis registry）→ `markdownToPostContent`（markdown → 飞书 PostContent）→ `replyPost`/`sendPost`（飞书 SDK 发送）。直接 import `@lark/basic/message`（replyPost/sendPost）和 `@integrations/lark-client`（uploadImage）。
- **recall-worker**（`src/workers/recall-worker.ts`）：消费 recall 队列。当前直接 import `@lark-client` 的 `deleteMessage`，逐条撤回 `agent_responses.replies[].message_id`（这些是飞书裸 message id）。

这两个 worker 在 `src/workers/` 下，不在 `core/**`，所以现状不违反 `boundary.test.ts`（它只扫 `core/**`）。B3 的目标不是"修边界违规"，而是**架构收口**：把飞书出站 SDK 调用从 worker 收进 `plugins/lark` 的能力端口，让出站交互只有一个飞书实现。

`plugins/lark/index.ts` 的 `capabilities.sendText/reply/recall` 当前是抛 `TODO(B3)` 的占位实现。

端口契约（`src/core/ports/channel-plugin.ts`，A 阶段已定，不改）：
```
sendText(conv: ConversationRef{channelId}, content: ContentItem[]): Promise<MessageRef{channelId}>
reply(thread: ThreadRef, content: ContentItem[]): Promise<MessageRef{channelId}>
recall?(msg: MessageRef{channelId}): Promise<void>
```
`ContentItem` 定义在 `src/core/channels/contracts.ts`：`text` / `image{key}` / `audio` / `file` / `sticker` / `unsupported`。

## Goals

**B3 收口边界（明确，避免验收口径自相矛盾）**：B3 只收口 **chat-response-worker 与 recall-worker 这两条 worker 出站链路**。lark 命令/卡片/图片/sticker 类 handler（meme/photo/balance/gen-history/repeat/delete/command-handler/utility-redirect/callback、daily-photo cron）保留原路、本次不参与，不在"零回归对齐"范围内（见 Non-goals）。"飞书出站 SDK 只一处"这句指的是**本次收口的这两条链路**所用的 send/reply/recall/upload 实现归并到 `plugins/lark` 的 OutboundCapabilities，**不是**指全仓所有飞书出站。

- chat-response-worker 与 recall-worker 不再直接 import 任何飞书 SDK（`@lark/basic/message`、`@lark-client`、`@larksuiteoapi`、`feishu-card`）。它们通过 `getChannelRegistry().get(channel).capabilities.{sendText,reply,recall}` 出站。
- `plugins/lark` 里实现 `OutboundCapabilities`：`sendText`、`reply`、`recall`，内部用飞书 SDK 真正发送/撤回。这两条 worker 链路的飞书富文本/图片渲染（markdown→post、@N.png 上传、@用户名 mention）全部在插件内做一次。
- core 侧出站只产出平台无关的 `ContentItem[]` + 渠道内 ref + 中性 `RenderContext`，绝不碰飞书 post 结构。`RenderContext` 是 B3 对端口的刻意演进（向后兼容的可选第三参），字段一律平台无关命名。
- `boundary.test.ts` 保持绿，BASELINE 不新增条目。
- 这两条 worker 链路的出站行为（回复 vs 新发、part_index 分段、proactive 回 root、图片/@用户名/markdown 渲染、撤回逐条）零回归。

## Non-goals

- **不动** lark 命令类 handler（meme / photo / balance / gen-history / repeat-message / delete-message / command-handler / utility-redirect）。它们发的是飞书 card / sticker / template / image，是飞书原生 UI，已在 `plugins/lark/` 内合法使用飞书 SDK；强塞进 `sendText/reply(ContentItem[])` 会丢结构或污染端口。它们不是"跨渠道出站"，是平台内指令交互。
- **不动** daily-photo cron（`infrastructure/crontab/services/daily-photo.ts`，发飞书 card，飞书专属定时任务，非跨渠道出站）。
- **不实现 C2**：IdentityResolver 已有 `resolve`/`toChannel`，B3 用现有能力。worker 仍自己做全局 id ↔ 飞书裸 id 反查（`reverseResolveForLark`），不把身份迁移逻辑提前到 B3。
- **不改** 入站链路、不改 `chat_response`/`recall` 队列 payload 协议、不改 DB schema。
- **不删** `@lark/basic/message`（sendPost/replyPost 等）模块本身——它仍被 plugin 内的能力实现和其他 lark 命令用。只把 worker 的直接调用切走。
- **不引入兼容层**：worker 切到能力端口后，旧的直接 SDK import 直接删，不留 re-export/别名。

## Key design decisions

### 决策一：worker 产出的 ContentItem[] 承载「待渲染的 AI markdown 文本」，飞书渲染全在插件内

AI 回复是一段 markdown 文本，内嵌 `@N.png` 图片引用、`@用户名` mention、markdown 图片语法。这段文本的「飞书化」——下载 TOS 图上传飞书拿 image_key、群成员名 → `<at user_id>`、markdown → 飞书 PostContent——**全是飞书专属**，必须在插件内。

所以 worker 侧产出的 `ContentItem[]` 就是 `[{kind:'text', text: <AI 原始 markdown>}]`：平台无关、不含任何飞书结构。插件的 `sendText`/`reply` 收到后，在内部跑完整飞书渲染管线再发。

考虑过的替代方案及否决理由：
- **worker 先把 markdown 拆成 `ContentItem[]`（text 段 + image 段）再传**：否决。`@N.png` 此刻还没上传飞书、没有 image_key，拆成 `{kind:'image', key:'N.png'}` 是假 key；而且 markdown→post 的分段逻辑本身是飞书 PostContent 的渲染细节，拆在 worker 侧等于把飞书渲染逻辑泄漏到 core 边界。保持「text 段承载原始 markdown，插件内渲染」最干净。
- **端口加一个 `sendRich(larkPost)` 方法**：否决。那是把飞书 PostContent 抬进端口，彻底违反「core 不碰飞书结构」。

### 决策二：图片 registry 查询 id 与 mention/发送 id 的双 id 问题，靠扩展能力端口的渲染上下文解决，而不是把全局 id 泄漏成 channelId

现状难点（worker 注释已点明、有回归测试钉死）：
- `@N.png` 图片 registry 的 redis key 是**全局 internal message_id**（agent-service 用全局 id 注册），查 registry 必须用全局 id。
- `@用户名` mention 解析、消息发送/回复，必须用**飞书裸 chatId / 裸 messageId**。

`ConversationRef{channelId}` / `MessageRef{channelId}` / `ThreadRef` 只承载渠道内裸 id。图片 registry 的全局 id 不是渠道内 id，不能塞进这些 ref。

决策：worker 在调能力端口前，自己用 `reverseResolveForLark` 把全局 id 反查成飞书裸 id，构造 `ConversationRef.channelId = 飞书裸 chatId` / `ThreadRef.selfChannelMessageId = 飞书裸 messageId`（这是端口契约本来的语义——「由 IdentityResolver 把 uuid 翻成裸 id 后传入」）。图片 registry 的全局 message_id 作为**渲染上下文**单独传给能力端口：`sendText`/`reply` 的 content 是 `ContentItem[]`，registry 全局 id 不进 content（它不是内容、是渲染所需的外部引用），而是作为可选的第三参数 `RenderContext { imageRegistryId?: string }` 传入。

考虑过的替代方案及否决理由：
- **把全局 message_id 塞进 `ConversationRef`**：否决。`channelId` 语义就是渠道内裸 id，塞全局 id 会让「飞书裸 om_* 查 registry 必 miss」的 bug 卷土重来（有回归测试 `chat-response-worker.image-registry.test.ts` 专门钉死这点）。
- **registry 解析仍留在 worker、解析完再传图片 key 给插件**：否决。registry 解析里的「下载 TOS → uploadImage 上传飞书」是飞书 SDK 调用（`uploadImage` from `@lark-client`），必须进插件，不能留 worker。
- **能力端口签名不动、registry id 走 context 全局变量**：否决。隐式全局状态不可测、违反「fail-loud 显式传参」。显式第三参数最清晰。

`RenderContext` 是飞书渲染的可选输入（image registry 引用），属于 OutboundCapabilities 端口契约的一部分——它不是飞书专属类型（只是 `{imageRegistryId?: string}` 字符串），core 可定义。非飞书 channel 不需要就不读。

### 决策三：sendText / reply 的「新发 vs 回复 vs proactive」分支留在 worker，端口只提供原子能力

worker 现状有 part_index/proactive 的分支逻辑（part 0 非 proactive → replyPost 触发消息；proactive 有 root → replyPost root，无 root → sendPost；part>0 → sleep + sendPost）。这是**出站策略**，平台无关（任何 channel 都有「回复某条 vs 新发」的选择），留在 worker，通过选择调 `reply(thread)` 还是 `sendText(conv)` 表达。端口只提供 `sendText`/`reply` 两个原子能力，不感知 part_index/proactive。

这与 `contracts.ts` 已有的 `deliver()`/`ReplyTarget` 取向一致（退化逻辑在中心、adapter 只做原子操作）。

### 决策四：recall 走 `capabilities.recall(MessageRef)`，逐条撤回的循环留在 worker

recall-worker 现状逐条 `deleteMessage(reply.message_id)`（飞书裸 id）。改为逐条 `capabilities.recall({channelId: reply.message_id})`。撤回循环、计数、状态更新（recalled/recall_failed）是 worker 的业务编排，留在 worker；`recall` 端口只做「撤回这一条」的原子飞书 SDK 调用。

注意：`agent_responses.replies[].message_id` 现状存的是飞书裸 message id（worker 注释明确 5c 才全局化），所以 recall 直接拿裸 id 构造 `MessageRef{channelId: 裸id}`，无需反查。channel 固定 'lark'（这两个 worker 当前只服务飞书链路；T6 接 QQ 时 payload 会带 channel，届时再参数化——B3 不提前做）。

## Caller coverage

grep `@lark/basic/message` + `@lark-client` + `reverseResolveForLark` 的全部 worker/core-edge 调用方：

| 调用方 | 现状 | B3 改动 |
|---|---|---|
| `src/workers/chat-response-worker.ts` | import `replyPost,sendPost`(@lark/basic/message) + `uploadImage`(@lark-client)；inline 做 reverseResolve→mention→image→markdown→send | 改走 `getChannelRegistry().get('lark').capabilities.sendText/reply`，传 `ContentItem[]`(AI markdown 文本) + `RenderContext{imageRegistryId, larkChatId}`。删除 worker 内 `resolveImageReferences`/`resolveMentionsForGroup`/`markdownToPostContent`/`replyPost`/`sendPost`/`uploadImage` 调用与对应 import。reverseResolve 反查保留（决策二）。新发消息 id 仍由能力端口返回（`MessageRef.channelId`），worker 拿来正查全局 id + 落库（这段身份/落库逻辑不变）。 |
| `src/workers/recall-worker.ts` | import `deleteMessage`(@lark-client)；逐条 deleteMessage(裸id) | 改走 `getChannelRegistry().get('lark').capabilities.recall({channelId: reply.message_id})`。删除 `@lark-client` import。循环/计数/状态更新不变。 |
| `src/plugins/lark/index.ts` | `capabilities.{sendText,reply,recall}` 是 `throw TODO(B3)` 占位 | 替换为真实实现（决策一/二/三/四）。飞书渲染管线（image registry resolve+upload、mention resolve、markdown→post、send/reply/delete）在此实现一次。 |

**不在 B3 改动范围**（已确认是平台内指令 / 飞书定时任务，非跨渠道出站，且已合法在 plugins 内或 infra 内用飞书 SDK）：`balance.ts`、`command-handler.ts`、`delete-message.ts`、`gen-history.ts`、`repeat-message.ts`、`commands.ts`、`utility-redirect.ts`、`services/meme/meme.ts`、`services/photo/*`、`services/callback/*`、`crontab/services/daily-photo.ts`。

**worker 内被搬走的飞书渲染辅助**：`resolveImageReferences`（worker 内私有函数，含 uploadImage 飞书调用）整段搬进插件；`resolveMentionsForGroup`（`core/services/message/resolve-mentions.ts`）和 `markdownToPostContent`/`createPostContentFromText`（`core/services/message/post-content-processor.ts`）现状在 core——它们读 DB/产飞书 PostNode，是飞书渲染细节，B3 改由插件 import 使用（core 不再被 worker 经由它们牵连）。**注意**：`post-content-processor.ts` 产出 `PostContent`（飞书 PostNode 结构）但 import 的是本地 `types/*` 而非飞书 SDK，故它在 core 不触发 boundary 违规；B3 把它的调用方从 worker 切到插件即可，是否把这两个文件物理移进 plugins 由实现阶段判断（移动文件无行为变化，但要保证 boundary 绿且无 core 死引用）。

## Data & deployment impact

- 无 DB schema 变更、无队列 payload 协议变更、无 migration。
- 一镜像多服务：channel-server 镜像产出 chat-response-worker / recall-worker / channel-server 三个 Deployment。B3 改了前两个 worker 的入口代码 + plugin 实现（plugin 被三者共享 import）。部署需三者同步 release（部署铁律：channel-server 部署后同步 release recall-worker + chat-response-worker）。
- 部署 = 杀 Pod = 中断正在跑的出站任务。验证泳道部署前确认无在途回复/撤回。
- coe/ppe 验证：飞书 dev bot 发消息走完整出站链路（含图片/@用户名/markdown/撤回）确认零回归。

## Task list

### Task 1：plugins/lark 实现 OutboundCapabilities（sendText / reply / recall）

- **目标**：把飞书出站渲染+发送+撤回收进 `plugins/lark` 的 `OutboundCapabilities` 真实实现，替换 `index.ts` 的 TODO 占位。飞书 SDK 调用（uploadImage / send / reply / deleteMessage）在此出现，且整仓只此一处出站实现。
- **产出**：`sendText(conv, content, ctx?)` / `reply(thread, content, ctx?)` 内部完成：image registry 解析（用 `ctx.imageRegistryId` 查 redis + 下载 TOS + uploadImage 上传飞书）、@用户名 mention 解析（用 `ctx.larkChatId` 或 conv.channelId）、markdown→PostContent、飞书 send/reply，返回 `MessageRef{channelId: 新消息飞书 id}`；`recall(msg)` 调飞书 deleteMessage。`RenderContext` 类型在端口侧定义。
- **验收**：单测覆盖——给定一段含 `@N.png`/`@用户名`/markdown 的文本 + mock 飞书 transport + mock registry，`sendText`/`reply` 产出的飞书调用参数与现状 worker 逐字一致（PostContent 结构、reply vs send 目标、image_key 替换）；`recall` 调 deleteMessage 传入裸 id。新写测试先 red 后 green。

### Task 2：chat-response-worker 切到能力端口

- **目标**：worker 不再 import 任何飞书 SDK，改走 `getChannelRegistry().get('lark').capabilities.sendText/reply` 出站。
- **产出**：worker 保留 reverseResolve（全局→裸 id）+ 身份正查/落库逻辑；出站段改为构造 `ContentItem[]`(AI markdown) + `RenderContext` 调能力端口，按 part_index/proactive 选择 `reply` 还是 `sendText`；删除 worker 内 `resolveImageReferences`/`resolveMentionsForGroup`/`markdownToPostContent`/`replyPost`/`sendPost`/`uploadImage` 的调用与 import。
- **验收**：`grep -n "@lark/basic/message\|@lark-client\|@larksuiteoapi\|uploadImage\|sendPost\|replyPost" src/workers/chat-response-worker.ts` 为空（除注释）；现有 `chat-response-worker.*.test.ts`（field-mapping / image-registry）仍绿（reverseResolve + registry id 契约不变）；新增/改写测试钉死 worker 现在调能力端口、传对的 content + ctx。

### Task 3：recall-worker 切到能力端口

- **目标**：recall-worker 不再 import 飞书 SDK，逐条撤回改走 `capabilities.recall`。
- **产出**：删 `@lark-client` import 与 `deleteMessage` 直接调用，改 `getChannelRegistry().get('lark').capabilities.recall({channelId: reply.message_id})`；循环/计数/recalled-recall_failed 状态机不变。
- **验收**：`grep -n "@lark-client\|deleteMessage" src/workers/recall-worker.ts` 仅剩注释；新增测试钉死「逐条调 recall、撤回成功计 recalled、全失败计 recall_failed」。

### Task 4：边界 + 全量回归验证

- **目标**：确认 core 边界守门绿、飞书出站 SDK 只在 plugins/lark、类型检查通过、无新增测试失败。
- **产出**：跑 `bun test src/core/boundary.test.ts`（绿，BASELINE 未增）；`grep -rn "@lark/basic/message\|@lark-client\|@larksuiteoapi\|feishu-card" src/core src/workers` 证明 worker + core 无飞书出站 SDK（plugins/lark 才有）；`tsc --noEmit`（或 package.json 对应命令）无错；`bun test` 全量失败集与改动前基线（3 个 pre-existing bot-var fail）一致、无新增。
- **验收**：上述命令实际输出贴进完成报告。
