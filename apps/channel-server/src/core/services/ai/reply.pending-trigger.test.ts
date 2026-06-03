import { describe, it, expect, mock, beforeEach } from 'bun:test';

// 5b 入站重排 + 必改2：makeTextReply 不再自己 publish / 取 setNx 锁 /
// 落 common_agent_response pending 行。它在 runRules 阶段只做纯预备工作：
// buildChatRequestPayload + 构造 pending 行落库闭包 savePending；通过
// ctx.registerPendingChatTrigger 登记 payload + lane + dedupeKey +
// savePending，由 handlers.ts 在 common/lark 入站消息写入成功、抢到去重锁后才调
// savePending() 并 publish（多 bot 同群只有抢锁 bot 写 pending 行，未
// 抢锁 bot 不留孤儿 pending 行）。本测试钉死：
//
//   makeTextReply 调用后 —— 不直接 publish、不取 setNx 锁、
//   **不立即 save pending 行**，而是 registerPendingChatTrigger 被调用
//   一次，payload 全局 ID 正确，savePending 是个尚未执行的闭包；
//   仅当显式调用 captured.savePending() 时 common_agent_response 行才落库。

const publishMock = mock(async () => undefined);
const setNxMock = mock(async () => 'OK');
const agentSaveMock = mock(async () => undefined);
const agentCreateMock = mock((x: unknown) => x);

mock.module('@integrations/rabbitmq', () => ({
    rabbitmqClient: { publish: publishMock },
    CHAT_REQUEST: { queue: 'chat_request', rk: 'chat.request' },
    getLane: () => undefined,
}));
mock.module('@cache/redis-client', () => ({
    setNx: setNxMock,
    evalScript: mock(async () => 1),
    exists: mock(async () => 0),
}));
mock.module('@repositories/repositories', () => ({
    CommonAgentResponseRepository: { create: agentCreateMock, save: agentSaveMock },
}));
mock.module('@middleware/context', () => ({
    context: {
        getBotName: () => 'bot-q',
        getLane: () => 'ppe-x',
        createContext: (botName?: string, traceId?: string, lane?: string) => ({
            botName,
            traceId: traceId ?? 't',
            lane,
        }),
        run: async (_ctx: unknown, cb: () => Promise<unknown>) => cb(),
    },
}));

const { makeTextReply } = await import('./reply');
import type { RuleMessage } from 'core/rules/rule-message';
import type { PendingChatTrigger } from 'core/rules/engine';

function rm(over: Partial<RuleMessage> = {}): RuleMessage {
    return {
        channel: 'qq',
        botName: 'bot-q',
        commonUserId: 'GU',
        commonConversationId: 'GC',
        commonMessageId: 'GM',
        commonRootMessageId: 'GR',
        isDirect: true,
        botCommonUserId: 'BOT-U',
        mentionedUserIds: [],
        createTime: 1,
        clearText: () => 'hi',
        text: () => 'hi',
        withoutEmojiText: () => 'hi',
        isTextOnly: () => true,
        isStickerOnly: () => false,
        stickerKey: () => '',
        imageKeys: () => [],
        ...over,
    };
}

describe('makeTextReply registers pending ChatTrigger instead of publishing', () => {
    beforeEach(() => {
        publishMock.mockClear();
        setNxMock.mockClear();
        agentSaveMock.mockClear();
        agentCreateMock.mockClear();
    });

    it('does not publish, does not take setNx lock; registers pending trigger with global ids', async () => {
        let captured: PendingChatTrigger | undefined;
        await makeTextReply(rm(), {
            registerPendingChatTrigger: (p) => {
                captured = p;
            },
        });

        expect(publishMock).not.toHaveBeenCalled();
        expect(setNxMock).not.toHaveBeenCalled();
        expect(captured).toBeDefined();
        expect(captured!.payload.message_id).toBe('GM');
        expect(captured!.payload.chat_id).toBe('GC');
        expect(captured!.payload.user_id).toBe('GU');
        expect(captured!.payload.root_id).toBe('GR');
        expect(captured!.payload.channel).toBe('qq');
        expect(captured!.payload.is_p2p).toBe(true);
        expect(captured!.lane).toBe('ppe-x');
        // dedupe lock key 后移到 handlers，但 key 口径必须跟旧实现一致
        expect(captured!.dedupeKey).toBe('make_reply:GM');
        // 必改2：common_agent_response pending 行 save 后移 —— makeTextReply
        // 内**不得**落库；只登记一个尚未执行的闭包。
        expect(agentSaveMock).not.toHaveBeenCalled();
        expect(typeof captured!.savePending).toBe('function');
        // 显式调闭包才真正落 pending 行（handlers 抢锁后才会调）。
        await captured!.savePending();
        expect(agentSaveMock).toHaveBeenCalledTimes(1);
    });

    it('still works without ctx (no-op registration safe)', async () => {
        // 防御性：无 ctx 传入时不 throw（handlers 一定传 ctx，
        // 但单元健壮性钉死）。
        await makeTextReply(rm());
        expect(publishMock).not.toHaveBeenCalled();
    });
});
