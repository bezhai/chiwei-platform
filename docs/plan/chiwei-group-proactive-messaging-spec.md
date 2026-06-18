# 赤尾在群里主动说话:群成为一等可投递对象 + life 知道自己在哪个群

## Problem

赤尾在飞书群里聊完后,如果 life 轮里还想主动继续讲,她会转头去**私聊**对方,而不是接着在那个群里说。根因是她主动发消息的唯一目标维度是「人」:`send_message(uid)` 把一个人解析成投递地址,而 `resolve_delivery` 只认 `persona:` / `user:` 两种 uid、SQL 写死只查 `scope='direct'` 私聊会话,根本没有「群」这种投递目标。再加上她被群消息唤醒时,信箱条目 `EventEnvelope` 只带 summary 文本、不带结构化的群 id/群名——她知道「刚和某人聊过」却不知道是哪个群、也拿不到群的句柄。两头一夹,她想在群里说也无群可发,只能落到私聊。

## Goal

把现有「身份与投递分层 + 模糊查候选」这套能力从「只有人」对称扩到「人 + 群」,让赤尾:
- life 轮里能知道「刚才那条动静来自哪个群」(群的 id + 名字进她的感知上下文);
- 能像查人一样模糊查群、拿到群的稳定句柄,自己决定这次主动发是私聊某人还是发到某个群;
- 在白名单群里聊完想继续讲时,主动发的消息真的出现在那个群里,而不是私聊。

## Non-goals

- 当面聊天机制本身(另立)、world 安排真人何时入场(另立)。
- 给「从没私聊过的真人」发起新私聊(这边没有 open_id,是已知能力 gap,不在本刀)。
- 非白名单群的感知与发送:她能发的群严格等于她能听见的群(白名单),白名单之外一律不可发。
- qq 等其他渠道的群。
- 用确定性规则触发她「该去群里说话」——发不发、在哪个群说、说什么,靠她的生活上下文涌现,本刀只提供「能发到群」这只手。

## Key design decisions

1. **群是一等可投递对象,uid = `group:<common_conversation_id>`**,对称 `persona:` / `user:`;群名取 `common_conversation.display_name`(线上已有,如「🐢🐢群(飞书版)」)。选它而不是「群聊主动发另做一套」,因为现有链路已经是「uid → 候选 → resolve → 投递」,群只是新增一种 uid 类型就能整条复用,她的心智也统一成「挑个 uid 发」,发人发群无差别。`GroupTarget` 必须像 `LarkP2PTarget` 一样**钉死发送身份**:按 `persona_id` + 群会话解析出该群里赤尾的 active `bot_name` 带进出站段——proactive 出站不写 `common_agent_response`,worker 没有别处可推断用哪个 bot,身份缺失会被 ack-drop 或用错 bot(codex T1 必改)。

2. **群发范围严格限 life 感知白名单(`life_feed_chat_whitelist`)**。选它而不是「她在的所有群 / 任意群」:对称「能听见才能说」,避免误发到线上一堆无关业务群(oncall/营销/团建);且发送范围跟感知范围一致最自洽。群的模糊查只在白名单内匹配,但**安全闸不能只落在查询侧**:`send_message(group:<id>)` 是模型直接调的,她可能从别处拿到、甚至编出一个非白名单群 id 绕过 look_up。所以真正的闸在 `resolve_delivery` 的 group 分支(投递最后一关)——硬校验「在当前白名单 + `scope='group'` + `channel='lark'` + 会话 active」,任一不满足 fail-loud,绝不投递(codex T1 必改)。

3. **让她「知道在哪个群」走结构化补传,而不是把群名埋进 summary 文本**。给 `EventEnvelope` 加 `chat_id` / `chat_scope` / `chat_name` 字段,回灌时带上;她 life 轮读到的不只是「认知上知道群名」,而是拿到群的稳定句柄、可直接 `send_message(group:<id>)`。文本方案要她「从 summary 认出群名 → 模糊查 → 拿 uid」绕一圈且没有 scope,结构化一步到位、对称 user 体系。代价是 durable schema 变更(见下)。字段语义钉死:`chat_id` = `common_conversation_id`;`chat_scope` 取 DB 原值 `direct` / `group`(传给 `build_inner_context` 时映射成它要的 `chat_type` p2p / group);stimulus 即使群名缺失也要兜底展示 `uid=group:<id>`,保证她任何时候都拿得到句柄(codex T1 建议)。

4. **群主动发 = 往群里发一条新消息,不 reply 群里某条/某人**。沿用 proactive 现有「无条件新发、root_id 留空」契约(出站 worker 已据 `is_proactive` 不反查来源消息)。她「想对群里谁说」靠 content 的自然语言表达(点名/@),不做结构化 reply 绑定。

5. **出站链路(channel-server chat-response-worker)不动**。它已按 `chat_id` 解析会话投递、不挑 p2p 还是 group。本刀全部改动收在 agent-service 一侧。

## Caller coverage

| 被改的函数 / 数据 | 现状 | 改后 | 调用方影响 |
|---|---|---|---|
| `resolve_delivery(uid)` | 只认 `persona:` / `user:`,只返回 `MailboxTarget` / `LarkP2PTarget` | 加 `group:` 分支 + 新 `GroupTarget`;分支内硬校验白名单/scope/channel/active 否则 fail-loud,并解析该群 active `bot_name` 钉进 target | 唯一调用方 `send_message`;旧两类 uid 行为不变 |
| `search_recipients(query)` | 只查人(bot_persona / common_user 的 display_name) | 加白名单群的 `display_name` 模糊匹配,候选混入 | 唯一调用方 `look_up_contact`;返回结构 `RecipientCandidate` 不变,多出群候选 |
| `send_message(uid, content)` | 处理 `MailboxTarget` / `LarkP2PTarget` 两分支 | 加 `GroupTarget` 分支(走群 proactive 渲染 + 出站) | life agent 工具循环;旧两分支不变 |
| `look_up_contact(query)` | 模糊查人 | 候选自然带出白名单群;docstring 点明也能查群 | life agent 工具循环 |
| `build_proactive_chat_context(...)` | 硬编 `chat_type="p2p"` | 接受会话 scope/群名,按 scope 传 `chat_type` | 调用方 `send_message`;p2p 路径行为不变 |
| `deliver_event(...)` | 无会话字段 | 加可选 `chat_id` / `chat_scope` / `chat_name`(默认 None) | 7 处调用方;加可选参数向后兼容,只群回灌处(`chat_node`)补传 |
| `EventEnvelope`(durable) | 7 字段 | 加 `chat_id` / `chat_scope` / `chat_name`(nullable) | 写端 `deliver_event`、读端 `list_unread_events` |
| `list_unread_events(...)` | 不返回会话字段 | 读出新字段随条目返回 | 调用方 `life_wake._run_life_round` |
| life_wake stimulus 拼装 | 群 external 只呈现 summary 文本 | 群消息标「来自群聊「群名」」、把群句柄摆给她 | — |

## Data & deployment impact

- **`EventEnvelope` 是 durable Data,加 3 个 nullable 列 = schema 变更,且是 forward-only**(参考 `reference_chiwei_data_schema_migrate_footgun`):加列对**上线**安全(migrator 自动补列、不触发 fail-closed),但破坏了**回滚**——一旦 DB 有了新列,回滚到旧镜像(旧 `EventEnvelope` 定义没这几列)会被 migrator 当成「字段被删」拒绝启动、pod crash loop。所以这是单向变更:回滚预案是「连列一起处理,或保留新列定义的过渡镜像」,不能简单回退旧 tag;coe 先验 migrate 能补列。另确认信箱条目不走 MQ 序列化(EventArrived 走 MQ 但本刀不动它,群身份从信箱读时补),否则旧 schema 消息反序列化会炸。
- 无新表;不新增 Dynamic Config(复用已有 `life_feed_chat_whitelist`)。
- **Langfuse `life_wake` prompt 可能要微调**:让她认知里「可以在群里主动说话、群和私聊是两种场合」。先发 coe-world-life2 label 验,再切 production。stimulus 里群消息的呈现是代码(life_wake.py)不是 prompt。
- 不跨服务:channel-server 不动。部署只 agent-service 单服务。
- coe 验证走 coe-world-life2 + dev bot 群聊(链路见 `reference_coe_lark_e2e_mq_isolation`:gateway 规则把 dev bot webhook 导到 coe channel-server)。需把验证用的群 chat_id 配进 coe 的 `life_feed_chat_whitelist`。

## Tasks

**Task 1 — 群成为可投递对象 + 能模糊查到。**
目标:赤尾能用 `group:<id>` 把消息发到一个白名单群,且 `look_up_contact` 能把白名单群作为候选列给她挑。
产出:`recipient_directory` 的群 uid 体系(`group_uid` / `GroupTarget` / `resolve_delivery` 的 group 分支 / 按白名单限定的群模糊查)+ `send_message` 的群投递分支。
验收:单测覆盖「`group:<id>` 解析成群目标并带正确 `bot_name`」「白名单外的 `group:<id>` fail-loud」「模糊查只返白名单群」;coe 真机里她对白名单群 `send_message` 后消息真的落到那个飞书群,且出站段 `is_p2p=false`、`bot_name` 正确。

**Task 2 — proactive 上下文支持群场景。**
目标:群主动发时,她的渲染上下文是「在群聊『X』里说话」而不是私聊。
产出:`build_proactive_chat_context` 解除 `chat_type="p2p"` 硬编、按会话 scope 与群名构建群场景上下文;`send_message` 群分支把 scope/群名传进去。
验收:trace 里群主动发那一轮的 inner_context 呈现的是群场景(`_scene_section` 群分支),不是 p2p 私聊。

**Task 3 — 让 life 知道「这条动静来自哪个群」。**
目标:她被白名单群的消息唤醒时,stimulus 里能看到来自哪个群、并拿到该群句柄,从而能接着在同一个群继续主动说。
产出:`EventEnvelope` 加会话身份字段(durable migrate)+ `deliver_event` 可选参数 + `chat_node` 群回灌处补传 chat_id/scope/群名 + `list_unread_events` 读出 + life_wake stimulus 把群消息标注群名并把群句柄摆给她。
验收:PG `event_envelope` 能查到群回灌条目带 chat_id/chat_scope/chat_name;coe 真机里她在白名单群聊完一轮后,life 轮主动发的下一条出现在**同一个群**而不是私聊。

**Task 4 — coe 端到端验证 + prompt 微调。**
目标:整链「白名单群里聊完 → 她主动在群里继续讲」在真机走通,不再转私聊。
产出:部 coe-world-life2 + dev bot 绑定 + 验证群配进白名单;按真机表现微调 `life_wake` prompt(coe label)。
验收:飞书群里真机观察到她主动发的消息出现在群里(对比改动前转私聊已纠正);并确认非白名单 `group:` 调用 fail-loud(可判定边界)。
