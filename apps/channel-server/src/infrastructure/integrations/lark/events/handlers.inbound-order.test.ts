import { describe, it, expect, mock, beforeEach, afterAll, spyOn } from 'bun:test';
import { MessageTransferer } from './factory';

// 5b 入站重排回报测试。目标顺序（决策三）：
//   resolve(契约链) → runRules(判定+副作用，不实际 publish，把待发
//   ChatTrigger 意图登记进 RuleTerminalState.pendingChatTrigger)
//   → storeMessage(无条件执行，不看 terminal kind)
//   → 若 terminal 带 pending 意图：取 setNx 锁；拿到锁才 publish
//
// 失败语义：storeMessage 抛错 → 记错误日志 + return，不 publish（fail-loud）。
//
// 全程不连真实库/MQ/Qdrant：契约链黑盒 mock 返回 ok:true；runRules 用可控
// mock 模拟引擎返回不同终态 + 是否登记 pendingChatTrigger；断言执行序
// （callOrder）+ 副作用（storeMessage 入参 / publish 调用 / setNx 调用）。

const callOrder: string[] = [];

let storeShouldThrow = false;
const storeMessageMock = mock(async (p: Record<string, unknown>) => {
    callOrder.push('storeMessage');
    capturedStorePayload = p;
    if (storeShouldThrow) throw new Error('PG insert down');
});
let capturedStorePayload: Record<string, unknown> | undefined;

// runRules mock：按场景返回不同终态。persona 场景登记 pendingChatTrigger。
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
let repeatSideEffectFired = false;
const runRulesMock = mock(async (): Promise<Terminal> => {
    callOrder.push('runRules');
    // 模拟引擎内 handler 副作用：复读场景触发 redis/飞书副作用（不依赖
    // conversation_messages）；persona 场景登记待发意图。
    if (nextTerminal.kind === 'repeat') {
        repeatSideEffectFired = true;
        return { ...nextTerminal, kind: 'responded' };
    }
    return nextTerminal;
});

let setNxReturn: 'OK' | null = 'OK';
const setNxMock = mock(async (key: string) => {
    callOrder.push(`setNx:${key}`);
    return setNxReturn;
});
const publishMock = mock(async () => {
    callOrder.push('publish');
});
// 必改2：agent_responses pending 行落库闭包，由 handlers 抢锁后才调。
const savePendingMock = mock(async () => {
    callOrder.push('savePending');
});

let currentSenderInfo: { name?: string } | undefined = { name: 'Alice' };
let isP2P = false;

function makeFakeMessage() {
    return {
        messageId: 'lark_m1',
        parentMessageId: undefined,
        messageType: 'text',
        createTime: '1700000000000',
        get senderInfo() {
            return currentSenderInfo;
        },
        allowDownloadResource: () => false,
        imageKeys: () => [],
        isP2P: () => isP2P,
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

mock.module('./event-registry', () => ({ EventHandler: () => () => undefined }));
mock.module('core/rules/engine', () => ({ runRules: runRulesMock }));
mock.module('infrastructure/integrations/memory', () => ({
    storeMessage: storeMessageMock,
}));
mock.module('@cache/redis-client', () => ({ setNx: setNxMock }));
mock.module('@plugins/lark/services/callback/fetch-photo-detail', () => ({
    fetchAndSendPhotoDetail: mock(),
}));
mock.module('@plugins/lark/services/callback/update-card', () => ({
    handleUpdatePhotoCard: mock(),
    handleUpdateDailyPhotoCard: mock(),
}));
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
// handlers 现在按 bot 的 channel 经 ChannelRegistry 取插件；契约链黑盒
// mock 已固定返回，故插件 inbound/addressing 只需类型满足、不参与判定。
// 注：bun mock.module 是进程级全局。本 stub 会泄漏到同进程其他测试，故除了
// 本文件用到的 get，还实现 has/channels，让真实注册表形状（has('lark')）的
// 断言（handlers.plugin-registration.test.ts）不被本 stub 顶掉而误失败。
mock.module('@core/registry/channel-registry', () => ({
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
    context: { getBotName: () => 'chiwei', getLane: () => undefined },
}));
mock.module('@integrations/rabbitmq', () => ({
    rabbitmqClient: { publish: publishMock },
    PROACTIVE_EVAL: 'proactive_eval',
    CHAT_REQUEST: { queue: 'chat_request', rk: 'chat.request' },
}));
// 必改1：契约链对非 @bot 群消息的真实语义是 ok:true, respond:false
// （真实 larkAddressing 给非空 reason → enforceDecision 不抛 →
// 不短路；见 inbound-pipeline.real-lark.test.ts 用真实组件钉死）。
// 故这里 chainRespond 可控，S2（非 @bot 群复读）设 false 贴近真实链路，
// 钉死 handlers.ts 只看 chain.ok（=true）就照常 runRules→storeMessage，
// respond 标志不 gate 飞书 native 链路 —— 复读+入库零变化。
let chainRespond = true;
mock.module('@core/channels/inbound-pipeline', () => ({
    runInboundContractChain: mock(async () => ({
        ok: true,
        respond: chainRespond,
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

function pending(over: Record<string, unknown> = {}) {
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
            ...over,
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

describe('handlers inbound reorder: resolve -> runRules -> storeMessage -> publish', () => {
    beforeEach(() => {
        callOrder.length = 0;
        capturedStorePayload = undefined;
        storeShouldThrow = false;
        setNxReturn = 'OK';
        chainRespond = true;
        repeatSideEffectFired = false;
        isP2P = false;
        currentSenderInfo = { name: 'Alice' };
        runRulesMock.mockClear();
        storeMessageMock.mockClear();
        setNxMock.mockClear();
        publishMock.mockClear();
        savePendingMock.mockClear();
    });

    it('S1 @bot group reply: runRules -> storeMessage -> publish, payload global ids', async () => {
        nextTerminal = {
            kind: 'responded',
            channel: 'lark',
            messageId: 'internal_msg_1',
            chatId: 'internal_chat_1',
            userId: 'internal_user_42',
            skipped: [],
            pendingChatTrigger: pending(),
        };
        await run();
        expect(callOrder).toEqual([
            'runRules',
            'storeMessage',
            'setNx:make_reply:internal_msg_1',
            'savePending',
            'publish',
        ]);
        expect(capturedStorePayload!.message_id).toBe('internal_msg_1');
        expect(capturedStorePayload!.user_id).toBe('internal_user_42');
    });

    it('S2 non-@bot group (real chain: ok:true respond:false): repeat fires, storeMessage still runs, NO publish', async () => {
        // 必改1：贴近真实契约链对非 @bot 群消息的返回 —— ok:true 但
        // respond:false（真实 larkAddressing 给非空 reason，
        // enforceDecision 不抛、不短路；见 inbound-pipeline.real-lark
        // .test.ts）。钉死 handlers.ts 只看 chain.ok（=true）就照常
        // runRules→storeMessage，respond=false 不 gate 飞书 native 链路
        // （复读 + 入库零变化），消除原 mock 固定 respond:true 的盲区。
        chainRespond = false;
        nextTerminal = {
            kind: 'repeat',
            channel: 'lark',
            messageId: 'internal_msg_1',
            chatId: 'internal_chat_1',
            userId: 'internal_user_42',
            skipped: [],
        };
        await run();
        expect(repeatSideEffectFired).toBe(true);
        expect(callOrder).toEqual(['runRules', 'storeMessage']);
        expect(publishMock).not.toHaveBeenCalled();
        expect(setNxMock).not.toHaveBeenCalled();
        expect(capturedStorePayload).toBeDefined();
    });

    it('S3 blacklisted: terminal blocked -> storeMessage still runs, NO publish', async () => {
        nextTerminal = {
            kind: 'blocked',
            channel: 'lark',
            messageId: 'internal_msg_1',
            chatId: 'internal_chat_1',
            userId: 'internal_user_42',
            skipped: [],
        };
        await run();
        expect(callOrder).toEqual(['runRules', 'storeMessage']);
        expect(publishMock).not.toHaveBeenCalled();
        expect(capturedStorePayload).toBeDefined();
    });

    it('S4 persona handler error: handler_error -> storeMessage ran, NO pending so NO publish', async () => {
        nextTerminal = {
            kind: 'handler_error',
            channel: 'lark',
            messageId: 'internal_msg_1',
            chatId: 'internal_chat_1',
            userId: 'internal_user_42',
            skipped: [],
        };
        await run();
        expect(callOrder).toEqual(['runRules', 'storeMessage']);
        expect(publishMock).not.toHaveBeenCalled();
    });

    it('S5 p2p private chat: persona hit -> same as S1, payload is_p2p=true', async () => {
        isP2P = true;
        nextTerminal = {
            kind: 'responded',
            channel: 'lark',
            messageId: 'internal_msg_1',
            chatId: 'internal_chat_1',
            userId: 'internal_user_42',
            skipped: [],
            pendingChatTrigger: pending({ is_p2p: true }),
        };
        await run();
        expect(callOrder).toEqual([
            'runRules',
            'storeMessage',
            'setNx:make_reply:internal_msg_1',
            'savePending',
            'publish',
        ]);
        const args = publishMock.mock.calls[0] as unknown[];
        expect((args[1] as Record<string, unknown>).is_p2p).toBe(true);
    });

    it('S6 storeMessage throws -> NO publish, error logged, returns (fail-loud)', async () => {
        storeShouldThrow = true;
        const errSpy = spyOn(console, 'error');
        nextTerminal = {
            kind: 'responded',
            channel: 'lark',
            messageId: 'internal_msg_1',
            chatId: 'internal_chat_1',
            userId: 'internal_user_42',
            skipped: [],
            pendingChatTrigger: pending(),
        };
        await run();
        expect(callOrder).toEqual(['runRules', 'storeMessage']);
        expect(publishMock).not.toHaveBeenCalled();
        expect(setNxMock).not.toHaveBeenCalled();
        // 必改2：storeMessage 失败 → 没拿锁 → pending 行也绝不落库
        // （storeMessage 抛错前 runRules 阶段不再 save pending）。
        expect(savePendingMock).not.toHaveBeenCalled();
        expect(
            errSpy.mock.calls.some((c) =>
                String(c[0]).includes('storeMessage'),
            ),
        ).toBe(true);
        errSpy.mockRestore();
    });

    it('S1b dedupe lock lost -> storeMessage ran but NO publish (other bot won)', async () => {
        setNxReturn = null;
        nextTerminal = {
            kind: 'responded',
            channel: 'lark',
            messageId: 'internal_msg_1',
            chatId: 'internal_chat_1',
            userId: 'internal_user_42',
            skipped: [],
            pendingChatTrigger: pending(),
        };
        await run();
        expect(callOrder).toEqual([
            'runRules',
            'storeMessage',
            'setNx:make_reply:internal_msg_1',
        ]);
        expect(publishMock).not.toHaveBeenCalled();
        // 必改2 回归核心：未抢到锁的 bot 既不 publish 也不落 pending 行
        // （重排前 setNx 在 pending save 之前、未抢锁者直接 return，本次
        // 重排须保持该语义，否则留永不完成的孤儿 pending 行）。
        expect(savePendingMock).not.toHaveBeenCalled();
    });
});
