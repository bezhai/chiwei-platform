// Run against an explicitly supplied disposable PostgreSQL database.
import { afterAll, beforeAll, beforeEach, describe, expect, it } from 'bun:test';
import { DataSource } from 'typeorm';
import { BotConfig, CommonMessage, CommonUser } from '@inner/shared/entities';
import { context } from '@inner/shared/middleware';
import { LARK_SERVICE_ENTITIES } from '../ormconfig';
import { loadLarkIdentityBots } from './bot-identities';
import { createLarkBotLookup } from './bot-lookup';
import { readLarkMessageEvent } from './message/read-message-event';
import type { LarkBotLookup } from './message/mentions';
import type { LarkMessageEvent } from './message/wire';
import { projectLarkInbound, type LarkInboundDeps } from './projection/inbound-projection';
import { postgresLarkTables } from './projection/postgres-tables';
import { deliverLarkChatResponse, type LarkDeliveryDeps } from './outbound/deliver';
import { postgresLarkOutboundTables } from './outbound/postgres-tables';

const url = process.env.LARK_TEST_DATABASE_URL;
const botId = '11111111-1111-4111-8111-111111111111';
const mentionedId = '22222222-2222-4222-8222-222222222222';
const outboundId = '33333333-3333-4333-8333-333333333333';
const bots: LarkBotLookup = {
    byAppId: () => null,
    byUnionId: (id) => id === 'on_chiwei'
        ? { botName: 'chiwei', botRole: 'persona', displayName: '赤尾', commonUserId: botId }
        : id === 'on_ayana'
            ? { botName: 'ayana', botRole: 'persona', displayName: '绫奈', commonUserId: mentionedId }
            : null,
};
function payload(receiver: string): LarkMessageEvent {
    return {
        app_id: `cli_${receiver}`,
        sender: { sender_type: 'bot', sender_id: { union_id: 'on_chiwei', open_id: `ou_${receiver}_chiwei` } },
        message: {
            message_id: 'om_echo', chat_id: 'oc_test', chat_type: 'group',
            create_time: '1700000000000', message_type: 'text', content: '{"text":"hi @_user_1"}',
            mentions: [{ key: '@_user_1', id: { union_id: 'on_ayana' }, name: '绫奈' }],
        },
    };
}

describe.skipIf(!url)('inbound/outbound deduplication on real PostgreSQL', () => {
    let db: DataSource;
    let inbound: LarkInboundDeps;
    let delivery: LarkDeliveryDeps;
    let conversationId: string;
    beforeAll(async () => {
        db = new DataSource({ type: 'postgres', url, entities: [...LARK_SERVICE_ENTITIES], synchronize: true });
        await db.initialize();
    });
    afterAll(async () => { await db?.destroy(); });
    beforeEach(async () => {
        await db.query('TRUNCATE bot_config, common_message, lark_message, lark_base_chat_info, common_conversation, common_bot_presence, lark_user_open_id, common_user CASCADE');
        await db.getRepository(CommonUser).insert([
            { common_user_id: botId, channel: 'lark', display_name: '赤尾' },
            { common_user_id: mentionedId, channel: 'lark', display_name: '绫奈' },
        ]);
        const store = postgresLarkTables(db);
        conversationId = Bun.randomUUIDv7();
        await store.saveCommonConversation({ common_conversation_id: conversationId, channel: 'lark', scope: 'group',
            is_active: true, attachment_policy: { download_allowed: true, source: 'lark' } });
        await store.claimCommonConversationId({ chat_id: 'oc_test', chat_mode: 'group' }, conversationId);
        inbound = { store, refreshDirectory: async () => false, newCommonId: () => Bun.randomUUIDv7(),
            appIdOfBot: (name) => `cli_${name}`, currentLane: 'prod', laneDispatchEnabled: async () => false,
            laneOf: async () => 'prod', handOffToLane: async () => {}, withMessageLock: (_id, run) => run() };
        delivery = {
            store: postgresLarkOutboundTables(db),
            ledger: { find: async () => null, appendReply: async () => {}, settle: async () => {}, settleSafety: async () => {} },
            api: { sendPost: async () => ({ messageId: 'om_echo' }), replyPost: async () => ({ messageId: 'om_echo' }) },
            render: async () => ({ post: { content: [] }, pictures: [] }),
            botRole: () => 'persona',
        botCommonUserId: () => botId, botDisplayName: () => '赤尾', newCommonId: () => Bun.randomUUIDv7(),
            now: () => 1700000005000, wait: async () => {}, speakAs: async (_who, run) => run(), observe: () => {},
        };
    });
    async function receive(receiver = 'ayana', lookup: LarkBotLookup = bots, message: Partial<LarkMessageEvent['message']> = {}) {
        const event = payload(receiver);
        Object.assign(event.message, message);
        return context.run(context.createContext('test'), () => projectLarkInbound(inbound,
            readLarkMessageEvent(event, lookup)!, { type: 'im.message.receive_v1', payload: event,
                botName: receiver, receivedAt: new Date(), traceId: 'test' }));
    }
    async function send() {
        await deliverLarkChatResponse(delivery, {
            channel: 'lark', session_id: null, message_id: `proactive:${outboundId}`,
            chat_id: conversationId, is_p2p: false, root_id: null, content: 'hi @绫奈',
            status: 'success', is_proactive: true, is_last: true, bot_name: 'chiwei',
        });
    }
    async function assertOneMessage() {
        const messages = await db.getRepository(CommonMessage).find();
        expect(messages).toHaveLength(1);
        expect(messages[0]).toMatchObject({ role: 'assistant', bot_name: 'chiwei', common_user_id: botId,
            sender_display_name: '赤尾', agent_outbound_id: outboundId, mentioned_common_user_ids: [mentionedId] });
        const mappings = await db.query('SELECT common_message_id FROM lark_message');
        expect(mappings).toEqual([{ common_message_id: messages[0]!.common_message_id }]);
        expect(messages[0]!.common_root_message_id).toBe(messages[0]!.common_message_id);
        return messages[0]!;
    }
    it('initializes inactive configured identities consistently across concurrent startups', async () => {
        await db.getRepository(BotConfig).insert({ bot_name: 'chiwei', channel: 'lark',
            is_active: false, bot_role: 'persona', persona_id: 'akao',
            credentials: { app_id: 'cli_chiwei', app_secret: 'test', encrypt_key: 'test',
                verification_token: 'test', robot_union_id: 'on_chiwei' } });
        const [first, second] = await Promise.all([loadLarkIdentityBots(db), loadLarkIdentityBots(db)]);
        const reading = readLarkMessageEvent(payload('ayana'),
            createLarkBotLookup({ getAllBotConfigs: () => first }, () => '赤尾'))!;
        expect(reading.sender.kind).toBe('person');
        expect(reading.sender.bot!.commonUserId).toBe(second[0]!.common_user_id);
        expect(first[0]!.is_active).toBe(false);
        const id = first[0]!.common_user_id!;
        expect(await db.getRepository(CommonUser).findOneBy({ common_user_id: id })).not.toBeNull();
        expect((await db.getRepository(BotConfig).findOneBy({ bot_name: 'chiwei' }))!.common_user_id).toBe(id);
    });
    it.each(['utility', 'unknown'] as const)('preserves %s bot identity and classification across receivers', async (kind) => {
        const lookup: LarkBotLookup = { byAppId: () => null, byUnionId: id =>
            id === 'on_ayana' ? bots.byUnionId(id) : kind === 'utility'
                ? { botName: 'tool', botRole: 'utility', displayName: null, commonUserId: botId }
                : null };
        const first = await receive('ayana', lookup);
        const second = await receive('chinagi', lookup);
        expect(first.kind).toBe('recorded'); expect(second.kind).toBe('recorded');
        if (first.kind === 'recorded' && second.kind === 'recorded') {
            expect(first.projection.commonUserId).toBe(second.projection.commonUserId);
        }
        const rows = await db.getRepository(CommonMessage).find();
        expect(rows).toHaveLength(1);
        expect(rows[0]!.role).toBe('bot');
        expect(rows[0]!.bot_name ?? null).toBe(kind === 'utility' ? 'tool' : null);
        if (kind === 'utility') expect(rows[0]!.common_user_id).toBe(botId);
    });
    it.each([false, true])('deduplicates passive reply parts and keeps their response/root links (direct=%s)', async (direct) => {
        const trigger = Bun.randomUUIDv7();
        await db.getRepository(CommonMessage).insert({ common_message_id: trigger,
            common_conversation_id: conversationId, channel: 'lark', role: 'user', content: [],
            scope: direct ? 'direct' : 'group', event_time: '1699999999000' });
        await db.query('INSERT INTO lark_message (om_id, common_message_id, chat_id, message_type) VALUES ($1,$2,$3,$4)',
            ['om_trigger', trigger, 'oc_test', 'text']);
        for (const part of [0, 1]) {
            const omId = `om_reply_${part}`;
            await receive('ayana', bots, { message_id: omId, parent_id: 'om_trigger', root_id: 'om_trigger',
                chat_type: direct ? 'p2p' : 'group' });
            delivery.api.sendPost = async () => ({ messageId: omId });
            delivery.api.replyPost = async () => ({ messageId: omId });
            await deliverLarkChatResponse(delivery, { channel: 'lark', session_id: 'session-test',
                message_id: trigger, chat_id: conversationId, is_p2p: direct, root_id: trigger,
                content: `part ${part}`, status: 'success', part_index: part, is_last: part === 1,
                is_proactive: false, bot_name: 'chiwei' });
        }
        const replies = await db.getRepository(CommonMessage).find({ where: { response_id: 'session-test' } });
        expect(replies).toHaveLength(2);
        expect(new Set(replies.map(row => row.common_message_id)).size).toBe(2);
        for (const row of replies) {
            expect(row).toMatchObject({ role: 'assistant', bot_name: 'chiwei',
                common_root_message_id: trigger, common_reply_message_id: trigger,
                scope: direct ? 'direct' : 'group', mentioned_common_user_ids: [mentionedId] });
            expect(row.agent_outbound_id ?? null).toBeNull();
        }
        expect(await db.getRepository(CommonMessage).count()).toBe(3);
        expect((await db.query<Array<unknown>>('SELECT om_id FROM lark_message')).length).toBe(3);
    });
    it('inbound first: completes outbound metadata without moving event_time or removing recall state', async () => {
        await receive();
        const before = (await db.getRepository(CommonMessage).find())[0]!;
        const recalled = new Date('2026-09-15T00:00:00Z');
        await db.getRepository(CommonMessage).update(before.common_message_id, { recalled_at: recalled });
        await send();
        const after = await assertOneMessage();
        expect(after.event_time).toBe(before.event_time);
        expect(after.recalled_at).toEqual(recalled);
    });
    it('outbound first: multiple receivers reuse the row and fill mentions', async () => {
        await send();
        const results = await Promise.all([receive('ayana'), receive('chinagi'), receive('chiwei')]);
        const row = await assertOneMessage();
        for (const result of results) {
            expect(result.kind).toBe('recorded');
            if (result.kind === 'recorded') {
                expect(result.projection.commonMessageId).toBe(row.common_message_id);
                expect(result.projection.commonUserId).toBe(botId);
            }
        }
    });
    it('concurrent first writes serialize across connections, producing no orphan message', async () => {
        // Hold the database lock until both paths have reached their write transactions.
        const holder = db.createQueryRunner();
        await holder.connect(); await holder.startTransaction();
        await holder.query('SELECT pg_advisory_xact_lock(hashtextextended($1, 0))', ['lark-message:om_echo']);
        let entered = 0;
        let ready!: () => void;
        const bothReady = new Promise<void>(resolve => { ready = resolve; });
        const watch = <T extends { atomically: Function }>(store: T) => {
            const original = store.atomically.bind(store);
            store.atomically = (run: Function) => original(async (tx: unknown) => {
                if (++entered === 2) ready();
                return run(tx);
            });
        };
        watch(inbound.store); watch(delivery.store);
        try {
            const pending = Promise.all([receive(), send()]);
            await bothReady;
            await holder.commitTransaction();
            await pending;
            await assertOneMessage();
        } finally {
            if (holder.isTransactionActive) await holder.rollbackTransaction();
            await holder.release();
        }
    });
    it('a failed transaction releases the shared lock and leaves no partial message', async () => {
        const original = inbound.store.atomically;
        inbound.store.atomically = run => original(async tx => {
            await run(tx);
            throw new Error('rollback test');
        });
        await expect(receive()).rejects.toThrow('rollback test');
        expect(await db.getRepository(CommonMessage).count()).toBe(0);
        expect(await db.query<Array<{ om_id: string }>>('SELECT om_id FROM lark_message')).toEqual([]);
        inbound.store.atomically = original;
        await receive(); await send(); await assertOneMessage();
    });
});
