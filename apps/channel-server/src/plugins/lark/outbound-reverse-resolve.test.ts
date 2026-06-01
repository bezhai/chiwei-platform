import { beforeEach, describe, expect, it, mock } from 'bun:test';

const larkMessages = new Map<string, { om_id: string; common_message_id: string }>();
const larkChats = new Map<string, { chat_id: string; common_conversation_id: string }>();

mock.module('ormconfig', () => ({
    default: {
        getRepository: (entity: { name?: string }) => {
            if (entity.name === 'LarkMessage') {
                return {
                    findOne: mock(
                        async ({ where }: { where: { common_message_id: string } }) =>
                            larkMessages.get(where.common_message_id) ?? null,
                    ),
                };
            }
            if (entity.name === 'LarkBaseChatInfo') {
                return {
                    findOne: mock(
                        async ({
                            where,
                        }: {
                            where: { common_conversation_id: string };
                        }) => larkChats.get(where.common_conversation_id) ?? null,
                    ),
                };
            }
            throw new Error(`unexpected repository: ${entity.name}`);
        },
    },
}));

const { reverseResolveOutbound } = await import('./outbound-reverse-resolve');

// 飞书出站适配器只在 lark 层做反查：chat-response-worker 拿到 common_*_id，
// reverseResolveOutbound 读取 lark_message/lark_base_chat_info 映射回飞书裸 id。
// common 层和 agent-service 都不允许持有 common->lark 的公共映射表。

describe('reverseResolveOutbound', () => {
    beforeEach(() => {
        larkMessages.clear();
        larkChats.clear();
    });

    it('reverse-resolves common ids back to lark channel ids', async () => {
        larkMessages.set('018f-msg', {
            common_message_id: '018f-msg',
            om_id: 'om_lark_msg',
        });
        larkMessages.set('018f-root', {
            common_message_id: '018f-root',
            om_id: 'om_lark_root',
        });
        larkChats.set('018f-chat', {
            common_conversation_id: '018f-chat',
            chat_id: 'oc_lark_chat',
        });

        const out = await reverseResolveOutbound({
            commonMessageId: '018f-msg',
            commonConversationId: '018f-chat',
            commonRootMessageId: '018f-root',
        });
        expect(out.channelMessageId).toBe('om_lark_msg');
        expect(out.channelChatId).toBe('oc_lark_chat');
        expect(out.channelRootId).toBe('om_lark_root');
    });

    it('missing lark message mapping -> throws (fail-loud, never silent wrong-send)', async () => {
        larkChats.set('018f-chat', {
            common_conversation_id: '018f-chat',
            chat_id: 'oc_lark_chat',
        });

        await expect(
            reverseResolveOutbound({
                commonMessageId: '018f-missing',
                commonConversationId: '018f-chat',
                commonRootMessageId: undefined,
            }),
        ).rejects.toThrow(/common_message_id=018f-missing/);
    });

    it('missing lark conversation mapping -> throws (fail-loud, never silent wrong-send)', async () => {
        larkMessages.set('018f-msg', {
            common_message_id: '018f-msg',
            om_id: 'om_lark_msg',
        });

        await expect(
            reverseResolveOutbound({
                commonMessageId: '018f-msg',
                commonConversationId: '018f-missing-chat',
                commonRootMessageId: undefined,
            }),
        ).rejects.toThrow(/common_conversation_id=018f-missing-chat/);
    });

    it('no root common id -> channelRootId undefined', async () => {
        larkMessages.set('018f-msg', {
            common_message_id: '018f-msg',
            om_id: 'om_lark_msg',
        });
        larkChats.set('018f-chat', {
            common_conversation_id: '018f-chat',
            chat_id: 'oc_lark_chat',
        });

        const out = await reverseResolveOutbound({
            commonMessageId: '018f-msg',
            commonConversationId: '018f-chat',
            commonRootMessageId: undefined,
        });
        expect(out.channelRootId).toBeUndefined();
    });
});
