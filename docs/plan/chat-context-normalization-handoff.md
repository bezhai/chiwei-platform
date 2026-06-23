# 对话 context 规范化 · 工作交接

> 配套 spec:`docs/plan/chat-context-normalization-spec.md`。
> 本文是实现进度 + 待办的交接。

## 缘起

从"接入 QQ 等更多渠道"的讨论引出。发现两个**飞书现在就存在**的问题,决定先做这条平台无关的"规范化"基座、把飞书的洞堵上,QQ 接入后续另起 spec 复用同一基座:

1. 对话 context 里"飞书"是写死字符串,接 QQ 会穿帮。
2. 赤尾认主人没有 ID 层机制,只靠可改的飞书显示名 → 任何人把昵称改成"原智鸿"理论上就能被当成主人(prompt injection)。

## 核心设计结论(已定稿)

- **防伪造根**:`rel="owner"` **只来自 DB 里 `common_user.is_owner=true` 的记录、按 `common_user_id` 查**,是 prompt 里唯一的身份权威;显示名和正文自称都不可信。fail-closed——缺 id / 查不到 / `is_owner` 列未加 / load 失败 一律无标签,**绝不回退用显示名当身份**。
- **身份做成数据、不做成代码**:`common_user` 加 `is_owner` 布尔字段,代码里**不留任何真实 `common_user_id`**;一个人多个马甲(同 union_id per-app 分裂)只需给每条记录打标就全认得出,新增马甲只 UPDATE 一行、不改代码。
- **可信通道隔离 + 全字段转义**:历史每条渲染 `<msg from=.. rel=.. marker=.. time=..>正文</msg>`,`rel` 只装系统按 `is_owner` 算的值;所有用户来源字串 `html.escape`。
- **环境标识跟 channel 走**,未知 channel 中性降级,绝不默认回"飞书"。
- **只认主人**:这次砍掉三姐妹 / per-persona 视角,只堵"改名冒充主人"一个洞。

## 已完成

- 代码侧字段方案全部落地,严格 TDD 红绿,**74 个相关测试绿**:`identity_registry` 改成读 `is_owner` + 进程内缓存 + fail-closed;`models.py` 加 `is_owner` 列声明;四个调用方(被动群聊/私聊、主动、私聊 `peer_rel`)+ 环境标识都改到位。
- 代码里一个真实 `common_user_id` 都不留。

## 待办(按序)

1. **DB 两步(由 bezhai 手动改表,写线上,真值自己填)**:
   ```sql
   ALTER TABLE common_user ADD COLUMN is_owner BOOLEAN NOT NULL DEFAULT false;
   UPDATE common_user SET is_owner = true WHERE common_user_id IN ('<主人的几条 common_user_id>');
   ```
   列未加 / 未打标时代码 fail-closed = 没人被认成主人(等于现状,不报错),不破坏部署。
2. **langfuse `context_builder`** 去掉"sister=你的姐妹"说明、改只 owner,发到部署泳道对应的 label(不动 prod)。
3. **部署**:`agent-service` → `ppe-identity`(不设 `DATAFLOW_ENABLE_TIME_SOURCES`,world/life 被 lane gate 挡住不双跑);飞书 dev bot 真机验改名冒充(可能还需配 inbound 路由 + channel-server,见 e2e 规则)。
4. **ship prod**。

## 衍生债 / 后续

- **数据债(独立立项)**:主人的 `common_user` 按 union_id 分裂成多条,本方案靠"逐条打 is_owner"兜住;根治是**按 union_id 把多条 common_user 合并成一条**——直接关系将来 QQ 接入的跨渠道身份统一。注意 agent-service 端的 `common_user` 表**没有 union_id / open_id**(那些在 channel-server 私有映射表)。
- `common_user.channel` 字段全 null(1234 行)。
- **QQ 接入(另起 spec)**:复用本基座,加 `plugins/qq` + bezhai 的 QQ openid 锚定;QQ 官方 openid per-bot、主动推送 2025-04 已下线,约束在那份 spec 展开。

## 关键文件

- spec:`docs/plan/chat-context-normalization-spec.md`
- 认主人:`apps/agent-service/app/memory/identity_registry.py`(读 `is_owner` + 缓存)、`app/data/models.py`(`is_owner` 字段)
- 结构化署名:`app/chat/_context_messages.py`、`app/chat/proactive_context.py`
- 私聊对方身份:`app/memory/context.py`(`peer_rel`)
- 环境标识:`app/memory/context.py`(`_scene_section`)
- 透传链:`app/chat/context.py`、`app/nodes/chat_node.py`
- 测试:`tests/unit/memory/test_identity_registry.py`、`tests/unit/chat/test_context_messages_identity.py`、`tests/unit/chat/test_proactive_history_identity.py`、`tests/unit/memory/test_context.py`
