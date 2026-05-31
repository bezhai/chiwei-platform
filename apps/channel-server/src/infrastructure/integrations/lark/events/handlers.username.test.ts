import { describe, it, expect, mock, beforeEach, afterAll, spyOn } from 'bun:test';
import { MessageTransferer } from './factory';

// 身份全局化后读取端不再 JOIN lark_user 取显示名，改读
// conversation_messages.username 冗余列。入站路径（handlers
// handleMessageReceive → storeMessage）必须把发送者显示名一并落库，
// 否则读取端读到的永远是空。本测试钉死入站契约：
//
//   入站 user 消息 → storeMessage 收到的 username = message.senderInfo?.name
//   且没有任何 fallback（senderInfo 缺失 → username=undefined，不写脏占位）
//
// 全程不连真实库 —— 把 handlers.ts 的所有协作方（契约链黑盒、runRules、
// multiBotManager、identity resolver、laneRouter、AppDataSource、context、
// rabbitmq 等）全 mock，断言落在捕获的 storeMessage 入参上。不碰 5b 契约
// 链顺序/engine.ts/rule.ts/inbound-outbound-pipeline 源码——契约链当黑盒
// 直接 mock 返回 ok:true。

let capturedStorePayload: Record<string, unknown> | undefined;

const storeMessageMock = mock(async (p: Record<string, unknown>) => {
    capturedStorePayload = p;
});
const runRulesMock = mock(async () => undefined);

// senderInfo 可被每个用例覆盖
let currentSenderInfo: { name?: string } | undefined;

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
        isP2P: () => true,
        toStorageFormat: () => '{"text":"hi"}',
        chatId: 'lark_c1',
    };
}

// 不 mock.module('./factory') —— bun mock.module 是进程级全局，会污染
// 同进程的 factory.test.ts。改用 spyOn 只覆盖真实类的 transfer 静态方法，
// afterAll 还原，getContentFactory 保持真实，factory.test.ts 不受影响。
const transferSpy = spyOn(MessageTransferer, 'transfer').mockImplementation(
    async () => makeFakeMessage() as never,
);
afterAll(() => {
    transferSpy.mockRestore();
});

mock.module('./event-registry', () => ({
    EventHandler: () => () => undefined,
}));

mock.module('core/rules/engine', () => ({
    runRules: runRulesMock,
}));

mock.module('infrastructure/integrations/memory', () => ({
    storeMessage: storeMessageMock,
}));

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
    rabbitmqClient: { publish: mock(async () => undefined) },
    PROACTIVE_EVAL: 'proactive_eval',
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
mock.module('core/rules/rule', () => ({
    setBotIdentityResolver: mock(),
}));

const { larkEventHandlers } = await import('./handlers');

describe('handlers 入站路径 username 透传（无 fallback）', () => {
    beforeEach(() => {
        capturedStorePayload = undefined;
    });

    it('入站 user 消息 → storeMessage.username = message.senderInfo?.name', async () => {
        currentSenderInfo = { name: 'Alice' };

        await larkEventHandlers.handleMessageReceive({
            message: { message_id: 'lark_m1', message_type: 'text' },
        } as never);

        expect(capturedStorePayload).toBeDefined();
        expect(capturedStorePayload!.username).toBe('Alice');
        expect(capturedStorePayload!.user_id).toBe('internal_user_42');
        expect(runRulesMock).toHaveBeenCalled();
    });

    it('senderInfo 缺失 → username=undefined（无 fallback、不写脏占位）', async () => {
        currentSenderInfo = undefined;

        await larkEventHandlers.handleMessageReceive({
            message: { message_id: 'lark_m1', message_type: 'text' },
        } as never);

        expect(capturedStorePayload).toBeDefined();
        expect(capturedStorePayload!.username).toBeUndefined();
    });
});
