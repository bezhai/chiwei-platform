// 一段回复从队列里出来之后，飞书那边和库里应该发生什么。
//
// 这些用例是拆分前 channel-server chat-response-handler 那条链的行为基线：反查、
// 渲染、发送、落库、记台账，逐个分支各一条。持久化和飞书 API 全部走端口，测试注入
// 内存实现。
//
// 内存 store 的事务是**真的** —— 写入先落暂存、抛错整体丢弃 —— 所以「lark_message
// 插失败 → common_message 不留」验的是生产代码把两条写入放进了同一个 atomically，
// 而不是内存实现自己老实。事务本身老实不老实，由最后一个 describe 单独钉住。

import { describe, expect, it } from 'bun:test';

import type { LarkChatResponse } from './chat-response';
import { deliverLarkChatResponse, type LarkDeliveryDeps } from './deliver';
import type { LarkPictureDeps } from './pictures';
import { createLarkPostRenderer } from './render';
import type {
    LarkAgentResponseRow,
    LarkResponseLedger,
    LarkResponseOutcome,
} from './ledger';
import type { LarkSentMessage } from './lark-api';
import type { PostContent } from './post-content';
import type { LarkRenderContext } from './render';
import type {
    LarkAssistantMessageRow,
    LarkOutboundMapping,
    LarkOutboundStore,
    LarkOutboundTables,
} from './tables';

// ---------------------------------------------------------------------------
// 内存实现
// ---------------------------------------------------------------------------

class MemoryOutboundTables implements LarkOutboundStore {
    /** common_conversation_id → 飞书 chat_id */
    chats = new Map<string, string>();
    /** common_message_id → 飞书 om_id */
    messages = new Map<string, string>();

    commonMessages = new Map<string, LarkAssistantMessageRow>();
    larkMessages = new Map<string, LarkOutboundMapping>();

    reads: string[] = [];

    /** 注入故障用。 */
    failLarkMessageInsert?: Error;
    failCommonMessageInsert?: Error;

    async atomically<T>(run: (tables: LarkOutboundTables) => Promise<T>): Promise<T> {
        const saved: Array<[Map<string, unknown>, Array<[string, unknown]>]> = [
            [this.commonMessages as Map<string, unknown>, [...this.commonMessages]],
            [this.larkMessages as Map<string, unknown>, [...this.larkMessages]],
        ];
        try {
            return await run(this);
        } catch (error) {
            for (const [table, entries] of saved) {
                table.clear();
                for (const [key, row] of entries) table.set(key, row);
            }
            throw error;
        }
    }

    async chatIdOf(commonConversationId: string): Promise<string | null> {
        this.reads.push(`chatIdOf:${commonConversationId}`);
        return this.chats.get(commonConversationId) ?? null;
    }

    async omIdOf(commonMessageId: string): Promise<string | null> {
        this.reads.push(`omIdOf:${commonMessageId}`);
        return this.messages.get(commonMessageId) ?? null;
    }

    async commonMessageIdOf(omId: string): Promise<string | null> {
        this.reads.push(`commonMessageIdOf:${omId}`);
        return this.larkMessages.get(omId)?.common_message_id ?? null;
    }

    async insertCommonMessage(row: LarkAssistantMessageRow): Promise<void> {
        if (this.failCommonMessageInsert) throw this.failCommonMessageInsert;
        if (this.commonMessages.has(row.common_message_id)) return; // or-ignore
        this.commonMessages.set(row.common_message_id, { ...row });
    }

    async insertLarkMessage(row: LarkOutboundMapping): Promise<void> {
        if (this.failLarkMessageInsert) throw this.failLarkMessageInsert;
        if (this.larkMessages.has(row.om_id)) return; // or-ignore
        this.larkMessages.set(row.om_id, { ...row });
    }
}

/**
 * 种进台账的行。
 *
 * 出站这条链只读 session_id 和 bot_name，撤回那两列（replies / safety_status）在
 * 这里一律取默认值 —— 让每个用例只写自己关心的字段，别被跟它无关的列淹掉。
 * 它们的行为由 recall.test.ts 覆盖。
 */
type LedgerSeed = Partial<LarkAgentResponseRow> & { session_id: string };

class MemoryLedger implements LarkResponseLedger {
    rows = new Map<string, LedgerSeed>();
    appended: Array<{ sessionId: string; reply: unknown }> = [];
    settled: Array<{ sessionId: string; outcome: LarkResponseOutcome }> = [];
    /** 出站一次都不该碰安全终态：碰了就等于覆盖掉安全判定的结论。 */
    safetySettled: string[] = [];
    failFind?: Error;
    failSettle?: Error;

    async find(sessionId: string): Promise<LarkAgentResponseRow | null> {
        if (this.failFind) throw this.failFind;
        const row = this.rows.get(sessionId);
        return row ? { replies: [], safety_status: 'pending', ...row } : null;
    }

    async appendReply(sessionId: string, reply: unknown): Promise<void> {
        this.appended.push({ sessionId, reply });
    }

    async settle(sessionId: string, outcome: LarkResponseOutcome): Promise<void> {
        if (this.failSettle) throw this.failSettle;
        this.settled.push({ sessionId, outcome });
    }

    async settleSafety(sessionId: string): Promise<void> {
        this.safetySettled.push(sessionId);
    }
}

interface ApiSpy {
    // 发消息那条链只声明它真的会打的两个方法（见 deliver.ts 的 LarkDeliveryDeps.api），
    // 所以替身也只实现这两个 —— 端口再扩容也不会拖着这份测试一起改。
    api: LarkDeliveryDeps['api'];
    sent: Array<{ chatId: string; content: PostContent }>;
    replied: Array<{ messageId: string; content: PostContent; inThread: boolean }>;
    /** 下一次发送飞书返回的 message_id。undefined = 平台没给。 */
    nextMessageId: string | undefined;
    fail?: Error;
}

function apiSpy(nextMessageId: string | undefined = 'om_sent'): ApiSpy {
    const spy: ApiSpy = {
        sent: [],
        replied: [],
        nextMessageId,
        api: {
            async sendPost(chatId, content): Promise<LarkSentMessage> {
                if (spy.fail) throw spy.fail;
                spy.sent.push({ chatId, content });
                return { messageId: spy.nextMessageId };
            },
            async replyPost(messageId, content, inThread): Promise<LarkSentMessage> {
                if (spy.fail) throw spy.fail;
                spy.replied.push({ messageId, content, inThread });
                return { messageId: spy.nextMessageId };
            },
        },
    };
    return spy;
}

interface Harness {
    deps: LarkDeliveryDeps;
    store: MemoryOutboundTables;
    ledger: MemoryLedger;
    api: ApiSpy;
    rendered: Array<{ markdown: string; ctx: LarkRenderContext }>;
    spoke: Array<{ botName: string; lane?: string }>;
    waited: number[];
    observed: Array<{ stage: string; seconds: number }>;
    mintedIds: string[];
}

const NOW = 1_700_000_000_000;

function harness(overrides: Partial<LarkDeliveryDeps> = {}): Harness {
    const store = new MemoryOutboundTables();
    const ledger = new MemoryLedger();
    const api = apiSpy();
    const rendered: Array<{ markdown: string; ctx: LarkRenderContext }> = [];
    const spoke: Array<{ botName: string; lane?: string }> = [];
    const waited: number[] = [];
    const observed: Array<{ stage: string; seconds: number }> = [];
    const mintedIds: string[] = [];
    let seq = 0;

    const deps: LarkDeliveryDeps = {
        store,
        ledger,
        api: api.api,
        render: async (markdown, ctx) => {
            rendered.push({ markdown, ctx });
            return [[{ tag: 'text', text: markdown }]] as unknown as PostContent;
        },
        botCommonUserId: (botName) => `cu_${botName}`,
        botDisplayName: (botName) => `名字_${botName}`,
        newCommonId: () => {
            const id = `cm_new_${(seq += 1)}`;
            mintedIds.push(id);
            return id;
        },
        now: () => NOW,
        wait: async (ms) => void waited.push(ms),
        speakAs: async (who, say) => {
            spoke.push(who);
            await say();
        },
        observe: (stage, seconds) => void observed.push({ stage, seconds }),
        ...overrides,
    };

    return { deps, store, ledger, api, rendered, spoke, waited, observed, mintedIds };
}

/** 被动回复的基线 payload：群聊、第一段、收尾。 */
function reply(overrides: Partial<LarkChatResponse> = {}): LarkChatResponse {
    return {
        channel: 'lark',
        session_id: 'sess-1',
        message_id: 'cm_trigger',
        chat_id: 'cc_group',
        is_p2p: false,
        root_id: null,
        content: '在的',
        full_content: '在的',
        status: 'success',
        part_index: 0,
        is_last: true,
        is_proactive: false,
        bot_name: 'chiwei',
        ...overrides,
    };
}

function proactive(overrides: Partial<LarkChatResponse> = {}): LarkChatResponse {
    return {
        channel: 'lark',
        session_id: null,
        message_id: 'proactive:550e8400-e29b-41d4-a716-446655440000',
        chat_id: 'cc_dm',
        is_p2p: true,
        root_id: null,
        content: '刚做完饭',
        status: 'success',
        part_index: 0,
        is_last: true,
        is_proactive: true,
        bot_name: 'chiwei',
        ...overrides,
    };
}

/** 反查要用到的映射：触发消息、会话。 */
function seedRefs(store: MemoryOutboundTables): void {
    store.messages.set('cm_trigger', 'om_trigger');
    store.chats.set('cc_group', 'oc_group');
    store.chats.set('cc_dm', 'oc_dm');
}

// ---------------------------------------------------------------------------
// 三种发送分支
// ---------------------------------------------------------------------------

describe('发送分支 — part 0 的被动回复', () => {
    it('回复触发消息本身，且不进话题串', async () => {
        const h = harness();
        seedRefs(h.store);
        h.ledger.rows.set('sess-1', { session_id: 'sess-1', bot_name: 'chiwei' });

        await deliverLarkChatResponse(h.deps, reply());

        expect(h.api.replied).toHaveLength(1);
        expect(h.api.replied[0]!.messageId).toBe('om_trigger');
        // inThread 必须显式 false：普通聊天的回复被挂进话题串，用户看到的是一个
        // 折叠起来的分支，等于没回。
        expect(h.api.replied[0]!.inThread).toBe(false);
        expect(h.api.sent).toHaveLength(0);
    });

    it('群聊渲染带 mention 会话 id，私聊不带', async () => {
        const group = harness();
        seedRefs(group.store);
        await deliverLarkChatResponse(group.deps, reply({ is_p2p: false }));
        expect(group.rendered[0]!.ctx.mentionChatId).toBe('oc_group');

        const dm = harness();
        seedRefs(dm.store);
        dm.store.messages.set('cm_trigger', 'om_trigger');
        await deliverLarkChatResponse(dm.deps, reply({ is_p2p: true, chat_id: 'cc_dm' }));
        // 私聊里没有第三个人，@ 谁都渲染不成 mention，查一次群成员纯属白花一次查询。
        expect(dm.rendered[0]!.ctx.mentionChatId).toBeUndefined();
    });

    it('把这一段带的图片句柄原样交给渲染，一个都不改', async () => {
        // 队列里传的是对象存储的永久句柄（签名只活 1.5 小时，现签在渲染那一步）。
        // 这里动过它的话，症状是图签不出来 —— 而那一路的失败是降级，不会红。
        const h = harness();
        seedRefs(h.store);

        await deliverLarkChatResponse(
            h.deps,
            reply({ picture_file_names: ['pictures/a.png', 'pictures/b.png'] }),
        );

        expect(h.rendered[0]!.ctx.pictureFileNames).toEqual([
            'pictures/a.png',
            'pictures/b.png',
        ]);
    });

    it('老消息没有 picture_file_names：渲染那一步一个句柄都收不到', async () => {
        // DLQ 里躺着的、旧版 agent-service 发的消息就是这种。缺字段 = 这一段不带图，
        // 照常发正文。
        const h = harness();
        seedRefs(h.store);

        await deliverLarkChatResponse(h.deps, reply());

        expect(h.rendered[0]!.ctx.pictureFileNames).toBeUndefined();
        expect(h.api.replied).toHaveLength(1);
    });

    it('第一段之前不等待', async () => {
        const h = harness();
        seedRefs(h.store);
        await deliverLarkChatResponse(h.deps, reply({ part_index: 0 }));
        expect(h.waited).toEqual([]);
    });
});

describe('发送分支 — part > 0 的续段', () => {
    it('新发到会话而不是回复，且发之前先等一段固定间隔', async () => {
        const h = harness();
        seedRefs(h.store);

        await deliverLarkChatResponse(h.deps, reply({ part_index: 1, is_last: false }));

        expect(h.waited).toEqual([2_500]);
        expect(h.api.sent).toHaveLength(1);
        expect(h.api.sent[0]!.chatId).toBe('oc_group');
        expect(h.api.replied).toHaveLength(0);
    });

    it('等待发生在发送之前，不是之后', async () => {
        const order: string[] = [];
        const h = harness();
        seedRefs(h.store);
        h.deps.wait = async () => void order.push('wait');
        const original = h.deps.api.sendPost;
        h.deps.api = {
            ...h.deps.api,
            sendPost: async (chatId, content) => {
                order.push('send');
                return original.call(h.deps.api, chatId, content);
            },
        };

        await deliverLarkChatResponse(h.deps, reply({ part_index: 2, is_last: false }));

        expect(order).toEqual(['wait', 'send']);
    });
});

describe('发送分支 — 主动发', () => {
    it('新发到会话，且绝不拿伪 message_id 去反查来源消息', async () => {
        const h = harness();
        seedRefs(h.store);

        await deliverLarkChatResponse(h.deps, proactive());

        expect(h.api.sent).toHaveLength(1);
        expect(h.api.sent[0]!.chatId).toBe('oc_dm');
        expect(h.api.replied).toHaveLength(0);
        // 伪 id 反查必 miss、必抛。主动发这条路一次都不该碰它。
        expect(h.store.reads.filter((r) => r.startsWith('omIdOf:'))).toEqual([]);
    });

    it('带了 root_id 也照样新发 —— root 被刻意忽略', async () => {
        const h = harness();
        seedRefs(h.store);
        h.store.messages.set('cm_root', 'om_root');

        await deliverLarkChatResponse(h.deps, proactive({ root_id: 'cm_root' }));

        // 主动发是赤尾自己开口，本就该是一条新消息。root_id 偶然带了值也不该让它
        // 退化成一条回复。
        expect(h.api.sent).toHaveLength(1);
        expect(h.api.replied).toHaveLength(0);
        expect(h.store.reads.filter((r) => r.startsWith('omIdOf:'))).toEqual([]);
    });

    it('台账一个字都不写 —— 主动发没有 session_id', async () => {
        const h = harness();
        seedRefs(h.store);

        await deliverLarkChatResponse(h.deps, proactive());

        expect(h.ledger.appended).toEqual([]);
        expect(h.ledger.settled).toEqual([]);
    });
});

// ---------------------------------------------------------------------------
// 反查
// ---------------------------------------------------------------------------

describe('反查 — 查不到就炸，绝不静默发到别处', () => {
    it('触发消息没有飞书映射：不发、不落库', async () => {
        const h = harness();
        h.store.chats.set('cc_group', 'oc_group');
        h.ledger.rows.set('sess-1', { session_id: 'sess-1' });

        await deliverLarkChatResponse(h.deps, reply());

        expect(h.api.replied).toHaveLength(0);
        expect(h.store.commonMessages.size).toBe(0);
        expect(h.ledger.settled).toEqual([
            { sessionId: 'sess-1', outcome: { status: 'failed' } },
        ]);
    });

    it('会话没有飞书映射：不发、不落库', async () => {
        const h = harness();
        h.store.messages.set('cm_trigger', 'om_trigger');

        await deliverLarkChatResponse(h.deps, reply());

        expect(h.api.replied).toHaveLength(0);
        expect(h.store.commonMessages.size).toBe(0);
    });

    it('主动发的会话没有映射：不发', async () => {
        const h = harness();

        await deliverLarkChatResponse(h.deps, proactive());

        expect(h.api.sent).toHaveLength(0);
    });

    it('root 有值但查不到映射：整条回复不发', async () => {
        const h = harness();
        seedRefs(h.store);

        await deliverLarkChatResponse(h.deps, reply({ root_id: 'cm_missing_root' }));

        expect(h.api.replied).toHaveLength(0);
    });
});

// ---------------------------------------------------------------------------
// 落库
// ---------------------------------------------------------------------------

describe('落库 — assistant 行的字段口径', () => {
    it('被动回复：root/reply 都挂在触发消息上', async () => {
        const h = harness();
        seedRefs(h.store);

        await deliverLarkChatResponse(h.deps, reply());

        const row = [...h.store.commonMessages.values()][0]!;
        expect(row).toEqual({
            common_message_id: 'cm_new_1',
            channel: 'lark',
            common_conversation_id: 'cc_group',
            common_user_id: 'cu_chiwei',
            sender_display_name: '名字_chiwei',
            role: 'assistant',
            content: [{ kind: 'text', text: '在的' }],
            content_text: '在的',
            common_root_message_id: 'cm_trigger',
            common_reply_message_id: 'cm_trigger',
            scope: 'group',
            message_type: 'post',
            bot_name: 'chiwei',
            event_time: String(NOW),
            response_id: 'sess-1',
            // 被动回复不是任何一次"她自己开口"的产物。
            agent_outbound_id: undefined,
        });
        expect(h.store.larkMessages.get('om_sent')).toEqual({
            om_id: 'om_sent',
            common_message_id: 'cm_new_1',
            chat_id: 'oc_group',
            message_type: 'post',
        });
    });

    it('有 root_id 时 root 挂 root、reply 仍挂触发消息', async () => {
        const h = harness();
        seedRefs(h.store);
        h.store.messages.set('cm_root', 'om_root');

        await deliverLarkChatResponse(h.deps, reply({ root_id: 'cm_root' }));

        const row = [...h.store.commonMessages.values()][0]!;
        expect(row.common_root_message_id).toBe('cm_root');
        expect(row.common_reply_message_id).toBe('cm_trigger');
    });

    it('主动发：root 回落成自己、reply 留空 —— 伪 id 绝不进公共层引用链', async () => {
        const h = harness();
        seedRefs(h.store);

        await deliverLarkChatResponse(h.deps, proactive());

        const row = [...h.store.commonMessages.values()][0]!;
        expect(row.common_root_message_id).toBe(row.common_message_id);
        expect(row.common_reply_message_id).toBeUndefined();
        // proactive: 伪 id 一次都不该出现在落库的行里。
        expect(JSON.stringify(row)).not.toContain('proactive:');
        // 没有台账行可挂。
        expect(row.response_id).toBeUndefined();
        expect(row.scope).toBe('direct');
    });

    it('主动发：记下这行是哪一次开口的产物 —— 剥掉前缀，只留 uuid', async () => {
        const h = harness();
        seedRefs(h.store);

        await deliverLarkChatResponse(h.deps, proactive());

        const row = [...h.store.commonMessages.values()][0]!;
        // 列是 uuid 类型，`proactive:` 是线格式的命名空间标记，不进列。
        expect(row.agent_outbound_id).toBe('550e8400-e29b-41d4-a716-446655440000');
    });

    it('被动回复：这一列留空 —— 它不是任何一次"她自己开口"的产物', async () => {
        const h = harness();
        seedRefs(h.store);

        await deliverLarkChatResponse(h.deps, reply());

        const row = [...h.store.commonMessages.values()][0]!;
        expect(row.agent_outbound_id).toBeUndefined();
    });

    it('主动发的续段也记同一个 id —— 一次开口切成几段，段段指回同一次', async () => {
        const h = harness();
        seedRefs(h.store);

        await deliverLarkChatResponse(h.deps, proactive({ part_index: 1, is_last: true }));

        const row = [...h.store.commonMessages.values()][0]!;
        expect(row.agent_outbound_id).toBe('550e8400-e29b-41d4-a716-446655440000');
    });

    it('伪 id 形状不对：留空，但消息照发 —— 这里在飞书 API 之后，抛错等于真人收两条', async () => {
        // 形状不对的几种：前缀在但后半截不是 uuid、整串就没有前缀、空串。
        for (const messageId of [
            'proactive:not-a-uuid',
            'proactive:',
            '550e8400-e29b-41d4-a716-446655440000',
            '',
        ]) {
            const h = harness();
            seedRefs(h.store);

            await deliverLarkChatResponse(h.deps, proactive({ message_id: messageId }));

            // 发出去了，而且落了库 —— 记不下"是哪次开口"不该让真人收不到这句话。
            expect(h.api.sent).toHaveLength(1);
            const row = [...h.store.commonMessages.values()][0]!;
            expect(row.agent_outbound_id).toBeUndefined();
            // 伪 id 一个字都不许进这一行。
            expect(JSON.stringify(row)).not.toContain('proactive:');
        }
    });

    it('大写 uuid 照收，落库统一成小写 —— pg 的 uuid 本来就不分大小写', async () => {
        const h = harness();
        seedRefs(h.store);

        await deliverLarkChatResponse(
            h.deps,
            proactive({ message_id: 'proactive:550E8400-E29B-41D4-A716-446655440000' }),
        );

        const row = [...h.store.commonMessages.values()][0]!;
        expect(row.agent_outbound_id).toBe('550e8400-e29b-41d4-a716-446655440000');
    });

    it('同一个 om_id 已经落过库：复用旧的 common_message_id，不铸新的', async () => {
        const h = harness();
        seedRefs(h.store);
        h.ledger.rows.set('sess-1', { session_id: 'sess-1' });
        h.store.larkMessages.set('om_sent', {
            om_id: 'om_sent',
            common_message_id: 'cm_already',
            chat_id: 'oc_group',
            message_type: 'post',
        });

        await deliverLarkChatResponse(h.deps, reply());

        expect(h.mintedIds).toEqual([]);
        expect(h.ledger.appended[0]!.reply).toMatchObject({
            common_message_id: 'cm_already',
        });
    });
});

describe('落库 — 平台没返回 message_id', () => {
    it('被动回复：合成 `{触发消息 om_id}_part{段序}` 落库', async () => {
        const h = harness();
        seedRefs(h.store);
        h.api.nextMessageId = undefined;

        await deliverLarkChatResponse(h.deps, reply({ part_index: 0 }));

        expect([...h.store.larkMessages.keys()]).toEqual(['om_trigger_part0']);
    });

    it('空串也算没返回', async () => {
        const h = harness();
        seedRefs(h.store);
        h.api.nextMessageId = '';

        await deliverLarkChatResponse(h.deps, reply({ part_index: 1, is_last: false }));

        expect([...h.store.larkMessages.keys()]).toEqual(['om_trigger_part1']);
    });

    it('主动发：合成 `{这次开口的 id}_part{段序}` —— 没有来源消息可当锚点，用开口本身', async () => {
        const h = harness();
        seedRefs(h.store);
        h.api.nextMessageId = undefined;

        await deliverLarkChatResponse(h.deps, proactive());

        expect([...h.store.larkMessages.keys()]).toEqual([
            '550e8400-e29b-41d4-a716-446655440000_part0',
        ]);
    });

    it('同一个会话里连着两次"发成功但没返回 id"的主动发：各落各的行，各带自己的开口 id', async () => {
        // 合成键只由会话和段序决定的话，这两次算出来的键一模一样：第二次会反查到
        // 第一次的 common_message_id 并复用它，而两条 insert 都是 or-ignore ——
        // 第二句话在公共层根本没有行，那次开口永久停在"未落地"，全程零报错。
        const h = harness();
        seedRefs(h.store);
        h.api.nextMessageId = undefined;

        await deliverLarkChatResponse(
            h.deps,
            proactive({
                message_id: 'proactive:11111111-1111-4111-8111-111111111111',
                content: '刚做完饭',
            }),
        );
        await deliverLarkChatResponse(
            h.deps,
            proactive({
                message_id: 'proactive:22222222-2222-4222-8222-222222222222',
                content: '锅还泡着',
            }),
        );

        expect(h.api.sent).toHaveLength(2);
        const rows = [...h.store.commonMessages.values()];
        expect(rows.map((row) => row.content_text)).toEqual(['刚做完饭', '锅还泡着']);
        expect(rows.map((row) => row.agent_outbound_id)).toEqual([
            '11111111-1111-4111-8111-111111111111',
            '22222222-2222-4222-8222-222222222222',
        ]);
        // 两条飞书映射也各占一个键，没有一条被 or-ignore 吃掉。
        expect([...h.store.larkMessages.keys()]).toEqual([
            '11111111-1111-4111-8111-111111111111_part0',
            '22222222-2222-4222-8222-222222222222_part0',
        ]);
    });

    it('同一次开口的两段：段序把它们分开，谁也不吃掉谁', async () => {
        const h = harness();
        seedRefs(h.store);
        h.api.nextMessageId = undefined;

        await deliverLarkChatResponse(h.deps, proactive({ part_index: 0, is_last: false }));
        await deliverLarkChatResponse(h.deps, proactive({ part_index: 1, is_last: true }));

        expect([...h.store.larkMessages.keys()]).toEqual([
            '550e8400-e29b-41d4-a716-446655440000_part0',
            '550e8400-e29b-41d4-a716-446655440000_part1',
        ]);
        expect(h.store.commonMessages.size).toBe(2);
    });
});

describe('落库 — 两条 insert 同生共死', () => {
    it('lark_message 插失败时 common_message 不留痕', async () => {
        const h = harness();
        seedRefs(h.store);
        h.store.failLarkMessageInsert = new Error('mapping insert exploded');

        await deliverLarkChatResponse(h.deps, reply());

        // 只写了 common_message 就是一条公共层有、飞书侧无对应物的孤儿记录，
        // 之后按 om_id 反查它的路径（撤回、引用回复）全部读空。
        expect(h.store.commonMessages.size).toBe(0);
        expect(h.store.larkMessages.size).toBe(0);
    });

    it('落库炸了不影响"消息已经发出去了"这个事实，但台账要记失败', async () => {
        const h = harness();
        seedRefs(h.store);
        h.ledger.rows.set('sess-1', { session_id: 'sess-1' });
        h.store.failLarkMessageInsert = new Error('mapping insert exploded');

        await deliverLarkChatResponse(h.deps, reply());

        expect(h.api.replied).toHaveLength(1);
        expect(h.ledger.settled).toEqual([
            { sessionId: 'sess-1', outcome: { status: 'failed' } },
        ]);
        expect(h.ledger.appended).toEqual([]);
    });
});

// ---------------------------------------------------------------------------
// 台账三处写
// ---------------------------------------------------------------------------

describe('台账 — replies 追加', () => {
    it('每发出一段就追加一条，指向刚落库的 assistant 行', async () => {
        const h = harness();
        seedRefs(h.store);
        h.ledger.rows.set('sess-1', { session_id: 'sess-1' });

        await deliverLarkChatResponse(h.deps, reply({ part_index: 0, is_last: false }));

        expect(h.ledger.appended).toEqual([
            {
                sessionId: 'sess-1',
                reply: {
                    common_message_id: 'cm_new_1',
                    content_type: 'post',
                    sent_at: new Date(NOW).toISOString(),
                },
            },
        ]);
    });

    it('台账行不存在时不追加 —— 没有行可以拼', async () => {
        const h = harness();
        seedRefs(h.store);
        // session_id 有值但库里没这一行（agent-service 那侧还没 INSERT / 已被清理）。

        await deliverLarkChatResponse(h.deps, reply());

        expect(h.ledger.appended).toEqual([]);
        expect(h.ledger.settled).toEqual([]);
        // 但消息照发、照落库：台账缺行不该让真人收不到回复。
        expect(h.api.replied).toHaveLength(1);
        expect(h.store.commonMessages.size).toBe(1);
    });
});

describe('台账 — 终态', () => {
    it('收尾那一段写全文 + completed', async () => {
        const h = harness();
        seedRefs(h.store);
        h.ledger.rows.set('sess-1', { session_id: 'sess-1' });

        await deliverLarkChatResponse(
            h.deps,
            reply({ is_last: true, content: '第二段', full_content: '第一段第二段' }),
        );

        expect(h.ledger.settled).toEqual([
            {
                sessionId: 'sess-1',
                outcome: { status: 'completed', responseText: '第一段第二段' },
            },
        ]);
    });

    it('没有 full_content 时退回本段正文', async () => {
        const h = harness();
        seedRefs(h.store);
        h.ledger.rows.set('sess-1', { session_id: 'sess-1' });

        await deliverLarkChatResponse(
            h.deps,
            reply({ is_last: true, content: '就这一段', full_content: undefined }),
        );

        expect(h.ledger.settled[0]!.outcome.responseText).toBe('就这一段');
    });

    it('出站一次都不碰安全终态 —— 那两列归安全判定和撤回链路', async () => {
        // 写入矩阵里 safety_status / safety_result 是 agent-service 与撤回链路双向写的
        // 那一对，而这张表没有 channel 列。出站顺手写一次，覆盖掉的是别人的结论。
        const h = harness();
        seedRefs(h.store);
        h.ledger.rows.set('sess-1', { session_id: 'sess-1' });

        await deliverLarkChatResponse(h.deps, reply({ is_last: true }));

        expect(h.ledger.safetySettled).toEqual([]);
    });

    it('不是收尾就不落终态', async () => {
        const h = harness();
        seedRefs(h.store);
        h.ledger.rows.set('sess-1', { session_id: 'sess-1' });

        await deliverLarkChatResponse(h.deps, reply({ is_last: false }));

        expect(h.ledger.settled).toEqual([]);
    });

    it('agent 自己就报了失败：记 failed，一条都不发', async () => {
        const h = harness();
        seedRefs(h.store);
        h.ledger.rows.set('sess-1', { session_id: 'sess-1' });

        await deliverLarkChatResponse(
            h.deps,
            reply({ status: 'failed', error: '模型超时' }),
        );

        expect(h.api.replied).toHaveLength(0);
        expect(h.api.sent).toHaveLength(0);
        expect(h.store.commonMessages.size).toBe(0);
        expect(h.ledger.settled).toEqual([
            { sessionId: 'sess-1', outcome: { status: 'failed' } },
        ]);
    });

    it('内容为空且是收尾：只落 completed，**不碰 response_text**', async () => {
        const h = harness();
        seedRefs(h.store);
        h.ledger.rows.set('sess-1', { session_id: 'sess-1' });

        await deliverLarkChatResponse(
            h.deps,
            reply({ content: '', is_last: true, full_content: '前面几段的全文' }),
        );

        expect(h.api.replied).toHaveLength(0);
        // 写 responseText 会把前面几段已经落好的全文抹掉。
        expect(h.ledger.settled).toEqual([
            { sessionId: 'sess-1', outcome: { status: 'completed' } },
        ]);
    });

    it('内容为空且不是收尾：什么都不写', async () => {
        const h = harness();
        seedRefs(h.store);
        h.ledger.rows.set('sess-1', { session_id: 'sess-1' });

        await deliverLarkChatResponse(h.deps, reply({ content: '', is_last: false }));

        expect(h.ledger.settled).toEqual([]);
        expect(h.ledger.appended).toEqual([]);
    });

    it('发送失败：记 failed，且不落库', async () => {
        const h = harness();
        seedRefs(h.store);
        h.ledger.rows.set('sess-1', { session_id: 'sess-1' });
        h.api.fail = new Error('feishu said no');

        await deliverLarkChatResponse(h.deps, reply());

        expect(h.store.commonMessages.size).toBe(0);
        expect(h.ledger.settled).toEqual([
            { sessionId: 'sess-1', outcome: { status: 'failed' } },
        ]);
    });

    it('连记 failed 都失败时不再往外抛 —— 上游只会把它变成一条 DLQ', async () => {
        const h = harness();
        seedRefs(h.store);
        h.ledger.rows.set('sess-1', { session_id: 'sess-1' });
        h.api.fail = new Error('feishu said no');
        h.ledger.failSettle = new Error('db is down too');

        await expect(deliverLarkChatResponse(h.deps, reply())).resolves.toBeUndefined();
    });
});

// ---------------------------------------------------------------------------
// 台账写失败的处置：分界线是「有没有调过飞书 API」
// ---------------------------------------------------------------------------

describe('台账写失败 — 还没调过飞书 API 的路径：往外抛', () => {
    // 这三条路径上消息根本没发出去，重投是安全的。吞掉等于把一次数据库抖动变成
    // 一条**永远停在 pending** 的台账 —— 而它本来能被重投救回来。

    it('agent 自己报了失败，落 failed 时库挂了：抛出去', async () => {
        const h = harness();
        seedRefs(h.store);
        h.ledger.rows.set('sess-1', { session_id: 'sess-1' });
        h.ledger.failSettle = new Error('pg is down');

        await expect(
            deliverLarkChatResponse(h.deps, reply({ status: 'failed', error: '模型超时' })),
        ).rejects.toThrow('pg is down');
        // 一个字都没发出去，所以重投不会让真人看到第二条。
        expect(h.api.replied).toHaveLength(0);
        expect(h.api.sent).toHaveLength(0);
    });

    it('空的收尾段，落 completed 时库挂了：抛出去', async () => {
        const h = harness();
        seedRefs(h.store);
        h.ledger.rows.set('sess-1', { session_id: 'sess-1' });
        h.ledger.failSettle = new Error('pg is down');

        await expect(
            deliverLarkChatResponse(h.deps, reply({ content: '', is_last: true })),
        ).rejects.toThrow('pg is down');
        expect(h.api.replied).toHaveLength(0);
    });

    it('反查就失败（映射查不到），落 failed 时库也挂了：抛出去', async () => {
        // 反查在发送**之前**，所以这条也在分界线的"还没调过飞书 API"那一侧 ——
        // 它和上面两条走的是不同的代码分支，分界线必须由"调没调过 API"决定，
        // 而不是由"在不在 try 块里"决定。
        const h = harness();
        h.store.chats.set('cc_group', 'oc_group'); // 缺 cm_trigger 的映射
        h.ledger.rows.set('sess-1', { session_id: 'sess-1' });
        h.ledger.failSettle = new Error('pg is down');

        await expect(deliverLarkChatResponse(h.deps, reply())).rejects.toThrow('pg is down');
        expect(h.api.replied).toHaveLength(0);
    });
});

describe('台账写失败 — 已经调过飞书 API 的路径：吞掉', () => {
    it('发出去之后落库失败、连记 failed 也失败：不抛，上游照常 ACK', async () => {
        const h = harness();
        seedRefs(h.store);
        h.ledger.rows.set('sess-1', { session_id: 'sess-1' });
        h.store.failLarkMessageInsert = new Error('mapping insert exploded');
        h.ledger.failSettle = new Error('pg is down');

        await expect(deliverLarkChatResponse(h.deps, reply())).resolves.toBeUndefined();
        // 消息**真的发出去了**：重投会让真人收到第二条，比台账停在 pending 严重。
        expect(h.api.replied).toHaveLength(1);
    });

    it('发送本身抛错时也算调过 —— 请求可能已经到了飞书，只是响应没回来', async () => {
        const h = harness();
        seedRefs(h.store);
        h.ledger.rows.set('sess-1', { session_id: 'sess-1' });
        h.api.fail = new Error('socket hang up');
        h.ledger.failSettle = new Error('pg is down');

        await expect(deliverLarkChatResponse(h.deps, reply())).resolves.toBeUndefined();
    });
});

// ---------------------------------------------------------------------------
// 同一次回答的多段并发跑：**当前行为**，不是期望行为
// ---------------------------------------------------------------------------

/**
 * 同一个 session 的几段回复会被并发处理 —— 单副本 prefetch=10 就已经会，多副本更甚。
 * 下面两条把这种情况下的**现状**钉住，好让下次有人动这块时知道自己破坏了什么。
 * 两条都不是我们想要的形态，也都不在这一批的范围里（改它们要给台账引入按段序的
 * 追加语义和单调的终态，跨服务共写同一行，是独立议题）。
 */
describe('多段并发 — 当前行为存档（不是期望行为）', () => {
    it('【现状】replies 的顺序是"谁先跑完谁在前"，不是 part_index 的顺序', async () => {
        const h = harness();
        seedRefs(h.store);
        h.ledger.rows.set('sess-1', { session_id: 'sess-1' });

        // 第一段卡在飞书 API 上，第二段一路跑完 —— 真实世界里网络抖一下就是这样。
        let releaseFirstPart!: () => void;
        const firstPartInFlight = new Promise<void>((resolve) => {
            releaseFirstPart = resolve;
        });
        h.deps.api = {
            ...h.deps.api,
            replyPost: async () => {
                await firstPartInFlight;
                return { messageId: 'om_part0' };
            },
            sendPost: async () => ({ messageId: 'om_part1' }),
        };

        const first = deliverLarkChatResponse(
            h.deps,
            reply({ part_index: 0, is_last: false, content: '第一段' }),
        );
        const second = deliverLarkChatResponse(
            h.deps,
            reply({ part_index: 1, is_last: false, content: '第二段' }),
        );
        await second;
        releaseFirstPart();
        await first;

        const contentOf = (commonMessageId: unknown): string | undefined =>
            h.store.commonMessages.get(String(commonMessageId))?.content_text;
        expect(
            h.ledger.appended.map((entry) =>
                contentOf((entry.reply as { common_message_id: string }).common_message_id),
            ),
        ).toEqual(['第二段', '第一段']);
        // 读台账的人（monitor-dashboard / rebuild）拿到的 replies 因此是乱序的。
    });

    it('【现状】终态不单调：先落的 failed 会被后落的 completed 盖掉', async () => {
        const h = harness();
        seedRefs(h.store);
        h.ledger.rows.set('sess-1', { session_id: 'sess-1' });

        // 第一段真的没发出去。
        h.api.fail = new Error('feishu said no');
        await deliverLarkChatResponse(h.deps, reply({ part_index: 0, is_last: false }));

        // 收尾那一段发成功了，于是整轮被记成 completed —— 尽管中间少了一段。
        h.api.fail = undefined;
        await deliverLarkChatResponse(h.deps, reply({ part_index: 1, is_last: true }));

        expect(h.ledger.settled.map((s) => s.outcome.status)).toEqual(['failed', 'completed']);
        // settle 是一次无条件 UPDATE（postgres-ledger.ts），没有"failed 之后不许回到
        // completed"这条守卫，所以库里最后留下的是 completed。
        expect(h.ledger.settled.at(-1)!.outcome.status).toBe('completed');
    });
});

// ---------------------------------------------------------------------------
// 谁在说话
// ---------------------------------------------------------------------------

describe('谁在说话', () => {
    it('payload 的 bot_name 优先于台账里那一列', async () => {
        const h = harness();
        seedRefs(h.store);
        h.ledger.rows.set('sess-1', { session_id: 'sess-1', bot_name: '台账里的旧值' });

        await deliverLarkChatResponse(h.deps, reply({ bot_name: 'chiwei' }));

        expect(h.spoke).toEqual([{ botName: 'chiwei', lane: undefined }]);
    });

    it('payload 没带就用台账里的', async () => {
        const h = harness();
        seedRefs(h.store);
        h.ledger.rows.set('sess-1', { session_id: 'sess-1', bot_name: '从台账来的' });

        await deliverLarkChatResponse(h.deps, reply({ bot_name: undefined }));

        expect(h.spoke).toEqual([{ botName: '从台账来的', lane: undefined }]);
    });

    it('两边都没有：一个字都不发 —— 发错 bot 比不发严重', async () => {
        const h = harness();
        seedRefs(h.store);

        await deliverLarkChatResponse(h.deps, reply({ bot_name: undefined }));

        expect(h.spoke).toEqual([]);
        expect(h.api.replied).toHaveLength(0);
        expect(h.store.commonMessages.size).toBe(0);
    });

    it('泳道随消息进上下文', async () => {
        const h = harness();
        seedRefs(h.store);

        await deliverLarkChatResponse(h.deps, reply(), 'ppe-x');

        expect(h.spoke).toEqual([{ botName: 'chiwei', lane: 'ppe-x' }]);
    });

    it('发送与落库都在"这个 bot 在说话"的上下文里 —— 客户端池按它选 bot', async () => {
        const inside: string[] = [];
        const h = harness();
        seedRefs(h.store);
        h.deps.speakAs = async (who, say) => {
            inside.push(`enter:${who.botName}`);
            await say();
            inside.push('leave');
        };
        const originalReply = h.deps.api.replyPost;
        h.deps.api = {
            ...h.deps.api,
            replyPost: async (id, content, inThread) => {
                inside.push('reply');
                return originalReply.call(h.deps.api, id, content, inThread);
            },
        };

        await deliverLarkChatResponse(h.deps, reply());

        expect(inside).toEqual(['enter:chiwei', 'reply', 'leave']);
    });
});

// ---------------------------------------------------------------------------
// 台账那次读的失败要往外走
// ---------------------------------------------------------------------------

describe('台账那次读失败', () => {
    it('往外抛 —— 由队列层决定怎么处置，这里不吞', async () => {
        const h = harness();
        seedRefs(h.store);
        h.ledger.failFind = new Error('pg is down');

        await expect(deliverLarkChatResponse(h.deps, reply())).rejects.toThrow('pg is down');
        expect(h.api.replied).toHaveLength(0);
    });
});

// ---------------------------------------------------------------------------
// 内存事务本身老不老实
// ---------------------------------------------------------------------------

describe('内存实现的事务语义（测试替身自检）', () => {
    it('run 抛错时，事务里写进去的行全部回滚', async () => {
        const store = new MemoryOutboundTables();
        store.commonMessages.set('before', {
            common_message_id: 'before',
        } as LarkAssistantMessageRow);

        await expect(
            store.atomically(async (tables) => {
                await tables.insertCommonMessage({
                    common_message_id: 'during',
                } as LarkAssistantMessageRow);
                throw new Error('boom');
            }),
        ).rejects.toThrow('boom');

        expect([...store.commonMessages.keys()]).toEqual(['before']);
    });

    it('run 正常返回时写入留下', async () => {
        const store = new MemoryOutboundTables();
        await store.atomically(async (tables) => {
            await tables.insertLarkMessage({
                om_id: 'om_1',
                common_message_id: 'cm_1',
                chat_id: 'oc_1',
                message_type: 'post',
            });
        });
        expect([...store.larkMessages.keys()]).toEqual(['om_1']);
    });
});

// ---------------------------------------------------------------------------
// 图片这一路的降级，接的是**真的渲染器**
// ---------------------------------------------------------------------------

// 上面所有用例的 render 都是替身。这一组反过来：把真的 createLarkPostRenderer 接进
// 来，只让图片那三个协作者依次失败，然后看飞书那边到底收到了什么。
//
// 判据不是"渲染没抛"，是**她那句话真的送到了飞书**：一张图发不出去不能让整条消息
// 发不出去。抛出去的结果是整条消息进重试，真人什么都收不到，而且重试大概率还是同样
// 的失败。
describe('图片每一步失败时：她那句话照常送到飞书', () => {
    function withRealRender(pictures: Partial<LarkPictureDeps> = {}): Harness {
        const h = harness();
        h.deps.render = createLarkPostRenderer({
            mentions: async (text) => text,
            pictures: {
                sign: async (fileName) => `https://tos.example/${fileName}`,
                download: async () => Buffer.from('bytes'),
                uploader: { uploadImage: async () => 'img_v3_uploaded' },
                ...pictures,
            },
        });
        seedRefs(h.store);
        return h;
    }

    const carrying = { content: '看这张', picture_file_names: ['pictures/cat.png'] };

    it('现签失败：正文照发，图降级成一行文字', async () => {
        const h = withRealRender({ sign: async () => null });

        await deliverLarkChatResponse(h.deps, reply(carrying));

        expect(h.api.replied).toHaveLength(1);
        const content = h.api.replied[0]!.content.content;
        expect(content[0]).toEqual([{ tag: 'md', text: '看这张' }]);
        expect(content.flat().some((node) => node.tag === 'img')).toBe(false);
    });

    it('下载失败：正文照发', async () => {
        const h = withRealRender({
            download: async () => {
                throw new Error('HTTP 502');
            },
        });

        await deliverLarkChatResponse(h.deps, reply(carrying));

        expect(h.api.replied).toHaveLength(1);
        expect(h.api.replied[0]!.content.content[0]).toEqual([{ tag: 'md', text: '看这张' }]);
    });

    it('上传飞书失败：正文照发', async () => {
        const h = withRealRender({ uploader: { uploadImage: async () => null } });

        await deliverLarkChatResponse(h.deps, reply(carrying));

        expect(h.api.replied).toHaveLength(1);
        expect(h.api.replied[0]!.content.content[0]).toEqual([{ tag: 'md', text: '看这张' }]);
    });

    it('三张图全挂：整条消息照样发出去，台账照样收口', async () => {
        const h = withRealRender({ sign: async () => null });
        h.ledger.rows.set('sess-1', { session_id: 'sess-1', bot_name: 'chiwei' });

        await deliverLarkChatResponse(
            h.deps,
            reply({
                content: '看这几张',
                full_content: '看这几张',
                picture_file_names: ['a.png', 'b.png', 'c.png'],
            }),
        );

        expect(h.api.replied).toHaveLength(1);
        expect(h.ledger.settled).toEqual([
            { sessionId: 'sess-1', outcome: { status: 'completed', responseText: '看这几张' } },
        ]);
    });

    it('顺利的时候：正文一行、图一行，都送到飞书', async () => {
        const h = withRealRender();

        await deliverLarkChatResponse(h.deps, reply(carrying));

        expect(h.api.replied[0]!.content.content).toEqual([
            [{ tag: 'md', text: '看这张' }],
            [{ tag: 'img', image_key: 'img_v3_uploaded' }],
        ]);
    });

    it('正文里带一个非法图片引用时，整条消息里唯一的 img 是结构化那张', async () => {
        // 飞书认不出的 image_key 会让它拒收**整条消息**。voice 模型随手写出的引用
        // 一个都不能变成 image_key。
        const h = withRealRender();

        await deliverLarkChatResponse(
            h.deps,
            reply({
                content: '先看这个 ![我编的](img_v3_totally_made_up) 再看那个',
                picture_file_names: ['pictures/real.png'],
            }),
        );

        expect(h.api.replied[0]!.content.content).toEqual([
            [{ tag: 'md', text: '先看这个' }],
            [{ tag: 'md', text: '再看那个' }],
            [{ tag: 'img', image_key: 'img_v3_uploaded' }],
        ]);
    });
});
