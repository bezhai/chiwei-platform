import { beforeEach, describe, expect, it, mock } from 'bun:test';

const larkMessages = new Map<string, { om_id: string; common_message_id: string }>();
const larkChats = new Map<string, { chat_id: string; common_conversation_id: string }>();

mock.module('ormconfig', () => ({
    default: {
        createEntityManager: mock(() => ({})),
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
            return {
                findOne: mock(async () => null),
                find: mock(async () => []),
                save: mock(async (value: unknown) => value),
                create: mock((value: unknown) => value),
                update: mock(async () => ({ affected: 0 })),
            };
        },
    },
}));

const { reverseResolveOutbound } = await import('../plugins/lark/outbound-reverse-resolve');

// 字段语义契约（钉死 publish ↔ consume 双侧字段口径）：
//
// chat-response-worker 把 chat.response payload 里的 ``message_id`` /
// ``chat_id`` / ``root_id`` 当 common_message/common_conversation id 喂给
// reverseResolveOutbound。lark 插件内部读取 lark_* 映射表，反查成飞书裸 id。
//
// 本测试钉死：
//   (a) ChatResponsePayload 三个 id 字段都按 common id 解释；
//   (b) 给个飞书裸 om_*/oc_* 会让 reverseResolveOutbound fail-loud，永远不静默
//       发到错地方 —— 即"publish 端塞错了字段值"的 contract violation 必被
//       消费方在边界炸出。

interface PublishedChatResponse {
    session_id: string;
    message_id: string;
    chat_id: string;
    root_id?: string;
}

// 模拟 chat-response-worker handleChatResponse 里那段从 payload destruct
// 后喂给 reverseResolveOutbound 的关键映射；不引入 worker 全量依赖。
async function consumeFieldMapping(
    payload: PublishedChatResponse,
): Promise<{
    channelMessageId: string;
    channelChatId: string;
    channelRootId: string | undefined;
}> {
    return reverseResolveOutbound({
        commonMessageId: payload.message_id,
        commonConversationId: payload.chat_id,
        commonRootMessageId: payload.root_id || undefined,
    });
}

describe('chat-response 字段映射契约：payload 三个 id 字段必须是 common id', () => {
    beforeEach(() => {
        larkMessages.clear();
        larkChats.clear();
    });

    it('publish 端正确填 common id -> consume 端反查回 lark 裸 id（happy path）', async () => {
        larkMessages.set('018f-common-msg', {
            common_message_id: '018f-common-msg',
            om_id: 'om_real_msg',
        });
        larkMessages.set('018f-common-root', {
            common_message_id: '018f-common-root',
            om_id: 'om_real_root',
        });
        larkChats.set('018f-common-chat', {
            common_conversation_id: '018f-common-chat',
            chat_id: 'oc_real_chat',
        });

        const out = await consumeFieldMapping(
            {
                session_id: 's1',
                message_id: '018f-common-msg',
                chat_id: '018f-common-chat',
                root_id: '018f-common-root',
            },
        );
        expect(out.channelMessageId).toBe('om_real_msg');
        expect(out.channelChatId).toBe('oc_real_chat');
        expect(out.channelRootId).toBe('om_real_root');
    });

    it('publish 端误把飞书裸 om_* 塞进 message_id -> reverseResolveOutbound 必抛 IdentityNotFoundError (fail-loud)', async () => {
        larkChats.set('018f-common-chat', {
            common_conversation_id: '018f-common-chat',
            chat_id: 'oc_x',
        });

        // message_id 字段值是 lark om_x... 而非 common_message_id
        await expect(
            consumeFieldMapping(
                {
                    session_id: 's2',
                    message_id: 'om_x100b6fecc8a838a4c3643c45e7a98db',
                    chat_id: '018f-common-chat',
                    root_id: undefined,
                },
            ),
        ).rejects.toThrow(/common_message_id=om_x100b6fecc8a838a4c3643c45e7a98db/);
    });

    it('publish 端误把飞书裸 om_* 塞进 root_id -> reverseResolveOutbound 必抛 IdentityNotFoundError (fail-loud)', async () => {
        larkMessages.set('018f-common-msg', {
            common_message_id: '018f-common-msg',
            om_id: 'om_msg',
        });
        larkChats.set('018f-common-chat', {
            common_conversation_id: '018f-common-chat',
            chat_id: 'oc_chat',
        });

        await expect(
            consumeFieldMapping(
                {
                    session_id: 's3',
                    message_id: '018f-common-msg',
                    chat_id: '018f-common-chat',
                    root_id: 'om_x100b6fecc8a838a4c3643c45e7a98db',
                },
            ),
        ).rejects.toThrow(
            /root common_message_id=om_x100b6fecc8a838a4c3643c45e7a98db/,
        );
    });
});
