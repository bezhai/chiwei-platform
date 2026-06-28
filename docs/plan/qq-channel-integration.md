# QQ 渠道接入：独立网关 + custom 协议对接 channel-server

## Problem

赤尾目前只活在飞书。要让她也能在 QQ 上对话。但 QQ 官方机器人在 2025-04 下线了主动推送，只能被动回复（用户发消息后 60 分钟窗口内、每条最多回 4 次）；而连 QQ 的底层很重——webhook 收包、Ed25519 验签、accessToken 刷新、图片/silk 语音编码、文件分片上传。腾讯官方的 `@tencent-connect/openclaw-qqbot` 已经把这些做得很成熟，但它的消息处理深度绑死 openclaw 这个 agent 框架，不能直接拿来跑赤尾。

channel-server 这边已经是「平台无关 core + 进程内插件」架构，`plugins/lark` 是现成样板，加新渠道只需新建插件 + 自注册，core 零改动。

## Goal

- bezhai 在 QQ 私聊赤尾、在 QQ 群里 @赤尾，她经 agent-service 正常回复（被动窗口内）。
- QQ 消息接入 chiwei 统一身份体系：common_user / common_conversation / common_message（UUIDv7），bezhai 的 QQ openid 被识别为 owner（`is_owner`），与飞书一致地认得出主人。
- 入站图片走赤尾现有的识图链路（共享媒体轨），她在 QQ 上也能看图聊天。
- 连 QQ 的所有平台细节隔离在一个独立网关进程里，channel-server 只认平台无关的 custom 协议、零 QQ 依赖。

## Non-goals

- 赤尾的一切主动通道（world/life 自主消息、定时提醒、afterthought、超出被动窗口的迟到回复）在 QQ 上一律不可达——官方机器人发不出。指向 QQ 会话/用户的主动消息直接丢弃并记日志，不改投其他渠道（跨渠道身份未打通，无从改投）。
- QQ 频道（guild / 子频道）：只做 C2C 单聊 + QQ 群 @消息。
- 出站富媒体（赤尾主动发图片/语音到 QQ）：本期只做出站文本；入站图片识图照做。
- 群聊 owner 识别：本期 owner 只在 C2C 私聊生效。群里成员是 member_openid（与私聊 user_openid 不同 ID 空间、且跨群变化），不做主人识别，赤尾在群里把 bezhai 当普通成员。
- 跨渠道身份合并：QQ openid 不与飞书 common_user 打通，bezhai 的 QQ 身份是独立的 common_user。

## Key design decisions

1. **独立 QQ 网关进程（基于 openclaw-qqbot 改造）over 把底层协议移植进 plugins/qq**：复用 openclaw-qqbot 成熟的连 QQ 实现（transport/验签/token/api/媒体/silk），只抽出它的纯协议部分、丢掉 openclaw 框架调用、自己写一个薄的「归一化→外发」入口和「收 custom→调 QQ api」出口。避免在 channel-server 里重写上千行 QQ api 和语音编码。代价是多一个进程要部署。

2. **custom 归一化协议（CustomInboundMessage / CustomOutboundMessage）做网关↔channel-server 的契约 over 透传 QQ 原始事件包**：网关把 QQ 事件归一化成平台无关的 custom 消息，channel-server 的 plugins/qq 只认这套协议。channel-server 保持零 QQ SDK 依赖，符合平台无关 core 的边界铁律；将来底层换实现（甚至换第三方协议端）对 channel-server 透明。

3. **网关作为新 app 进 chiwei monorepo、走 PaaS 部署 over 仓库外独立运行 openclaw-qqbot**：统一构建/部署/可观测，能用集群出口 IP 加 QQ 白名单、webhook 回调经 api-gateway 暴露，与 chiwei 其余服务一致。

4. **plugins/qq 照 plugins/lark 样板实现并自注册 over 改 core**：实现 InboundAdapter（custom→InboundMessage）、AddressingPolicy（私聊总响应 / 群看是否 @bot）、OutboundCapabilities（产 CustomOutboundMessage 发回网关）、ChannelRuntime（注册收 custom 协议的 HTTP ingress），import 即自注册进 channel/command registry。加渠道零改 core。

5. **被动窗口契约由网关独占、custom 协议必须可实现**：网关是被动窗口的唯一权威。出站 CustomOutboundMessage 必须携带它所回应的入站 msg_id（从入站一路带到出站），网关据此对同一 msg_id 维护 msg_seq 递增、60min 窗口与 4 次上限；赤尾的多段回复复用同一 msg_id、递增 seq，合计超上限的段就近合并或丢弃记录；窗口过期/超次一律丢弃记录，绝不 fallback 主动消息（会被 QQ 拒）。custom 协议出站带幂等键（基于全局 message id + 段序），网关与 channel-server 出站记录共同保证 MQ 重投不重复发。网关↔channel-server 双向 HTTP 走内网鉴权。owner 复用已上线的 `is_owner`——bezhai 的私聊 openid 经 ops-db 数据打标、代码不留真实 id，本期仅私聊（群聊不做，见 Non-goals）。

6. **入站副作用严格后置于身份解析**：识图、presence、规则副作用一律在 addressing → identity → lane 决策完成之后才执行；任何最前置的 raw 审计不得参与身份解析 / 规则 / agent-service 投递。直面 PR #228 副作用前移翻车的老坑。

## Caller coverage

- **plugins/index.ts**：加一行 `import './qq'` 触发自注册。现有 lark 不受影响。
- **chat-response-worker**：已按 `payload.channel` 取对应插件的 OutboundCapabilities（平台无关，默认 lark）。新增 qq capabilities 后无需改 worker 代码；需保证入站链路给 QQ 消息正确写入 `channel='qq'`，使回复 payload 带上正确 channel。
- **入站 contract chain（addressing / identity / lane / store / MQ）**：QQ 走与飞书同一套；plugins/qq 产出标准 InboundMessage 后复用，无需改链路。
- **bot_config 数据**：新增一行 `channel='qq'` 的 bot；QQ 的 appId/secret 等连接凭据归网关侧（ConfigBundle/envs），channel-server 侧该行只承载 bot 标识 / persona / common_user_id / 网关回调地址。现有 lark bot 行不动。
- **agent-service**：QQ 消息只见 common_* 全局 id，不碰裸 QQ id。owner 经 `is_owner` 识别（已上 prod），只需锚定 bezhai 的 QQ 私聊 common_user。环境标识跟 channel 走（chat_context_normalization 已上线），需确认 context_builder 对 `qq` channel 有对应的环境文案、未知 channel 不串台。
- **赤尾主动消息发送方**（proactive messaging、life wake、notebook 定时提醒等）：这些通道产生的指向 QQ 会话/用户的消息没有可用入站 msg_id（属主动发），在 QQ 上必被丢弃 + 记日志、不得调网关发送（会被 QQ 拒）。需确认这些发送路径遇 `channel='qq'` 的处理。

## Data & deployment impact

- **新表（ops-db submit）**：QQ 私有映射表，照 lark 私有映射表的形态——QQ 用户 openid ↔ common_user、QQ 消息 id ↔ common_message、QQ 群 id ↔ common_conversation。具体表名/字段实现时定。
- **owner 锚定（ops-db mutation）**：bezhai 先私聊 QQ 测试 bot 拿到他的私聊 openid，再把对应 common_user 打 `is_owner=true`。一次性，只他一人，仅私聊 scope。
- **网关被动窗口状态持久化**：msg_seq、4 次计数、已投递幂等记录需持久化（Redis 或 DB，实现时定），使网关可重启、并容忍 MQ 重投——不重复发、不丢段、不误判超限。
- **新 app（PaaS 构建 + 部署）**：QQ 网关进程。需配 QQ appId/secret（ConfigBundle 或 Release envs）；集群出口 IP 加进 QQ 开放平台 IP 白名单；webhook 回调地址经 api-gateway 动态规则对外暴露（`/ops gateway upsert` + `explain` 预览）。
- **channel-server 改动**：一镜像多服务，部署 channel-server 后必须同步 release recall-worker 和 chat-response-worker。
- **无 Langfuse prompt 变更**：复用现有 chat prompt。
- **部署中断**：部署会杀在跑的异步任务，部署前确认无 rebuild / afterthought 在跑。

## Tasks

### T1 — QQ 网关进程（被动文本闭环）
**Goal**：基于 openclaw-qqbot 抽出纯连 QQ 协议层（剥掉 openclaw 的 AI/session/config），做一个被动收发文本的网关——webhook 收包 + Ed25519 验签 + token 刷新；入站把 QQ 事件（含附件 url 原样透传）归一化成 CustomInboundMessage 经内网 HTTP 推给 channel-server；出站收 CustomOutboundMessage、按其携带的 msg_id 在被动窗口内调 QQ api 发文本，独占管理 msg_seq / 4 次 / 60min 窗口与幂等。出站富媒体属 non-goal，运维（IP 白名单 / 网关暴露）归 T5。
**Deliverable**：一个新的 monorepo app，含 webhook 收发、token、custom 协议双向 HTTP 端点、被动窗口状态的持久化存储。
**Verification**：webhook 握手通过；真实 QQ 私聊/群消息能 POST 出结构正确的 CustomInboundMessage；给定带 msg_id 的 CustomOutboundMessage 能在窗口内发到 QQ 并在客户端看到；网关重启后窗口/幂等状态不丢、重复投递不重发。

### T2 — plugins/qq 入站
**Goal**：在 channel-server 新增 plugins/qq，实现一个接收 CustomInboundMessage 的 HTTP ingress（经 ChannelRuntime 注册）、InboundAdapter（custom→InboundMessage，含 attachments 接入识图链路）、AddressingPolicy（私聊总响应 / 群聊看 @bot），import 即自注册并走 contract chain。
**Deliverable**：plugins/qq 入站相关文件 + plugins/index.ts 的一行 import。
**Verification**：parse / addressing 单测红→绿；喂一条 CustomInboundMessage 能走完 contract chain 产出全局 id 并把请求投给 agent-service；识图 / presence 等副作用确实在 identity / lane 决策之后才触发；带图片的入站消息触发识图。

### T3 — QQ 身份与 owner
**Goal**：实现 QQ openid → common_user/conversation/message（UUIDv7）的 projector 与私有映射表（私聊 user_openid、群聊 member_openid 各自稳定归一、互不混淆），并把 bezhai 的私聊 openid 锚定为 owner。
**Deliverable**：QQ common projector + 映射表 DDL（ops-db submit）+ 私聊 owner 锚定步骤。
**Verification**：同一 QQ 用户在同一 scope 多次发消息产生稳定一致的 common_* 映射；bezhai 私聊时 agent-service 经 is_owner 识别为主人；群聊不做 owner、不误判；非主人不被误判。

### T4 — plugins/qq 出站
**Goal**：实现 QQ 的 OutboundCapabilities——反查全局 id → QQ 会话/消息 ref，产出携带原始入站 msg_id + 幂等键的 CustomOutboundMessage 发给网关，记录出站映射；多段回复在同一 msg_id 下交给网关续段（不另起新发）；对没有可用 msg_id 的主动发场景直接丢弃记录。
**Deliverable**：plugins/qq 出站能力实现。
**Verification**：chat-response-worker 对 `channel='qq'` 的回复经网关在被动窗口内送达；多段回复不超 4 次/窗口、不变成主动发；超窗或主动发场景记录日志、不抛错、不误发到别处。

### T5 — coe 端到端验证与上线
**Goal**：完成上线运维前置（集群出口 IP 加 QQ 白名单、webhook 回调经 api-gateway 暴露并 explain 预览），在 coe 泳道部署网关 + channel-server 三件套，绑 QQ 测试 bot，跑真机端到端。
**Deliverable**：运维前置就绪 + coe 验证证据 + 上线前检查清单（调用方全覆盖、数据读写一致、副作用、部署影响）。
**Verification**：真实 QQ 私聊和群 @赤尾都能正常对话；入站图片识图正确；私聊 owner 识别正确；被动窗口/频次表现符合预期。
