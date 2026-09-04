// 一条飞书消息落进公共层之后，库里应该多出哪些行。
//
// 这些用例是「common_* 写入矩阵」的可执行版本：每个 describe 对应矩阵里的一行，
// 或者对应一条矩阵没写、但现有实现确实在做的事（见 inbound-projection.ts 顶部的
// 逐条对照）。
//
// 持久化走 LarkTables 端口，测试注入内存实现。那份实现的事务是**真的** —— 写入
// 先落暂存、抛错就整体丢弃 —— 所以「lark_message 插失败 → common_message 不留」
// 验的是生产代码把两条写入放进了同一个 atomically，而不是内存实现自己老实。
// 事务本身老实不老实，由本文件最后一个 describe 单独钉住。

import { describe, expect, it } from 'bun:test';
import { context } from '@inner/shared/middleware';

import type { LarkEvent } from '../ingress/lark-event';
import type { LarkBotLookup } from '../message/mentions';
import { readLarkMessageEvent } from '../message/read-message-event';
import type { LarkMessageEvent } from '../message/wire';
import { projectLarkInbound, type LarkInboundDeps, type LarkInboundOutcome } from './inbound-projection';
import type {
    CommonConversationRow,
    CommonMessageClaim,
    CommonMessageRow,
    CommonUserRow,
    LarkChatKey,
    LarkChatPermission,
    LarkChatRow,
    LarkGroupBinding,
    LarkGroupChatFacts,
    LarkGroupMemberRow,
    LarkMessageRow,
    LarkStore,
    LarkTables,
    LarkUserFacts,
    LarkUserKey,
    LarkUserLink,
    LarkUserProfile,
} from './tables';

// ---------------------------------------------------------------------------
// 内存实现：一组 Map 加一个真事务
// ---------------------------------------------------------------------------

type Row = Record<string, unknown>;

/** upsert 的语义要跟 TypeORM 对齐：值是 undefined 的列不参与覆盖（不会把已有值抹成空）。 */
function upsertInto<T extends Row>(table: Map<string, T>, key: string, row: T): void {
    const existing = table.get(key);
    if (!existing) {
        table.set(key, { ...row });
        return;
    }
    const merged = { ...existing };
    for (const [field, value] of Object.entries(row)) {
        if (value !== undefined) (merged as Row)[field] = value;
    }
    table.set(key, merged);
}

class MemoryLarkTables implements LarkStore {
    commonUsers = new Map<string, Row>();
    commonConversations = new Map<string, Row>();
    commonMessages = new Map<string, Row>();
    botPresence = new Map<string, Row>();
    larkUserOpenIds = new Map<string, LarkUserLink>();
    larkUsers = new Map<string, LarkUserProfile & { union_id: string }>();
    larkChats = new Map<string, Row>();
    larkGroupChats = new Map<string, LarkGroupChatFacts & { chat_id: string }>();
    larkMessages = new Map<string, Row>();
    larkGroupMembers = new Map<string, LarkGroupMemberRow>();
    larkGroupBindings = new Map<string, LarkGroupBinding>();

    /** 注入故障用。 */
    failLarkMessageInsert?: Error;
    failMarkBotPresent?: Error;
    failSaveCommonUser?: Error;
    /** 制造竞态：事务开始前偷偷改一行。 */
    onBeforeAtomically?: () => void;
    /** 制造竞态：读到某张表时挂一下，让另一条流也读到同样的空。 */
    onRead?: (table: string) => Promise<void>;

    snapshot(): unknown {
        return structuredClone({
            commonUsers: [...this.commonUsers],
            commonConversations: [...this.commonConversations],
            commonMessages: [...this.commonMessages],
            botPresence: [...this.botPresence],
            larkUserOpenIds: [...this.larkUserOpenIds],
            larkChats: [...this.larkChats],
            larkMessages: [...this.larkMessages],
        });
    }

    private restore(saved: Array<[Map<string, Row>, Array<[string, Row]>]>): void {
        for (const [table, entries] of saved) {
            table.clear();
            for (const [key, row] of entries) table.set(key, row);
        }
    }

    async atomically<T>(run: (tx: LarkTables) => Promise<T>): Promise<T> {
        this.onBeforeAtomically?.();
        const saved: Array<[Map<string, Row>, Array<[string, Row]>]> = [
            [this.commonMessages, [...this.commonMessages]],
            [this.larkMessages, [...this.larkMessages]],
            [this.commonUsers, [...this.commonUsers]],
            [this.commonConversations, [...this.commonConversations]],
        ];
        try {
            return await run(this);
        } catch (error) {
            this.restore(saved);
            throw error;
        }
    }

    // ---- 读 ----

    async larkUserByOpenId(appId: string, openId: string): Promise<LarkUserLink | null> {
        await this.onRead?.('lark_user_open_id');
        return this.larkUserOpenIds.get(`${appId}|${openId}`) ?? null;
    }

    async larkUserByUnionId(unionId: string): Promise<LarkUserLink | null> {
        // ORDER BY common_user_id ASC —— 取哪一条必须是确定的。
        const matches = [...this.larkUserOpenIds.values()]
            .filter((row) => row.union_id === unionId)
            .sort((a, b) => (a.common_user_id ?? '').localeCompare(b.common_user_id ?? ''));
        return matches[0] ?? null;
    }

    async larkUserProfile(unionId: string): Promise<LarkUserProfile | null> {
        return this.larkUsers.get(unionId) ?? null;
    }

    async larkChat(chatId: string): Promise<LarkChatRow | null> {
        await this.onRead?.('lark_base_chat_info');
        return (this.larkChats.get(chatId) as LarkChatRow | undefined) ?? null;
    }

    async larkGroupChat(chatId: string): Promise<LarkGroupChatFacts | null> {
        return this.larkGroupChats.get(chatId) ?? null;
    }

    async larkMessage(omId: string): Promise<LarkMessageRow | null> {
        return (this.larkMessages.get(omId) as LarkMessageRow | undefined) ?? null;
    }

    // 退群的人不从表里删，所以这里也不过滤 is_leave —— 真身怎么答，这里就怎么答。
    async larkGroupMember(chatId: string, unionId: string): Promise<LarkGroupMemberRow | null> {
        return this.larkGroupMembers.get(`${chatId}|${unionId}`) ?? null;
    }

    // 解绑是软删，所以 is_active 为假的行照样读得到。
    async larkGroupBinding(chatId: string, unionId: string): Promise<LarkGroupBinding | null> {
        return this.larkGroupBindings.get(`${chatId}|${unionId}`) ?? null;
    }

    // ---- 写 ----

    // (user_union_id, chat_id) 上没有唯一约束，所以真身那条是普通 insert：同一对
    // 键写两次会留下两行。这里也不去重 —— 去重了，调用方"先读再写"的竞态就测不出来。
    async insertLarkGroupBinding(chatId: string, unionId: string): Promise<void> {
        this.larkGroupBindings.set(`${chatId}|${unionId}`, {
            user_union_id: unionId,
            chat_id: chatId,
            is_active: true,
        });
    }

    async setLarkGroupBindingActive(
        chatId: string,
        unionId: string,
        isActive: boolean,
    ): Promise<void> {
        const at = `${chatId}|${unionId}`;
        const row = this.larkGroupBindings.get(at);
        // UPDATE 打不到行就是 no-op，不会凭空建一行出来。
        if (row) this.larkGroupBindings.set(at, { ...row, is_active: isActive });
    }

    // 真身那条是 `jsonb ||`：合并，同一列上的其他开关留着。这里也合并 —— 整份覆盖的
    // 假实现会让"开了复读、发图权限没了"这类回归在内存里完全看不出来。
    async setLarkChatPermission(
        chatId: string,
        patch: Partial<LarkChatPermission>,
    ): Promise<void> {
        const row = this.larkChats.get(chatId) as LarkChatRow | undefined;
        if (!row) return;
        this.larkChats.set(chatId, {
            ...row,
            permission_config: { ...row.permission_config, ...patch },
        } as unknown as Row);
    }

    async saveCommonUser(row: CommonUserRow): Promise<void> {
        if (this.failSaveCommonUser) throw this.failSaveCommonUser;
        upsertInto(this.commonUsers, row.common_user_id, row as unknown as Row);
    }

    async saveCommonConversation(row: CommonConversationRow): Promise<void> {
        upsertInto(this.commonConversations, row.common_conversation_id, row as unknown as Row);
    }

    // 认领的两个方法**没有 await**：JS 单线程下这就是一条语句的原子性，忠实模拟
    // 真身那条带 ON CONFLICT ... COALESCE 的 upsert。谁先写进去谁就是 canonical。
    async claimCommonUserId(
        key: LarkUserKey,
        facts: LarkUserFacts,
        candidate: string,
    ): Promise<string> {
        const at = `${key.app_id}|${key.open_id}`;
        const winner = this.larkUserOpenIds.get(at)?.common_user_id ?? candidate;
        this.larkUserOpenIds.set(at, {
            app_id: key.app_id,
            open_id: key.open_id,
            union_id: facts.union_id,
            name: facts.name,
            common_user_id: winner,
        });
        return winner;
    }

    async linkLarkUser(key: LarkUserKey, commonUserId: string): Promise<void> {
        const at = `${key.app_id}|${key.open_id}`;
        const row = this.larkUserOpenIds.get(at);
        if (row) this.larkUserOpenIds.set(at, { ...row, common_user_id: commonUserId });
    }

    async claimCommonConversationId(chat: LarkChatKey, candidate: string): Promise<string> {
        const existing = this.larkChats.get(chat.chat_id) as LarkChatRow | undefined;
        const winner = existing?.common_conversation_id ?? candidate;
        this.larkChats.set(chat.chat_id, {
            // 冲突子句只写 common_conversation_id，同一行上的别的列（权限开关）原封
            // 不动。整行覆盖会把它们抹掉，而真身不会。
            ...existing,
            chat_id: chat.chat_id,
            // 已经有的行不改 chat_mode：那一行可能是别的代码路径按更准的信息建的。
            chat_mode: existing?.chat_mode ?? chat.chat_mode,
            common_conversation_id: winner,
        });
        return winner;
    }

    async insertCommonMessage(row: CommonMessageRow): Promise<void> {
        // insert-or-ignore
        if (this.commonMessages.has(row.common_message_id)) return;
        this.commonMessages.set(row.common_message_id, { ...row });
    }

    async insertLarkMessage(row: LarkMessageRow): Promise<void> {
        if (this.failLarkMessageInsert) throw this.failLarkMessageInsert;
        if (this.larkMessages.has(row.om_id)) {
            throw new Error('duplicate key value violates unique constraint "lark_message_pkey"');
        }
        this.larkMessages.set(row.om_id, { ...row });
    }

    // 投影不认领 —— 那是规则段抢到去重锁之后的事（见 rules/inbound-rules.ts）。
    // 这里实现它只是为了让这份替身完整地满足端口。
    async claimCommonMessageForBot(claim: CommonMessageClaim): Promise<void> {
        const row = this.commonMessages.get(claim.common_message_id);
        if (!row || row.role !== 'user') {
            throw new Error(`no user message ${claim.common_message_id} to claim`);
        }
        row.bot_name = claim.bot_name;
        row.common_user_id = claim.common_user_id;
    }

    // 投影也不撤回 —— 那是入站撤回事件那条路的事（见 lark/recall-message.ts）。这里
    // 实现它同样只是为了让这份替身完整地满足端口，语义照真身：首写保留。
    async markCommonMessageRecalled(commonMessageId: string, recalledAt: Date): Promise<boolean> {
        const row = this.commonMessages.get(commonMessageId);
        if (!row || row.recalled_at) return false;
        row.recalled_at = recalledAt;
        return true;
    }

    async markBotPresent(
        commonConversationId: string,
        botName: string,
        isActive: boolean,
    ): Promise<void> {
        if (this.failMarkBotPresent) throw this.failMarkBotPresent;
        this.botPresence.set(`${commonConversationId}|${botName}`, {
            common_conversation_id: commonConversationId,
            bot_name: botName,
            is_active: isActive,
        });
    }
}

// ---------------------------------------------------------------------------
// 固定装置
// ---------------------------------------------------------------------------

const APP_ID = 'cli_chiwei';
const BOT_NAME = 'chiwei';
const BOT_COMMON_USER_ID = 'cu_bot_chiwei';

const bots: LarkBotLookup = {
    byAppId: (appId) =>
        appId === APP_ID
            ? { botName: BOT_NAME, displayName: '赤尾', commonUserId: BOT_COMMON_USER_ID }
            : null,
    byUnionId: (unionId) =>
        unionId === 'on_bot_chiwei'
            ? { botName: BOT_NAME, displayName: '赤尾', commonUserId: BOT_COMMON_USER_ID }
            : null,
};

function larkMessageEvent(overrides: Partial<LarkMessageEvent['message']> = {}): LarkMessageEvent {
    return {
        app_id: APP_ID,
        sender: { sender_type: 'user', sender_id: { open_id: 'ou_user', union_id: 'on_user' } },
        message: {
            message_id: 'om_1',
            chat_id: 'oc_1',
            chat_type: 'group',
            create_time: '1700000000000',
            message_type: 'text',
            content: '{"text":"hi"}',
            ...overrides,
        },
    };
}

function reading(event: LarkMessageEvent) {
    const parsed = readLarkMessageEvent(event, bots);
    if (!parsed) throw new Error('test fixture is not a message event');
    return parsed;
}

function larkEvent(payload: LarkMessageEvent): LarkEvent {
    return {
        type: 'im.message.receive_v1',
        payload,
        botName: BOT_NAME,
        receivedAt: new Date('2026-09-04T06:50:54.000Z'),
        traceId: 'trace-1',
    };
}

function deps(
    tables: MemoryLarkTables,
    overrides: Partial<LarkInboundDeps> = {},
): { deps: LarkInboundDeps; handedOff: Array<Record<string, unknown>> } {
    const handedOff: Array<Record<string, unknown>> = [];
    let minted = 0;
    return {
        handedOff,
        deps: {
            store: tables,
            newCommonId: () => `id_${++minted}`,
            appIdOfBot: () => APP_ID,
            currentLane: 'prod',
            laneDispatchEnabled: async () => false,
            laneOf: async () => 'prod',
            handOffToLane: async (envelope) => {
                handedOff.push(envelope as unknown as Record<string, unknown>);
            },
            withMessageLock: (_omId, run) => run(),
            ...overrides,
        },
    };
}

async function project(
    tables: MemoryLarkTables,
    event: LarkMessageEvent = larkMessageEvent(),
    overrides: Partial<LarkInboundDeps> = {},
    eventOverrides: Partial<LarkEvent> = {},
) {
    const wired = deps(tables, overrides);
    const outcome = await context.run(context.createContext('trace-1'), () =>
        projectLarkInbound(wired.deps, reading(event), { ...larkEvent(event), ...eventOverrides }),
    );
    return { outcome, handedOff: wired.handedOff };
}

function recorded(outcome: LarkInboundOutcome) {
    if (outcome.kind !== 'recorded') {
        throw new Error(`expected the message to be recorded here, got ${outcome.kind}`);
    }
    return outcome.projection;
}

/** 投影顺路读到的、只有指令层要用的那几件事。 */
function commandFacts(outcome: LarkInboundOutcome) {
    if (outcome.kind !== 'recorded') {
        throw new Error(`expected the message to be recorded here, got ${outcome.kind}`);
    }
    return outcome.commands;
}

/**
 * 让 n 条流都到齐了才继续。用来把"两个进程都读到了空"这个竞态窗口摆到确定的位置
 * —— 不这样的话，两条流会一前一后地跑，永远撞不上。
 */
function barrier(n: number) {
    let arrived = 0;
    let open!: () => void;
    const gate = new Promise<void>((resolve) => {
        open = resolve;
    });
    return async (): Promise<void> => {
        arrived += 1;
        if (arrived >= n) open();
        await gate;
    };
}

// ---------------------------------------------------------------------------

describe('身份对应：common_user + lark_user_open_id', () => {
    it('第一次见到这个人：铸一个 common_user_id，两张表一起记上', async () => {
        const tables = new MemoryLarkTables();
        tables.larkUsers.set('on_user', { union_id: 'on_user', name: '张三' });

        const { outcome } = await project(tables);

        expect(recorded(outcome).commonUserId).toBe('id_1');
        expect(tables.commonUsers.get('id_1')).toEqual({
            common_user_id: 'id_1',
            channel: 'lark',
            display_name: '张三',
        });
        expect(tables.larkUserOpenIds.get(`${APP_ID}|ou_user`)).toEqual({
            app_id: APP_ID,
            open_id: 'ou_user',
            union_id: 'on_user',
            name: '张三',
            common_user_id: 'id_1',
        });
    });

    // 同一个人在每个飞书应用下 open_id 都不一样，union_id 才是稳定的。按 union_id
    // 收敛，否则同一个人会在公共层裂成两个用户。
    it('同一个 union_id 在别的 app 下出现时收敛到已有的 common_user_id', async () => {
        const tables = new MemoryLarkTables();
        tables.larkUserOpenIds.set('cli_other|ou_other', {
            app_id: 'cli_other',
            open_id: 'ou_other',
            union_id: 'on_user',
            name: '张三',
            common_user_id: 'cu_existing',
        });

        const { outcome } = await project(tables);

        expect(recorded(outcome).commonUserId).toBe('cu_existing');
        expect(tables.larkUserOpenIds.get(`${APP_ID}|ou_user`)?.common_user_id).toBe('cu_existing');
        expect([...tables.commonUsers.keys()]).toEqual(['cu_existing']);
    });

    // 一个 union_id 下挂着多条 open_id 行时，取哪一条必须是确定的，否则两个进程
    // 会各自选一条、把同一个人分成两半。
    it('一个 union_id 挂着多条记录时按 common_user_id 升序取第一条', async () => {
        const tables = new MemoryLarkTables();
        for (const [appId, commonUserId] of [
            ['cli_b', 'cu_b'],
            ['cli_a', 'cu_a'],
        ] as const) {
            tables.larkUserOpenIds.set(`${appId}|ou_x`, {
                app_id: appId,
                open_id: 'ou_x',
                union_id: 'on_user',
                name: '张三',
                common_user_id: commonUserId,
            });
        }

        const { outcome } = await project(tables);

        expect(recorded(outcome).commonUserId).toBe('cu_a');
    });

    // 这个人在两个飞书应用下已经各有一个公共层身份了（历史遗留，或者两条流在更早
    // 的某次并发里各建了一份）。收敛到 union 维度那一个 —— 取哪一个由排序定死，
    // 所以两个进程算出来一样，重复跑结果也一样。
    it('本行指向的 id 与 union 维度的 canonical 不一致时收敛过去', async () => {
        const tables = new MemoryLarkTables();
        tables.larkUserOpenIds.set(`${APP_ID}|ou_user`, {
            app_id: APP_ID,
            open_id: 'ou_user',
            union_id: 'on_user',
            name: '张三',
            common_user_id: 'cu_z',
        });
        tables.larkUserOpenIds.set('cli_other|ou_other', {
            app_id: 'cli_other',
            open_id: 'ou_other',
            union_id: 'on_user',
            name: '张三',
            common_user_id: 'cu_a',
        });

        const { outcome } = await project(tables);

        expect(recorded(outcome).commonUserId).toBe('cu_a');
        expect(tables.larkUserOpenIds.get(`${APP_ID}|ou_user`)?.common_user_id).toBe('cu_a');
    });

    it('没有 union_id 时退回按 (app_id, open_id) 找已有映射', async () => {
        const tables = new MemoryLarkTables();
        tables.larkUserOpenIds.set(`${APP_ID}|ou_user`, {
            app_id: APP_ID,
            open_id: 'ou_user',
            union_id: undefined,
            name: '老名字',
            common_user_id: 'cu_kept',
        });
        const event = larkMessageEvent();
        event.sender.sender_id = { open_id: 'ou_user' };

        const { outcome } = await project(tables, event);

        expect(recorded(outcome).commonUserId).toBe('cu_kept');
        // 事件里查不到新名字时不能把已有的名字抹掉
        expect(tables.larkUserOpenIds.get(`${APP_ID}|ou_user`)?.name).toBe('老名字');
    });

    it('发送者没有 open_id 就没法映射到公共层用户，直接炸', async () => {
        const tables = new MemoryLarkTables();
        const event = larkMessageEvent();
        event.sender.sender_id = { union_id: 'on_user' };

        await expect(project(tables, event)).rejects.toThrow(/open_id/);
    });
});

// codex 指出的竞态：om_id 锁只保护"同一条消息"，但同一个人/同一个会话可以在**不同
// 消息**里被并发地第一次创建。两条流各自读到空、各铸一个 id，后写的还会把前一个的
// 映射覆盖掉 —— 回读一次挡不住，因为回读也可能发生在对方写入之前。
//
// 修法不是再加一把锁，而是让自然键自己回答"谁是 canonical"：认领用一条带
// ON CONFLICT ... COALESCE 的 upsert，返回**库里最终生效的那一个**，调用方一律用
// 返回值而不是自己铸的候选值。
describe('并发第一次创建', () => {
    /** 两条流：不同消息、同一个人、同一个会话。都在读完之后才允许去认领。 */
    async function twoAtOnce(tables: MemoryLarkTables) {
        const bothRead = barrier(2);
        tables.onRead = async (table) => {
            if (table === 'lark_user_open_id' || table === 'lark_base_chat_info') {
                await bothRead();
            }
        };
        const mint = (prefix: string) => {
            let n = 0;
            return () => `${prefix}${++n}`;
        };

        return Promise.all([
            project(tables, larkMessageEvent({ message_id: 'om_a' }), {
                newCommonId: mint('A'),
            }),
            project(tables, larkMessageEvent({ message_id: 'om_b' }), {
                newCommonId: mint('B'),
            }),
        ]);
    }

    it('两条流同时第一次见到同一个人：恰好一个 common_user_id', async () => {
        const tables = new MemoryLarkTables();
        const [a, b] = await twoAtOnce(tables);

        expect(recorded(a.outcome).commonUserId).toBe(recorded(b.outcome).commonUserId);
        // 输的那一条绝不能把自己铸的 id 也写进 common_user —— 那就是一条没人指向的
        // 孤儿身份，而赤尾对这个人的记忆会从此分成两半。
        expect(tables.commonUsers.size).toBe(1);
        expect(tables.larkUserOpenIds.size).toBe(1);
        expect([...tables.commonUsers.keys()]).toEqual([recorded(a.outcome).commonUserId]);
    });

    it('两条流同时第一次见到同一个会话：恰好一个 common_conversation_id', async () => {
        const tables = new MemoryLarkTables();
        const [a, b] = await twoAtOnce(tables);

        expect(recorded(a.outcome).commonConversationId).toBe(
            recorded(b.outcome).commonConversationId,
        );
        expect(tables.commonConversations.size).toBe(1);
        expect(tables.larkChats.size).toBe(1);
    });

    it('两条消息都落了账，且都挂在同一个人同一个会话下', async () => {
        const tables = new MemoryLarkTables();
        const [a] = await twoAtOnce(tables);

        expect(tables.commonMessages.size).toBe(2);
        expect(tables.larkMessages.size).toBe(2);
        for (const row of tables.commonMessages.values()) {
            expect(row.common_user_id).toBe(recorded(a.outcome).commonUserId);
            expect(row.common_conversation_id).toBe(recorded(a.outcome).commonConversationId);
        }
    });

    // 认领的返回值就是契约本身。调用方要是继续用自己铸的候选值，上面几条全会红。
    it('认领返回的是库里最终生效的那一个，不是调用方给的候选值', async () => {
        const tables = new MemoryLarkTables();
        const key = { app_id: APP_ID, open_id: 'ou_user' };

        const first = await tables.claimCommonUserId(key, { name: '张三' }, 'first');
        const second = await tables.claimCommonUserId(key, { name: '张三' }, 'second');

        expect(first).toBe('first');
        expect(second).toBe('first');
    });
});

// 顺序也是修的一部分：先认领渠道映射（它定 canonical），再写 common_* 那一行。
// 反过来的话，中途失败会留下一条没人指向的 common_user；这个顺序下，中途失败留下
// 的是"映射有、common 行还没建"，下一条消息照着映射把它补上。
describe('身份写入的顺序与自愈', () => {
    it('映射行是 canonical 的唯一来源，common_user 由它派生', async () => {
        const tables = new MemoryLarkTables();
        tables.failSaveCommonUser = new Error('common_user is on fire');

        await expect(project(tables)).rejects.toThrow('common_user is on fire');

        // 映射已经落地并定下了 canonical
        expect(tables.larkUserOpenIds.get(`${APP_ID}|ou_user`)?.common_user_id).toBe('id_1');
        expect(tables.commonUsers.size).toBe(0);
    });

    it('下一条消息照着映射把 common_user 补上，且不换 id', async () => {
        const tables = new MemoryLarkTables();
        tables.failSaveCommonUser = new Error('common_user is on fire');
        await expect(project(tables)).rejects.toThrow();

        tables.failSaveCommonUser = undefined;
        const { outcome } = await project(tables, larkMessageEvent({ message_id: 'om_2' }));

        expect(recorded(outcome).commonUserId).toBe('id_1');
        expect([...tables.commonUsers.keys()]).toEqual(['id_1']);
    });
});

describe('身份对应：被 @ 的人', () => {
    function withMentions(): LarkMessageEvent {
        return larkMessageEvent({
            content: '{"text":"@_user_1 @_user_2 @_user_1 在吗"}',
            mentions: [
                {
                    key: '@_user_1',
                    id: { union_id: 'on_bot_chiwei' },
                    name: 'chiwei-raw',
                    mentioned_type: 'bot',
                    bot_info: { app_id: APP_ID },
                },
                {
                    key: '@_user_2',
                    id: { union_id: 'on_li', open_id: 'ou_li' },
                    name: '李四',
                    mentioned_type: 'user',
                },
                {
                    key: '@_user_1',
                    id: { union_id: 'on_bot_chiwei' },
                    name: 'chiwei-raw',
                    mentioned_type: 'bot',
                    bot_info: { app_id: APP_ID },
                },
            ],
        });
    }

    it('自家 bot 用它启动时回填的 common_user_id，投影层不再铸新的', async () => {
        const tables = new MemoryLarkTables();
        const { outcome } = await project(tables, withMentions());

        // 发送者是 id_1，被 @ 的真人是 id_2；bot 用的是目录里那个
        expect(recorded(outcome).mentionedCommonUserIds).toEqual([BOT_COMMON_USER_ID, 'id_2']);
        expect(tables.commonUsers.has(BOT_COMMON_USER_ID)).toBe(false);
    });

    it('被 @ 的真人登记进 common_user，重复 @ 同一人只算一次', async () => {
        const tables = new MemoryLarkTables();
        await project(tables, withMentions());

        expect(tables.larkUserOpenIds.get(`${APP_ID}|ou_li`)).toMatchObject({
            union_id: 'on_li',
            name: '李四',
            common_user_id: 'id_2',
        });
    });

    it('既不是自家 bot、又没有 open_id 的 @ 无法映射，直接炸', async () => {
        const tables = new MemoryLarkTables();
        const event = larkMessageEvent({
            content: '{"text":"@_user_1"}',
            mentions: [{ key: '@_user_1', id: { union_id: 'on_ghost' }, name: '幽灵' }],
        });

        await expect(project(tables, event)).rejects.toThrow(/open_id/);
    });
});

describe('会话对应：common_conversation + lark_base_chat_info', () => {
    it('第一次见到这个群：铸会话 id，并把群资料投影过去', async () => {
        const tables = new MemoryLarkTables();
        tables.larkGroupChats.set('oc_1', {
            chat_id: 'oc_1',
            name: '赤尾的群',
            avatar: 'https://avatar',
            user_count: 7,
            is_leave: false,
            download_has_permission_setting: 'all_members',
        });

        const { outcome } = await project(tables);

        expect(recorded(outcome).commonConversationId).toBe('id_2');
        expect(tables.commonConversations.get('id_2')).toEqual({
            common_conversation_id: 'id_2',
            channel: 'lark',
            scope: 'group',
            display_name: '赤尾的群',
            avatar_url: 'https://avatar',
            member_count: 7,
            is_active: true,
            attachment_policy: { download_allowed: true, source: 'lark' },
        });
        expect(tables.larkChats.get('oc_1')).toEqual({
            chat_id: 'oc_1',
            chat_mode: 'group',
            common_conversation_id: 'id_2',
        });
    });

    // 群里没开「所有人可下载」时附件策略要如实投影 —— 下游按它决定要不要取原图。
    it('群的下载权限投影进 attachment_policy', async () => {
        const tables = new MemoryLarkTables();
        tables.larkGroupChats.set('oc_1', {
            chat_id: 'oc_1',
            name: '赤尾的群',
            user_count: 3,
            download_has_permission_setting: 'not_anyone',
        });

        await project(tables);

        expect(tables.commonConversations.get('id_2')?.attachment_policy).toEqual({
            download_allowed: false,
            source: 'lark',
        });
    });

    it('群资料查不到时按「允许下载、还在群里」投影', async () => {
        const tables = new MemoryLarkTables();
        await project(tables);

        expect(tables.commonConversations.get('id_2')).toMatchObject({
            is_active: true,
            attachment_policy: { download_allowed: true, source: 'lark' },
        });
    });

    it('bot 已退群时会话标成不活跃', async () => {
        const tables = new MemoryLarkTables();
        tables.larkGroupChats.set('oc_1', {
            chat_id: 'oc_1',
            name: '赤尾的群',
            user_count: 0,
            is_leave: true,
        });

        await project(tables);

        expect(tables.commonConversations.get('id_2')?.is_active).toBe(false);
    });

    it('私聊用发送者的资料当会话名，chat_mode 记成 p2p', async () => {
        const tables = new MemoryLarkTables();
        tables.larkUsers.set('on_user', {
            union_id: 'on_user',
            name: '张三',
            avatar_origin: 'https://zhangsan',
        });

        await project(tables, larkMessageEvent({ chat_type: 'p2p' }));

        expect(tables.commonConversations.get('id_2')).toMatchObject({
            scope: 'direct',
            display_name: '张三',
            avatar_url: 'https://zhangsan',
            is_active: true,
        });
        expect(tables.larkChats.get('oc_1')?.chat_mode).toBe('p2p');
    });

    it('已经有映射的会话沿用它的 id，只刷会变的那几项', async () => {
        const tables = new MemoryLarkTables();
        tables.larkChats.set('oc_1', {
            chat_id: 'oc_1',
            chat_mode: 'group',
            common_conversation_id: 'cc_existing',
        });
        tables.commonConversations.set('cc_existing', {
            common_conversation_id: 'cc_existing',
            channel: 'lark',
            scope: 'group',
            display_name: '旧名字',
            is_active: true,
        });
        tables.larkGroupChats.set('oc_1', { chat_id: 'oc_1', name: '新名字', user_count: 9 });

        const { outcome } = await project(tables);

        expect(recorded(outcome).commonConversationId).toBe('cc_existing');
        expect([...tables.commonConversations.keys()]).toEqual(['cc_existing']);
        expect(tables.commonConversations.get('cc_existing')).toMatchObject({
            display_name: '新名字',
            member_count: 9,
            // 写回去的 channel / scope 是同一个值，等于什么都没改
            scope: 'group',
            channel: 'lark',
        });
    });

    // 另一条代码路径（比如"用户进入私聊"事件）会建一条只有 chat_id / chat_mode 的
    // 行。认领必须能把 common_conversation_id 补上，而不是撞了主键就放弃。
    it('会话行已存在但还没对应到公共层时，把对应关系补上', async () => {
        const tables = new MemoryLarkTables();
        tables.larkChats.set('oc_1', { chat_id: 'oc_1', chat_mode: 'p2p' });

        const { outcome } = await project(tables, larkMessageEvent({ chat_type: 'p2p' }));

        expect(tables.larkChats.get('oc_1')).toEqual({
            chat_id: 'oc_1',
            chat_mode: 'p2p',
            common_conversation_id: recorded(outcome).commonConversationId,
        });
        expect(tables.commonConversations.size).toBe(1);
    });

    // 中途崩在"映射已写、会话行还没建"之间：下一条消息照着映射把它补上。
    it('映射有、公共层会话行没有时，下一条消息补建', async () => {
        const tables = new MemoryLarkTables();
        tables.larkChats.set('oc_1', {
            chat_id: 'oc_1',
            chat_mode: 'group',
            common_conversation_id: 'cc_orphan',
        });

        const { outcome } = await project(tables);

        expect(recorded(outcome).commonConversationId).toBe('cc_orphan');
        expect(tables.commonConversations.get('cc_orphan')).toMatchObject({
            common_conversation_id: 'cc_orphan',
            channel: 'lark',
            scope: 'group',
        });
    });
});

describe('落账：common_message + lark_message', () => {
    it('两张表一起写：通用消息记 common 口径，飞书消息记原始报文', async () => {
        const tables = new MemoryLarkTables();
        const event = larkMessageEvent({ root_id: 'om_1' });
        const { outcome } = await project(tables, event);

        expect(recorded(outcome)).toMatchObject({
            commonUserId: 'id_1',
            commonConversationId: 'id_2',
            commonMessageId: 'id_3',
            commonRootMessageId: 'id_3',
            commonReplyMessageId: undefined,
        });
        expect(tables.commonMessages.get('id_3')).toEqual({
            common_message_id: 'id_3',
            channel: 'lark',
            common_conversation_id: 'id_2',
            common_user_id: 'id_1',
            sender_display_name: undefined,
            role: 'user',
            content: [{ kind: 'text', text: 'hi' }],
            content_text: 'hi',
            common_root_message_id: 'id_3',
            common_reply_message_id: undefined,
            mentioned_common_user_ids: [],
            scope: 'group',
            message_type: 'text',
            bot_name: BOT_NAME,
            event_time: '1700000000000',
        });
        expect(tables.larkMessages.get('om_1')).toEqual({
            om_id: 'om_1',
            common_message_id: 'id_3',
            chat_id: 'oc_1',
            sender_open_id: 'ou_user',
            sender_union_id: 'on_user',
            root_om_id: 'om_1',
            reply_om_id: undefined,
            message_type: 'text',
            raw_event: event,
        });
    });

    it('发送者展示名取自 lark_user 档案（事件里根本没有名字）', async () => {
        const tables = new MemoryLarkTables();
        tables.larkUsers.set('on_user', { union_id: 'on_user', name: '张三' });

        await project(tables);

        expect(tables.commonMessages.get('id_3')?.sender_display_name).toBe('张三');
    });

    // 档案同时喂给身份对应（common_user.display_name）和落账
    // （common_message.sender_display_name）。查两遍就是每条消息白打一次库。
    it('档案只查一次，两处共用', async () => {
        const tables = new MemoryLarkTables();
        tables.larkUsers.set('on_user', { union_id: 'on_user', name: '张三' });
        const read = tables.larkUserProfile.bind(tables);
        let reads = 0;
        tables.larkUserProfile = async (unionId) => {
            reads += 1;
            return read(unionId);
        };

        await project(tables);

        expect(reads).toBe(1);
    });

    // content_text 是给人看的一行摘要：非文字片段折成 [kind]，全空则不写。
    it('非文字消息的 content_text 折成占位串', async () => {
        const tables = new MemoryLarkTables();
        await project(
            tables,
            larkMessageEvent({ message_type: 'image', content: '{"image_key":"img_1"}' }),
        );

        expect(tables.commonMessages.get('id_3')).toMatchObject({
            content: [{ kind: 'image', key: 'img_1' }],
            content_text: '[image]',
            message_type: 'image',
        });
    });

    it('正文为空时 content_text 留空而不是空串', async () => {
        const tables = new MemoryLarkTables();
        await project(tables, larkMessageEvent({ content: '{"text":"  "}' }));

        expect(tables.commonMessages.get('id_3')?.content_text).toBeUndefined();
    });

    // 「这条消息点了谁的名」必须**跟着消息一起落库**，不能只交给规则引擎。
    // 公共层的内容契约里没有 mention 这种片段，@ 在投影时被内联回了正文，出了这一次
    // 请求就再也认不出来 —— 而 agent-service 判断"群里叫的是不是我"是异步的、晚得多。
    const BOT_MENTION = {
        key: '@_user_1',
        id: { union_id: 'on_bot_chiwei' },
        name: 'chiwei-raw',
        mentioned_type: 'bot',
        bot_info: { app_id: APP_ID },
    };
    const HUMAN_MENTION = {
        key: '@_user_2',
        id: { union_id: 'on_li', open_id: 'ou_li' },
        name: '李四',
        mentioned_type: 'user',
    };

    it('点了她的名，记下的是她在公共层的 id', async () => {
        const tables = new MemoryLarkTables();
        await project(
            tables,
            larkMessageEvent({ content: '{"text":"@_user_1 在吗"}', mentions: [BOT_MENTION] }),
        );

        expect(tables.commonMessages.get('id_3')?.mentioned_common_user_ids).toEqual([
            BOT_COMMON_USER_ID,
        ]);
    });

    it('点的是别人，记下的就只有别人', async () => {
        const tables = new MemoryLarkTables();
        const { outcome } = await project(
            tables,
            larkMessageEvent({ content: '{"text":"@_user_2 帮个忙"}', mentions: [HUMAN_MENTION] }),
        );

        const stored = tables.commonMessages.get(recorded(outcome).commonMessageId);
        expect(stored?.mentioned_common_user_ids).toEqual(['id_2']);
        expect(stored?.mentioned_common_user_ids).not.toContain(BOT_COMMON_USER_ID);
    });

    // 空数组和留空在读的一侧是两件事：空数组 = 算过、确实谁都没点；留空（库里的
    // NULL）= 没人算过这条消息。后者绝不能被当成"确认没人被点"。
    it('谁都没点时记空数组，不是留空', async () => {
        const tables = new MemoryLarkTables();
        await project(tables);

        expect(tables.commonMessages.get('id_3')?.mentioned_common_user_ids).toEqual([]);
    });
});

describe('落账：回复链收敛成 common id', () => {
    function alreadyStored(tables: MemoryLarkTables, omId: string, commonMessageId: string): void {
        tables.larkMessages.set(omId, {
            om_id: omId,
            common_message_id: commonMessageId,
            chat_id: 'oc_1',
            message_type: 'text',
        });
    }

    it('root / parent 指向的飞书消息映射成 common_message_id', async () => {
        const tables = new MemoryLarkTables();
        alreadyStored(tables, 'om_root', 'cm_root');
        alreadyStored(tables, 'om_parent', 'cm_parent');

        const { outcome } = await project(
            tables,
            larkMessageEvent({ root_id: 'om_root', parent_id: 'om_parent' }),
        );

        expect(recorded(outcome)).toMatchObject({
            commonRootMessageId: 'cm_root',
            commonReplyMessageId: 'cm_parent',
        });
    });

    // 被回复的那条可能从来没被处理过（当时不在群里 / 那条消息没入库）。丢链接、
    // 留消息，比整条入站炸掉好。
    it('引用的消息没有映射时丢掉这条链接，消息照常入库', async () => {
        const tables = new MemoryLarkTables();
        const { outcome } = await project(
            tables,
            larkMessageEvent({ root_id: 'om_gone', parent_id: 'om_also_gone' }),
        );

        expect(recorded(outcome)).toMatchObject({
            // root 查不到就退回自己，这样每条消息都在某个话题串里
            commonRootMessageId: 'id_3',
            commonReplyMessageId: undefined,
        });
        expect(tables.commonMessages.has('id_3')).toBe(true);
    });

    it('root 指向自己时直接用本条的 common id', async () => {
        const tables = new MemoryLarkTables();
        const { outcome } = await project(tables, larkMessageEvent({ root_id: 'om_1' }));
        expect(recorded(outcome).commonRootMessageId).toBe('id_3');
    });
});

describe('重放安全', () => {
    // 泳道消费是 at-least-once：投递成功和写完成标记之间崩一次，同一条消息会被
    // 完整重放。整条投影必须能安全地跑第二遍。
    it('同一条消息跑两遍：库里的行与产出的 id 都不变', async () => {
        const tables = new MemoryLarkTables();
        const event = larkMessageEvent();

        const first = await project(tables, event);
        const afterFirst = tables.snapshot();
        const second = await project(tables, event);

        expect(second.outcome).toEqual(first.outcome);
        expect(tables.snapshot()).toEqual(afterFirst);
    });

    it('第二遍复用第一遍铸好的 common_message_id，而不是再铸一个', async () => {
        const tables = new MemoryLarkTables();
        const event = larkMessageEvent();

        await project(tables, event);
        // 换一套 id 发生器：如果第二遍还在铸新 id，这里会看到 fresh_1
        const { outcome } = await project(tables, event, { newCommonId: () => 'fresh_1' });

        expect(recorded(outcome).commonMessageId).toBe('id_3');
        expect(tables.commonMessages.size).toBe(1);
        expect(tables.larkMessages.size).toBe(1);
    });

    // 同一条 om_id 已经映射到别的 common_message_id：说明有人算错了，继续写会让
    // 同一条飞书消息在公共层有两个身份。这里制造的是竞态 —— 读的时候没有、写的
    // 时候被别人抢先写上了。
    it('om_id 已映射到别的 common_message_id 时拒绝落账', async () => {
        const tables = new MemoryLarkTables();

        await expect(
            project(tables, larkMessageEvent(), {
                withMessageLock: async (_omId, run) => {
                    tables.onBeforeAtomically = () => {
                        tables.larkMessages.set('om_1', {
                            om_id: 'om_1',
                            common_message_id: 'cm_other',
                            chat_id: 'oc_1',
                            message_type: 'text',
                        });
                        tables.onBeforeAtomically = undefined;
                    };
                    return run();
                },
            }),
        ).rejects.toThrow(/already maps to cm_other/);

        expect(tables.commonMessages.size).toBe(0);
    });
});

describe('两张表同事务', () => {
    it('lark_message 插不进去时 common_message 一起回滚', async () => {
        const tables = new MemoryLarkTables();
        tables.failLarkMessageInsert = new Error('duplicate key value violates unique constraint');

        await expect(project(tables)).rejects.toThrow(/common_message insert rolled back/);

        expect(tables.commonMessages.size).toBe(0);
        expect(tables.larkMessages.size).toBe(0);
    });

    // 回滚只退掉这个事务里的两条写入。身份对应发生在事务之前，是**已经落库的既成
    // 事实** —— 这一点必须写清楚，否则读代码的人会以为整条投影是原子的。
    it('回滚不会退掉事务之前写好的用户与会话', async () => {
        const tables = new MemoryLarkTables();
        tables.failLarkMessageInsert = new Error('boom');

        await expect(project(tables)).rejects.toThrow();

        expect(tables.commonUsers.size).toBe(1);
        expect(tables.commonConversations.size).toBe(1);
    });

    // 上面两条的前提是内存实现的事务真的会回滚。它要是假的，那两条就成了空转。
    it('内存实现的事务确实会整体回滚（上面两条不是空转）', async () => {
        const tables = new MemoryLarkTables();
        await expect(
            tables.atomically(async (tx) => {
                await tx.insertCommonMessage({
                    common_message_id: 'cm_x',
                    channel: 'lark',
                    common_conversation_id: 'cc',
                    common_user_id: 'cu',
                    sender_display_name: undefined,
                    role: 'user',
                    content: [],
                    content_text: undefined,
                    mentioned_common_user_ids: [],
                    common_root_message_id: 'cm_x',
                    common_reply_message_id: undefined,
                    scope: 'group',
                    message_type: 'text',
                    bot_name: BOT_NAME,
                    event_time: '1',
                });
                expect(tables.commonMessages.size).toBe(1);
                throw new Error('rollback please');
            }),
        ).rejects.toThrow('rollback please');
        expect(tables.commonMessages.size).toBe(0);
    });
});

describe('泳道分叉', () => {
    const toLane = { laneDispatchEnabled: async () => true, laneOf: async () => 'ppe-x' };

    it('该走别的泳道时交出去，本地不落账', async () => {
        const tables = new MemoryLarkTables();
        const { outcome } = await project(tables, larkMessageEvent(), toLane);

        expect(outcome).toEqual({ kind: 'handed-off', lane: 'ppe-x' });
        expect(tables.commonMessages.size).toBe(0);
        expect(tables.larkMessages.size).toBe(0);
        expect(tables.botPresence.size).toBe(0);
    });

    // 现状如此，而且是本段最容易被误解的一点：身份对应发生在分叉**之前**，所以
    // 即使这条消息随后被交给泳道，prod 库里也已经留下了用户和会话行。
    it('交出去之前，用户与会话已经写进本进程的库了', async () => {
        const tables = new MemoryLarkTables();
        await project(tables, larkMessageEvent(), toLane);

        expect(tables.commonUsers.size).toBe(1);
        expect(tables.commonConversations.size).toBe(1);
        expect(tables.larkUserOpenIds.size).toBe(1);
        expect(tables.larkChats.size).toBe(1);
    });

    it('信封带上目标泳道、全局消息 id、bot 与原始报文', async () => {
        const tables = new MemoryLarkTables();
        const event = larkMessageEvent();
        const { handedOff } = await project(tables, event, toLane);

        expect(handedOff).toEqual([
            {
                channel: 'lark',
                event_type: 'im.message.receive_v1',
                global_message_id: 'id_3',
                trace_id: 'trace-1',
                lane: 'ppe-x',
                bot_name: BOT_NAME,
                params: event,
                // 接收端认这个标记来停止二次判定。泳道缺席时 sidecar 把请求打回 prod
                // 自己，不带这个标记就是无限自投。
                handed_off: true,
            },
        ]);
    });

    // 落回 prod 的那一支：信封说 ppe-x，处理它的却是 prod 进程，绑定也还指向 ppe-x。
    // 只有信封上的「已交接」标记能挡住第二次投递。
    it('交接来的事件不再判泳道，即使本进程是 prod 且绑定仍指向那条泳道', async () => {
        const tables = new MemoryLarkTables();
        let asked = 0;
        const { outcome, handedOff } = await project(
            tables,
            larkMessageEvent(),
            {
                laneDispatchEnabled: async () => true,
                laneOf: async () => {
                    asked += 1;
                    return 'ppe-x';
                },
            },
            { handedOff: true, lane: 'ppe-x' },
        );

        expect(asked).toBe(0);
        expect(handedOff).toEqual([]);
        expect(outcome).toMatchObject({ kind: 'recorded' });
    });

    // 交接是一次跨服务调用，而这把锁是 Redis 的、prod 与泳道共用同一个 Redis。在锁里
    // 等交接返回的话，接收端重走投影去抢同一个 om_id 的锁，两边就互等到窗口超时。所以
    // 锁只覆盖投影与判定，交接在锁外做。
    it('交接发生在锁释放之后', async () => {
        const tables = new MemoryLarkTables();
        const trace: string[] = [];

        await project(tables, larkMessageEvent(), {
            ...toLane,
            withMessageLock: async (omId, run) => {
                trace.push(`acquire:${omId}`);
                try {
                    return await run();
                } finally {
                    trace.push(`release:${omId}`);
                }
            },
            handOffToLane: async () => {
                trace.push('hand-off');
            },
        });

        expect(trace).toEqual(['acquire:om_1', 'release:om_1', 'hand-off']);
    });

    it('算出来的泳道就是本进程时留在本地', async () => {
        const tables = new MemoryLarkTables();
        const { outcome, handedOff } = await project(tables, larkMessageEvent(), {
            laneDispatchEnabled: async () => true,
            laneOf: async () => 'prod',
        });

        expect(outcome).toMatchObject({ kind: 'recorded' });
        expect(handedOff).toEqual([]);
    });

    // 泳道部署拿到的信封已经是「判过一次」的结果，再判一次会在绑定刚改过时把消息
    // 二次转投，破坏信封的幂等三元组。
    it('本进程不是 prod 时不再判泳道', async () => {
        const tables = new MemoryLarkTables();
        let asked = 0;
        const { outcome, handedOff } = await project(tables, larkMessageEvent(), {
            currentLane: 'ppe-x',
            laneDispatchEnabled: async () => true,
            laneOf: async () => {
                asked += 1;
                return 'ppe-y';
            },
        });

        expect(asked).toBe(0);
        expect(outcome).toMatchObject({ kind: 'recorded' });
        expect(handedOff).toEqual([]);
    });

    it('开关关着时连泳道都不算', async () => {
        const tables = new MemoryLarkTables();
        let asked = 0;
        await project(tables, larkMessageEvent(), {
            laneDispatchEnabled: async () => false,
            laneOf: async () => {
                asked += 1;
                return 'ppe-x';
            },
        });

        expect(asked).toBe(0);
    });

    // 投递失败绝不能被吞：吞了就是这条消息谁也没处理，而且没有任何信号。
    it('投递失败往上抛，不退回本地处理', async () => {
        const tables = new MemoryLarkTables();
        await expect(
            project(tables, larkMessageEvent(), {
                ...toLane,
                handOffToLane: async () => {
                    throw new Error('broker is down');
                },
            }),
        ).rejects.toThrow('broker is down');
        expect(tables.commonMessages.size).toBe(0);
    });
});

describe('bot 在场', () => {
    it('本地处理的每条消息都刷新 bot 在这个会话里的在场状态', async () => {
        const tables = new MemoryLarkTables();
        await project(tables);

        expect(tables.botPresence.get(`id_2|${BOT_NAME}`)).toEqual({
            common_conversation_id: 'id_2',
            bot_name: BOT_NAME,
            is_active: true,
        });
    });

    // 在场状态是旁路：它写不进去不该让消息丢掉。
    it('在场状态写失败不挡消息落账', async () => {
        const tables = new MemoryLarkTables();
        tables.failMarkBotPresent = new Error('presence table is on fire');

        const { outcome } = await project(tables);

        expect(outcome).toMatchObject({ kind: 'recorded' });
        expect(tables.commonMessages.size).toBe(1);
    });
});

describe('同一条消息的串行化', () => {
    // 同群多个 bot 会并发处理同一条 om_id。不串行就会各铸一个 common_message_id，
    // 谁先写谁赢，剩下的成为孤儿。
    it('整条投影跑在按 om_id 取的锁里', async () => {
        const tables = new MemoryLarkTables();
        const trace: string[] = [];
        await project(tables, larkMessageEvent(), {
            withMessageLock: async (omId, run) => {
                trace.push(`enter:${omId}`);
                const result = await run();
                trace.push(`leave:${omId}`);
                return result;
            },
        });

        expect(trace).toEqual(['enter:om_1', 'leave:om_1']);
    });
});

describe('指令事实：投影顺路读到的、只有指令层要用的那几件', () => {
    // is_admin 跟发送者的名字在同一行、permission_config 跟会话映射在同一行、群资料
    // 里的 user_count 和 download_has_permission_setting 跟 attachment_policy 在同一行
    // —— 投影为了别的事本来就要读这三行。不带出去的话，指令层只能各自再查一遍。
    it('is_admin / permission_config / 群资料一并交给规则段', async () => {
        const tables = new MemoryLarkTables();
        tables.larkUsers.set('on_user', { union_id: 'on_user', name: '张三', is_admin: true });
        tables.larkChats.set('oc_1', {
            chat_id: 'oc_1',
            chat_mode: 'group',
            common_conversation_id: 'cc_1',
            permission_config: { open_repeat_message: true, allow_send_pixiv_image: true },
        });
        tables.larkGroupChats.set('oc_1', {
            chat_id: 'oc_1',
            name: '水群',
            user_count: 7,
            download_has_permission_setting: 'all_members',
        });

        const { outcome } = await project(tables);

        const facts = commandFacts(outcome);
        expect(facts.appId).toBe(APP_ID);
        expect(facts.isAdmin).toBe(true);
        expect(facts.permission).toEqual({
            open_repeat_message: true,
            allow_send_pixiv_image: true,
        });
        // 拆分前 sendPhoto 读 user_count、genMeme 读 download_has_permission_setting，
        // 各自又查了一次 lark_group_chat_info —— 那一行此刻就在手上。
        expect(facts.groupChat).toMatchObject({
            name: '水群',
            user_count: 7,
            download_has_permission_setting: 'all_members',
        });
    });

    // 这两列都 nullable，老行上压根没有。"没配过"一律等于关，所以交出去的是 false 和
    // 空对象 —— 让每个指令自己写 `?.` 兜底，迟早有人漏一处。
    it('查不到那几行时：不是管理员、开关全空、没有群资料', async () => {
        const tables = new MemoryLarkTables();

        const { outcome } = await project(tables, larkMessageEvent({ chat_type: 'p2p' }));

        expect(commandFacts(outcome)).toEqual({
            appId: APP_ID,
            isAdmin: false,
            permission: {},
            groupChat: null,
        });
    });

    // 搭车读的全部意义就在这里：多带一列不多一次查询。permission_config 要是自己再查
    // 一次 lark_base_chat_info，每条入站消息就多一条 SQL。
    it('permission_config 与建会话行读的是同一次 lark_base_chat_info', async () => {
        const tables = new MemoryLarkTables();
        let reads = 0;
        tables.onRead = async (table) => {
            if (table === 'lark_base_chat_info') reads += 1;
        };

        await project(tables);

        expect(reads).toBe(1);
    });

    // 事件里没带 app_id 时用处理这条事件的 bot 自己的应用兜底 —— 与建身份映射用的是
    // 同一个值。「撤回」拿它跟消息的 sender.id 比，两处算得不一样就会去撤别人的消息。
    it('appId 事件里没有时按 bot 兜底，与身份映射用的是同一个', async () => {
        const tables = new MemoryLarkTables();
        const event = larkMessageEvent();
        delete (event as { app_id?: string }).app_id;

        const { outcome } = await project(tables, event);

        expect(commandFacts(outcome).appId).toBe(APP_ID);
        expect(tables.larkUserOpenIds.has(`${APP_ID}|ou_user`)).toBe(true);
    });
});
