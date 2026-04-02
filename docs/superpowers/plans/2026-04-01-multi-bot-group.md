# Multi-Bot Group 分工体系 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 支持同一群内多个 bot 按角色分工（persona 只聊天，utility 只做工具），通过 gray_config 灰度控制，同时修复 storeMessage 原子性防止重复向量化。

**Architecture:** 给 bot_config 表加 `bot_role` 字段区分角色；规则引擎按 `category` 标记每条规则，运行时根据灰度开关 + bot 角色过滤；storeMessage 改为 INSERT ON CONFLICT DO NOTHING，仅插入成功时推向量化任务。

**Tech Stack:** TypeScript, TypeORM 0.3.x, PostgreSQL

---

## File Structure

| Action | File | Responsibility |
|--------|------|---------------|
| Modify | `apps/lark-server/src/infrastructure/dal/entities/bot-config.ts` | 加 `bot_role` 字段 |
| Modify | `apps/lark-server/src/core/rules/rule.ts` | 加 `RuleCategory` 类型和 `MultiBotGuard` 规则 |
| Modify | `apps/lark-server/src/core/rules/engine.ts` | RuleConfig 加 `category`，runRules 过滤 |
| Modify | `apps/lark-server/src/infrastructure/integrations/memory.ts` | 原子插入 + 条件推向量化 |

---

### Task 1: bot_config 加 bot_role 字段

**Files:**
- Modify: `apps/lark-server/src/infrastructure/dal/entities/bot-config.ts:4-40`

- [ ] **Step 1: 加数据库列**

在 `bot-config.ts` 的 `is_dev` 字段后面加：

```typescript
    @Column({ type: 'varchar', length: 20, default: 'persona' })
    bot_role!: 'persona' | 'utility'; // persona=拟人聊天, utility=工具功能
```

- [ ] **Step 2: 通过 ops-db 给已有数据加默认值**

线上 bot_config 表现有记录需要加列。TypeORM synchronize 会自动加列（default 'persona'），无需手动迁移。上线后手动把工具 bot 改为 utility：

```sql
-- 上线后执行，将工具 bot 角色设为 utility
UPDATE bot_config SET bot_role = 'utility' WHERE bot_name = '<utility-bot-name>';
```

- [ ] **Step 3: Commit**

```bash
git add apps/lark-server/src/infrastructure/dal/entities/bot-config.ts
git commit -m "feat(bot): add bot_role field to bot_config (persona/utility)"
```

---

### Task 2: 规则引擎支持 category 过滤

**Files:**
- Modify: `apps/lark-server/src/core/rules/rule.ts`
- Modify: `apps/lark-server/src/core/rules/engine.ts`

- [ ] **Step 1: 在 rule.ts 添加 RuleCategory 类型**

在 `rule.ts` 的 `RuleConfig` interface 定义之前加：

```typescript
/** 规则分类：utility=工具功能, persona=拟人聊天 */
export type RuleCategory = 'utility' | 'persona';
```

在 `RuleConfig` interface 中加 `category` 字段：

```typescript
export interface RuleConfig {
    rules: Rule[];
    async_rules?: AsyncRule[];
    handler: Handler;
    fallthrough?: boolean;
    comment?: string;
    category?: RuleCategory; // 未标记的规则所有角色都执行
}
```

- [ ] **Step 2: 在 engine.ts 给每条规则标记 category**

修改 `chatRules` 数组，给每条规则加上 `category`：

```typescript
const chatRules: RuleConfig[] = [
    {
        rules: [NeedNotRobotMention, OnlyGroup, WhiteGroupCheck((chatInfo) => chatInfo.permission_config?.open_repeat_message ?? false)],
        handler: repeatMessage,
        fallthrough: true,
        comment: '复读功能',
        category: 'utility',
    },
    {
        rules: [EqualText('余额'), TextMessageLimit, NeedRobotMention, IsAdmin],
        handler: sendBalance,
        comment: '发送余额信息',
        category: 'utility',
    },
    {
        rules: [EqualText('帮助'), TextMessageLimit, NeedRobotMention],
        handler: async (message) => {
            replyTemplate(message.messageId, 'ctp_AAYrltZoypBP', undefined);
        },
        comment: '给用户发送帮助信息',
        category: 'utility',
    },
    {
        rules: [EqualText('撤回'), TextMessageLimit, NeedRobotMention],
        handler: deleteBotMessage,
        comment: '撤回消息',
        category: 'utility',
    },
    {
        rules: [EqualText('水群', '水群趋势'), TextMessageLimit, NeedRobotMention],
        handler: genHistoryCard,
        comment: '生成水群历史卡片',
        category: 'utility',
    },
    {
        rules: [EqualText('开启复读'), TextMessageLimit, NeedRobotMention, OnlyGroup],
        handler: changeRepeatStatus(true),
        category: 'utility',
    },
    {
        rules: [EqualText('关闭复读'), TextMessageLimit, NeedRobotMention, OnlyGroup],
        handler: changeRepeatStatus(false),
        category: 'utility',
    },
    {
        rules: [CommandRule, TextMessageLimit, NeedRobotMention],
        handler: CommandHandler,
        comment: '指令处理',
        category: 'utility',
    },
    {
        rules: [RegexpMatch('^发图'), TextMessageLimit, NeedRobotMention],
        handler: sendPhoto,
        comment: '发送图片',
        category: 'utility',
    },
    {
        rules: [NeedRobotMention],
        async_rules: [checkMeme],
        handler: genMeme,
        comment: 'Meme',
        category: 'persona',
    },
    {
        rules: [NeedRobotMention],
        handler: makeTextReply,
        comment: '聊天',
        category: 'persona',
    },
];
```

- [ ] **Step 3: 在 runRules 中加 category 过滤逻辑**

修改 `engine.ts` 顶部 import，加入 bot 相关依赖：

```typescript
import { context } from '@middleware/context';
import { multiBotManager } from '@core/services/bot/multi-bot-manager';
```

修改 `runRules` 函数，在 for 循环内、规则检查之前加入过滤：

```typescript
export async function runRules(message: Message) {
    // 黑名单检查：被拉黑的用户直接忽略
    if (!(await NotBlocked(message))) {
        console.info(`Blocked user ${message.sender} message ignored`);
        return;
    }

    // 多 bot 分工：灰度开启时，按 bot 角色过滤规则
    const multiBotEnabled = message.basicChatInfo?.gray_config?.multi_bot === 'enabled';
    const botRole = multiBotEnabled
        ? multiBotManager.getBotConfig(context.getBotName() || '')?.bot_role
        : undefined;

    for (const { rules, handler, fallthrough, async_rules, category } of chatRules) {
        // 灰度开启且规则有分类时，只执行匹配角色的规则
        if (botRole && category && category !== botRole) {
            continue;
        }

        // 检查同步规则
        const syncRulesPass = rules.every((rule) => rule(message));

        // 检查异步规则
        const asyncRulesPass = async_rules
            ? (await Promise.all(async_rules.map((rule) => rule(message)))).every(
                  (result) => result,
              )
            : true;

        // 如果所有规则（同步和异步）都通过
        if (syncRulesPass && asyncRulesPass) {
            try {
                await handler(message);
            } catch (e) {
                console.error('rule engine error:', {
                    message: e instanceof Error ? e.message : 'Unknown error',
                    stack: e instanceof Error ? e.stack : undefined,
                });
            }

            if (!fallthrough) break;
        }
    }
}
```

- [ ] **Step 4: Commit**

```bash
git add apps/lark-server/src/core/rules/rule.ts apps/lark-server/src/core/rules/engine.ts
git commit -m "feat(rules): add category-based filtering for multi-bot role separation"
```

---

### Task 3: storeMessage 原子插入防重复向量化

**Files:**
- Modify: `apps/lark-server/src/infrastructure/integrations/memory.ts`

- [ ] **Step 1: 改为 INSERT ON CONFLICT DO NOTHING**

将 `storeMessage` 函数改为使用 QueryBuilder 的 `orIgnore()`（TypeORM 0.3.x 支持），并根据插入结果决定是否推向量化：

```typescript
import { ChatMessage } from 'types/chat';
import { ConversationMessage } from '@entities/conversation-message';
import { context } from '@middleware/context';
import { rabbitmqClient, VECTORIZE } from '@integrations/rabbitmq';
import AppDataSource from 'ormconfig';

/**
 * 判断消息内容是否为空
 */
function isEmptyContent(content: string | undefined | null): boolean {
    return !content || content.trim() === '';
}

/**
 * 存储消息到 PostgreSQL 并推送向量化任务到 RabbitMQ
 *
 * 使用 INSERT ... ON CONFLICT DO NOTHING 实现原子去重：
 * - 多 bot 同群时，同一 message_id 只有第一个到达的 bot 能成功插入
 * - 仅插入成功时推送向量化任务，天然防止重复向量化
 */
export async function storeMessage(message: ChatMessage): Promise<void> {
    try {
        const botName = message.bot_name || context.getBotName() || 'bytedance';
        const isEmpty = isEmptyContent(message.content);

        // INSERT ... ON CONFLICT (message_id) DO NOTHING
        const result = await AppDataSource.createQueryBuilder()
            .insert()
            .into(ConversationMessage)
            .values({
                message_id: message.message_id,
                user_id: message.user_id,
                content: message.content,
                role: message.role,
                root_message_id: message.root_message_id || message.message_id,
                reply_message_id: message.reply_message_id,
                chat_id: message.chat_id,
                chat_type: message.chat_type,
                create_time: message.create_time,
                message_type: message.message_type || 'text',
                vector_status: isEmpty ? 'skipped' : 'pending',
                bot_name: botName,
            })
            .orIgnore()
            .execute();

        // result.identifiers 为空数组时表示冲突未插入
        const inserted = result.identifiers.length > 0;

        // 仅首次插入成功且非空消息时推送向量化
        if (inserted && !isEmpty) {
            const lane = context.getLane() || undefined;
            await rabbitmqClient.publish(
                VECTORIZE,
                { message_id: message.message_id, lane: lane },
                undefined,
                undefined,
                lane,
            );
        }
    } catch (error: unknown) {
        console.error('Failed to store message:', (error as Error).message);
    }
}
```

- [ ] **Step 2: 验证 orIgnore 在 TypeORM 0.3.x + PostgreSQL 下的行为**

在本地运行 lark-server 确认：
1. 首次插入返回 `identifiers: [{ message_id: 'xxx' }]`
2. 重复插入返回 `identifiers: []`（不报错）

如果 `orIgnore()` 不可用或 `identifiers` 行为不符合预期，降级为原生 SQL：

```typescript
const result = await AppDataSource.query(
    `INSERT INTO conversation_messages (message_id, user_id, content, role, root_message_id, reply_message_id, chat_id, chat_type, create_time, message_type, vector_status, bot_name)
     VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
     ON CONFLICT (message_id) DO NOTHING
     RETURNING message_id`,
    [message.message_id, message.user_id, message.content, message.role,
     message.root_message_id || message.message_id, message.reply_message_id,
     message.chat_id, message.chat_type, message.create_time,
     message.message_type || 'text', isEmpty ? 'skipped' : 'pending', botName]
);
const inserted = result.length > 0;
```

- [ ] **Step 3: Commit**

```bash
git add apps/lark-server/src/infrastructure/integrations/memory.ts
git commit -m "fix(memory): atomic insert to prevent duplicate vectorization in multi-bot groups"
```

---

### Task 4: chat-response-worker 的 storeMessage 兼容

**Files:**
- Modify: `apps/lark-server/src/workers/chat-response-worker.ts:295` (check only)

- [ ] **Step 1: 检查 chat-response-worker 中的 storeMessage 调用**

`chat-response-worker.ts` 也调用了 `storeMessage` 来存储 bot 的回复消息。这里的 message_id 是 bot 回复的 message_id（不同于用户消息），不会出现多 bot 重复存同一条回复的场景，因此 ON CONFLICT DO NOTHING 不影响正确性。

确认不需要修改，仅检查。

- [ ] **Step 2: Commit（如有改动）**

无改动则跳过。

---

## 灰度启用方式

部署后，管理员在目标群 @赤尾 发送：

```
/config multi_bot set enabled
```

关闭灰度：

```
/config multi_bot set disabled
```

## 上线后手动操作

1. 在数据库中将工具 bot 的 `bot_role` 设为 `utility`：
   ```sql
   UPDATE bot_config SET bot_role = 'utility' WHERE bot_name = '<utility-bot-name>';
   ```
2. 重载 MultiBotManager（重启 lark-server 或触发 reload）
