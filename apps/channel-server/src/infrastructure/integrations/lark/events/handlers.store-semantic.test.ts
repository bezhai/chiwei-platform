import { describe, it, expect, mock, beforeEach, afterAll, spyOn } from 'bun:test';
import { MessageTransferer } from './factory';

// 建议2：把 storeMessage 成功语义钉成「message_id 已可回查」，并覆盖
// publish 决策在三种 storeMessage 情形下都正确。
//
// 真实 storeMessage（infrastructure/integrations/memory.ts）语义：
//   INSERT ... ON CONFLICT DO NOTHING（TypeORM `.orIgnore()`）。
//   - 正常成功：本次插入了行 → message_id 可回查 → publish 安全。
//   - ON CONFLICT 跳过：本次没插入（identifiers 为空），但冲突恰恰说明
//     该 message_id 已被另一个 bot 先插入、行已存在 → 下游
//     find_message_content(message_id) 仍查得到 → publish 安全。
//   - PG 抛错：真实实现 catch 后吞掉、返回 void（既有 fail-loud 缺陷，
//     非本次重排引入、不在本次范围扩大，见回报）；本测试沿用 S6 既有
//     口径——以 mock 抛错代表"storeMessage 未成功"，钉死 handlers.ts
//     抛错分支：NOT publish。
//
// 关键结论：handlers.ts 只在 storeMessage **抛错** 时不 publish；只要它
// 返回（无论真插入还是 ON CONFLICT 跳过），message_id 都已可回查 →
// publish 安全。本测试用 storeMessage 返回 void（正常）/ 返回 void（贴
// 近 ON CONFLICT 跳过：同样不抛、不插入新行）/ 抛错 三种情形钉死。

const callOrder: string[] = [];
let storeBehavior: 'inserted' | 'conflict_skip' | 'throw' = 'inserted';
const storeMessageMock = mock(async () => {
    callOrder.push('storeMessage');
    if (storeBehavior === 'throw') throw new Error('PG insert down');
    // inserted 与 conflict_skip 在 handlers 视角完全一致：都不抛、返回
    // void。conflict_skip 下行已被别的 bot 插入 → message_id 仍可回查。
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

const setNxMock = mock(async (key: string) => {
    callOrder.push(`setNx:${key}`);
    return 'OK';
});
const publishMock = mock(async () => {
    callOrder.push('publish');
});
const savePendingMock = mock(async () => {
    callOrder.push('savePending');
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
// context mock 补全 createContext/run：bun mock.module 进程级全局，缺这两个会
// 泄漏污染同进程里用真实 context 的测试（dispatch.test.ts 调 context.createContext）。
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

describe('建议2: storeMessage success semantic = message_id queryable -> publish decision', () => {
    beforeEach(() => {
        callOrder.length = 0;
        storeBehavior = 'inserted';
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

    it('normal insert success -> message_id queryable -> savePending + publish', async () => {
        storeBehavior = 'inserted';
        await run();
        expect(callOrder).toEqual([
            'runRules',
            'storeMessage',
            'setNx:make_reply:internal_msg_1',
            'savePending',
            'publish',
        ]);
    });

    it('ON CONFLICT DO NOTHING skip (row already inserted by another bot) -> message_id still queryable -> publish proceeds', async () => {
        // 冲突说明该 message_id 行已存在（别的 bot 先插入）→ 下游
        // find_message_content 查得到 → publish 安全。handlers 视角与
        // 正常成功一致：storeMessage 不抛、返回 void。
        storeBehavior = 'conflict_skip';
        await run();
        expect(callOrder).toEqual([
            'runRules',
            'storeMessage',
            'setNx:make_reply:internal_msg_1',
            'savePending',
            'publish',
        ]);
        expect(publishMock).toHaveBeenCalledTimes(1);
    });

    it('storeMessage throws -> message_id NOT guaranteed queryable -> NO savePending, NO publish (fail-loud)', async () => {
        storeBehavior = 'throw';
        const errSpy = spyOn(console, 'error');
        await run();
        expect(callOrder).toEqual(['runRules', 'storeMessage']);
        expect(savePendingMock).not.toHaveBeenCalled();
        expect(publishMock).not.toHaveBeenCalled();
        expect(setNxMock).not.toHaveBeenCalled();
        expect(
            errSpy.mock.calls.some((c) => String(c[0]).includes('storeMessage')),
        ).toBe(true);
        errSpy.mockRestore();
    });
});
