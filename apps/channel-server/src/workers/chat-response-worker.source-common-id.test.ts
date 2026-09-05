import { afterAll, beforeEach, describe, expect, it, mock } from 'bun:test';

import { imageRegistryLookupId } from './image-registry-key';

const qqMessages = new Map<string, { qq_message_id: string; common_message_id: string }>();
const qqChats = new Map<string, { conversation_id: string; common_conversation_id: string }>();

// bun 的 mock.module 是**整模块替换**且**进程级全局**（mock.restore() 不撤销）：
// 不抓真身就会把 ormconfig 的其他导出顶掉，不在 afterAll 放回真身就会让后续
// 加载的**生产代码**一直看到这个只有 findOne 的假 DataSource。
const realOrmconfig = { ...(await import('ormconfig')) };
mock.module('ormconfig', () => ({
    ...realOrmconfig,
    default: {
        getRepository: (entity: { name?: string }) => {
            if (entity.name === 'QqMessage') {
                return {
                    findOne: mock(
                        async ({ where }: { where: { common_message_id: string } }) =>
                            qqMessages.get(where.common_message_id) ?? null,
                    ),
                };
            }
            if (entity.name === 'QqGroupChatInfo') {
                return {
                    findOne: mock(
                        async ({
                            where,
                        }: {
                            where: { common_conversation_id: string };
                        }) => qqChats.get(where.common_conversation_id) ?? null,
                    ),
                };
            }
            throw new Error(`unexpected repository: ${entity.name}`);
        },
    },
}));

afterAll(() => {
    mock.module('ormconfig', () => realOrmconfig);
});

const { reverseResolveOutbound } = await import('../plugins/qq/outbound-reverse-resolve');

// 发图被吞回归钉死（trace aae2dd2cacbc711123da9e41d1525e4f）：
//
// agent-service 在 app/chat/context.py 用 ImageRegistry(req.message_id) 把 generate_image
// 产出的图片注册到 Redis，key = image_registry:{common_message_id}。
// chat-response-worker 必须用同一个 common_message_id 去查 registry。
//
// worker 需要把 payload.message_id 反查成渠道裸 message id 用于 reply，但 registry
// 查询仍然必须使用 common_message_id。用渠道裸 id 查 registry 会 miss。
//
// 本测试钉死：image registry 的查询 id 是 payload.message_id（common id），
// 且它跟 reverseResolveOutbound 反查出来的渠道裸 id 是不同的字符串。

describe('image registry 查询 id 契约：必须用 common message_id，不能用反查后的渠道裸 id', () => {
    beforeEach(() => {
        qqMessages.clear();
        qqChats.clear();
    });

    it('registry 查询 id == payload.message_id（agent-service 注册用的同一个 key）', () => {
        const commonMsg = '018f-common-msg';
        expect(imageRegistryLookupId({ message_id: commonMsg })).toBe(commonMsg);
    });

    it('common message_id 反查出的渠道裸 id 与 common id 不同 —— 用裸 id 查 registry 必 miss', async () => {
        qqMessages.set('018f-common-msg', {
            common_message_id: '018f-common-msg',
            qq_message_id: 'qq_real_msg',
        });
        qqChats.set('018f-common-chat', {
            common_conversation_id: '018f-common-chat',
            conversation_id: 'qq_real_conv',
        });

        const rr = await reverseResolveOutbound({
            commonMessageId: '018f-common-msg',
            commonConversationId: '018f-common-chat',
            commonRootMessageId: undefined,
        });

        // 反查回的渠道裸 id 就是 qq_real_msg，跟 common id 是两个不同字符串
        expect(rr.channelMessageId).toBe('qq_real_msg');
        expect(rr.channelMessageId).not.toBe('018f-common-msg');

        // registry 查询必须用 common id，绝不能用 rr.channelMessageId（bug 路径）
        const lookupId = imageRegistryLookupId({ message_id: '018f-common-msg' });
        expect(lookupId).toBe('018f-common-msg');
        expect(lookupId).not.toBe(rr.channelMessageId);
    });
});
