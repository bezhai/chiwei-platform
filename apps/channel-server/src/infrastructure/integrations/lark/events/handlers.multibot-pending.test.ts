import { describe, it, expect, mock, beforeEach, afterAll, spyOn } from 'bun:test';
import { MessageTransferer } from './factory';

// 必改2 回归钉死：5b 入站重排后，agent_responses pending 行的 save 必须
// 只在抢到 setNx 去重锁之后发生（与 publish 原子相邻）。
//
// 回归来源：重排前 setNx 在 makeTextReply 内、在 pending save 之前，
// 未抢锁的 bot 直接 return、不会 save pending。重排后 pending save 若仍
// 在 runRules 阶段（makeTextReply 内、早于 handlers 后移的 setNx），多
// bot 同群处理同一全局 message_id 时，每个 bot 都 save 一条 pending 行，
// 但只有抢到锁的才 publish → 未抢锁 bot 留下永不完成的 pending 行。
//
// 本测试用两个 bot 并发处理同一 internalMessageId：
//   - 第一个 setNx 返回 'OK'（抢到锁）→ save pending + publish
//   - 第二个 setNx 返回 null（锁被占）→ 既不 save pending 也不 publish
// 全程不连真实库/MQ/redis（黑盒 mock），断言 savePending 调用次数 == 1、
// publish 调用次数 == 1，且二者都发生在抢锁 bot 一侧。

const callOrder: string[] = [];

const storeMessageMock = mock(async () => {
    callOrder.push('storeMessage');
});

// pending save 的真实落点（5b 重排 + 必改2 后应只被抢锁 bot 调一次）。
const savePendingMock = mock(async () => {
    callOrder.push('savePending');
});

type Terminal = {
    kind: string;
    channel: string;
    messageId: string;
    chatId: string;
    userId: string;
    skipped: string[];
    pendingChatTrigger?: {
        payload: Record<string, unknown>;
        lane: string | undefined;
        dedupeKey: string;
        savePending: () => Promise<void>;
    };
};
let nextTerminal: Terminal;
const runRulesMock = mock(async (): Promise<Terminal> => {
    callOrder.push('runRules');
    return nextTerminal;
});

// 多 bot 去重锁：第一次抢到（OK），其后都被占（null）。
let setNxCallCount = 0;
const setNxMock = mock(async (key: string) => {
    callOrder.push(`setNx:${key}`);
    setNxCallCount += 1;
    return setNxCallCount === 1 ? 'OK' : null;
});
const publishMock = mock(async () => {
    callOrder.push('publish');
});

function makeFakeMessage() {
    return {
        messageId: 'lark_m1',
        parentMessageId: undefined,
        messageType: 'text',
        createTime: '1700000000000',
        senderInfo: { name: 'Alice' },
        allowDownloadResource: () => false,
        imageKeys: () => [],
        isP2P: () => false,
        toStorageFormat: () => '{"text":"hi"}',
        chatId: 'lark_c1',
    };
}

const transferSpy = spyOn(MessageTransferer, 'transfer').mockImplementation(
    async () => makeFakeMessage() as never,
);
afterAll(() => {
    transferSpy.mockRestore();
});

mock.module('core/rules/engine', () => ({
    runRules: runRulesMock,
    setUtilityRedirectResponder: mock(),
}));
mock.module('@core/services/ai/reply', () => ({ setChatRequestEnricher: mock() }));
mock.module('@core/services/message/resolve-mentions', () => ({
    resolveMentionsForGroup: mock(async () => []),
}));
mock.module('infrastructure/integrations/memory', () => ({
    storeMessage: storeMessageMock,
}));
mock.module('@cache/redis-client', () => ({
    setNx: setNxMock,
    hgetall: mock(async () => ({})),
    exists: mock(async () => 0),
}));
mock.module('@plugins/lark/services/callback/fetch-photo-detail', () => ({
    fetchAndSendPhotoDetail: mock(),
}));
mock.module('@plugins/lark/services/callback/update-card', () => ({
    handleUpdatePhotoCard: mock(),
    handleUpdateDailyPhotoCard: mock(),
}));
mock.module('@plugins/lark/commands', () => ({ larkCommands: [] }));
mock.module('infrastructure/dal/entities', () => ({
    LarkGroupMember: class {},
    LarkUser: class {},
    LarkBaseChatInfo: class {},
}));
mock.module('infrastructure/dal/entities/lark-user-open-id', () => ({
    LarkUserOpenId: class {},
}));
mock.module('infrastructure/dal/entities/bot-chat-presence', () => ({
    BotChatPresence: class {},
}));
mock.module('infrastructure/integrations/lark-client', () => ({
    getUserInfo: mock(async () => ({ user: {} })),
}));
mock.module('@lark-client', () => ({
    getUserInfo: mock(async () => ({ user: {} })),
    uploadImage: mock(async () => ({ image_key: 'img_key' })),
    deleteMessage: mock(async () => undefined),
    downloadResource: mock(async () => Buffer.from('')),
}));
mock.module('infrastructure/dal/repositories/repositories', () => ({
    GroupMemberRepository: { save: mock() },
    UserRepository: { save: mock() },
    LarkUserOpenIdRepository: { save: mock() },
    GroupChatInfoRepository: { increment: mock(), save: mock(), update: mock() },
    UserGroupBindingRepository: { findByUserAndChat: mock() },
}));
mock.module('@core/services/bot/bot-var', () => ({
    getBotAppId: () => 'app1',
    getBotUnionId: () => 'bot_union_1',
}));
mock.module('@core/services/bot/multi-bot-manager', () => ({
    multiBotManager: {
        getBotConfig: () => ({ bot_name: 'chiwei', channel: 'lark' }),
    },
}));
// 注：bun mock.module 是进程级全局。本 stub 会泄漏到同进程其他测试，故除了
// 本文件用到的 get，还实现 has/channels，让真实注册表形状（has('lark')）的
// 断言（handlers.plugin-registration.test.ts）不被本 stub 顶掉而误失败。
mock.module('@core/registry/channel-registry', () => ({
    registerPlugin: mock(),
    getChannelRegistry: () => ({
        has: () => true,
        channels: () => ['lark'],
        get: () => ({
            inbound: { parse: (r: unknown) => r },
            addressing: { decide: () => ({ respond: true, reason: 'x' }) },
        }),
    }),
}));
mock.module('@lark/basic/group', () => ({
    searchLarkChatInfo: mock(),
    searchLarkChatMember: mock(),
    addChatMember: mock(),
}));
mock.module('@lark/basic/message', () => ({
    sendMsg: mock(async () => undefined),
    sendSticker: mock(async () => undefined),
    replyMessage: mock(async () => undefined),
    sendPost: mock(async () => 'm_reply'),
    replyPost: mock(async () => 'm_reply'),
    sendCard: mock(async () => undefined),
    replyCard: mock(async () => undefined),
    replyImage: mock(async () => undefined),
    replyTemplate: mock(async () => undefined),
    searchGroupMessage: mock(async () => []),
}));
mock.module('@aliyun/oss', () => ({
    getOss: () => ({ getFile: mock(async () => undefined) }),
}));
mock.module('ormconfig', () => ({
    default: {
        getRepository: () => ({ upsert: mock(async () => undefined) }),
        transaction: mock(async () => undefined),
    },
}));
mock.module('@infrastructure/lane-router', () => ({
    laneRouter: { createClient: () => ({ post: mock() }) },
}));
mock.module('@middleware/context', () => ({
    context: {
        getBotName: () => 'chiwei',
        getTraceId: () => 'trace-test',
        getLane: () => undefined,
        createContext: (botName?: string, traceId?: string, lane?: string) => ({
            botName,
            traceId: traceId ?? 't',
            lane,
        }),
        run: async (_ctx: unknown, cb: () => Promise<unknown>) => cb(),
    },
}));
mock.module('@integrations/rabbitmq', () => ({
    rabbitmqClient: { publish: publishMock },
    PROACTIVE_EVAL: 'proactive_eval',
    CHAT_REQUEST: { queue: 'chat_request', rk: 'chat.request' },
    getLane: () => undefined,
}));
// 处理层泳道分流决策点。flag 默认 off → dispatched=false → 走现状入站路径，
// 本测试断言不变。mock 掉避免把 lane-router(TypeORM)/dynamic-config 真链拉进测试图。
mock.module('@integrations/inbound-lane-dispatch', () => ({
    dispatchInboundIfNeeded: async () => false,
}));
mock.module('@core/channels/inbound-pipeline', () => ({
    runInboundContractChain: mock(async () => ({
        ok: true,
        respond: true,
        globalUserId: 'internal_user_42',
        globalChatId: 'internal_chat_1',
        globalMessageId: 'internal_msg_1',
        globalRootId: undefined,
        inbound: { addressing_hints: [] },
    })),
}));
mock.module('@integrations/identity-resolver-runtime', () => ({
    getIdentityResolver: () => ({ resolve: mock(async () => 'x') }),
}));
mock.module('@plugins/lark/build-rule-message', () => ({
    buildLarkRuleMessage: mock(() => ({ channel: 'lark', internalMessageId: 'internal_msg_1' })),
}));
mock.module('@plugins/lark/lark-context-store', () => ({
    larkContextStore: { put: mock(() => {}), get: mock(() => ({})), clear: mock(() => {}) },
}));
mock.module('core/rules/rule', () => ({ setBotIdentityResolver: mock() }));

const { larkEventHandlers } = await import('./handlers');

function pending() {
    return {
        payload: {
            session_id: 's',
            channel: 'lark',
            message_id: 'internal_msg_1',
            chat_id: 'internal_chat_1',
            is_p2p: false,
            root_id: 'internal_msg_1',
            user_id: 'internal_user_42',
            bot_name: 'chiwei',
            is_canary: false,
            lane: undefined,
            enqueued_at: 0,
            mentions: [],
        },
        lane: undefined,
        dedupeKey: 'make_reply:internal_msg_1',
        savePending: savePendingMock,
    };
}

async function run() {
    await larkEventHandlers.handleMessageReceive({
        message: { message_id: 'lark_m1', message_type: 'text' },
    } as never);
}

describe('handlers multi-bot: pending save only after winning dedupe lock', () => {
    beforeEach(() => {
        callOrder.length = 0;
        setNxCallCount = 0;
        runRulesMock.mockClear();
        storeMessageMock.mockClear();
        setNxMock.mockClear();
        publishMock.mockClear();
        savePendingMock.mockClear();
        nextTerminal = {
            kind: 'responded',
            channel: 'lark',
            messageId: 'internal_msg_1',
            chatId: 'internal_chat_1',
            userId: 'internal_user_42',
            skipped: [],
            pendingChatTrigger: pending(),
        };
    });

    it('two bots process same internalMessageId: only lock winner saves pending + publishes', async () => {
        // bot A 处理（抢到锁）
        await run();
        // bot B 处理同一消息（锁已被占）
        await run();

        // savePending 只应被抢锁那一侧调一次
        expect(savePendingMock).toHaveBeenCalledTimes(1);
        expect(publishMock).toHaveBeenCalledTimes(1);

        // 抢锁 bot 的执行序：runRules -> storeMessage -> setNx -> savePending -> publish
        // 未抢锁 bot：runRules -> storeMessage -> setNx (止于此，无 savePending/publish)
        expect(callOrder).toEqual([
            'runRules',
            'storeMessage',
            'setNx:make_reply:internal_msg_1',
            'savePending',
            'publish',
            'runRules',
            'storeMessage',
            'setNx:make_reply:internal_msg_1',
        ]);
    });

    it('single bot wins lock: savePending adjacent-before publish, both after setNx', async () => {
        await run();
        expect(callOrder).toEqual([
            'runRules',
            'storeMessage',
            'setNx:make_reply:internal_msg_1',
            'savePending',
            'publish',
        ]);
        expect(savePendingMock).toHaveBeenCalledTimes(1);
        expect(publishMock).toHaveBeenCalledTimes(1);
    });
});
