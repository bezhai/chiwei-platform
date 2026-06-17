import { beforeEach, describe, expect, it, mock } from 'bun:test';

const larkChats = new Map<string, { chat_id: string; common_conversation_id: string }>();

mock.module('ormconfig', () => ({
    default: {
        getRepository: (entity: { name?: string }) => {
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

const { resolveLarkConversationRef } = await import('./outbound-reverse-resolve');

// 主动发（is_proactive）没有来源消息，只有真实的 common_conversation_id（p2p 会话）。
// resolveLarkConversationRef 只做会话反查：common_conversation_id → 飞书裸 chat_id，
// 绝不碰 lark_message 表（那才是被动回复反查源消息时做的事）。
// 查不到 fail-loud，绝不静默把主动发的消息送到错地方。

describe('resolveLarkConversationRef（会话独立反查，主动发用）', () => {
    beforeEach(() => {
        larkChats.clear();
    });

    it('common_conversation_id -> 飞书裸 chat_id（不碰 lark_message）', async () => {
        larkChats.set('018f-p2p', {
            common_conversation_id: '018f-p2p',
            chat_id: 'oc_real_p2p',
        });

        const ref = await resolveLarkConversationRef('018f-p2p');
        expect(ref.channelId).toBe('oc_real_p2p');
    });

    it('查不到会话 -> fail-loud', async () => {
        await expect(resolveLarkConversationRef('018f-missing')).rejects.toThrow(
            /common_conversation_id=018f-missing/,
        );
    });
});
