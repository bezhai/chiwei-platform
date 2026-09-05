import { afterAll, beforeEach, describe, expect, it, mock } from 'bun:test';

import type {
    ConversationRef,
    MessageRef,
    OutboundCapabilities,
    RenderContext,
} from '@inner/shared/channel';
import type { ContentItem, ThreadRef } from '@inner/shared/channel';
import type { ConsumeMessage } from 'amqplib';

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
const { handleChatResponse } = await import('./chat-response-handler');
type ChatResponseHandlerDeps = import('./chat-response-handler').ChatResponseHandlerDeps;

// 「出站上下文里放的是全局 id，不是反查后的渠道裸 id」回归钉死。
//
// 这条不变量最早是为一套按全局 id 查的 Redis 图片注册表立的；那套东西已经删了，
// 而不变量还活着：QQ 插件拿同一个字段反查 qq_message，给续段找回原始 msg_id 当回复
// 锚点、并据它派生出站幂等键。喂渠道裸 id 进去，插件那次反查必 miss —— 续段挂不上
// 原始 msg_id、幂等键跟着错，全程零报错。
//
// 本测试钉死两件事：
//   (1) worker 放进 ctx.sourceCommonMessageId 的是 payload.message_id（全局 common id），
//       不是它刚刚反查出来的 channelMessageId；
//   (2) 这两个确实是不同的字符串 —— 否则第 (1) 条验不出任何东西。

function makeCap(): {
    cap: OutboundCapabilities;
    calls: {
        reply: Array<{ thread: ThreadRef; content: ContentItem[]; ctx: RenderContext }>;
        sendText: Array<{ conv: ConversationRef; content: ContentItem[]; ctx: RenderContext }>;
    };
} {
    const calls = {
        reply: [] as Array<{ thread: ThreadRef; content: ContentItem[]; ctx: RenderContext }>,
        sendText: [] as Array<{ conv: ConversationRef; content: ContentItem[]; ctx: RenderContext }>,
    };
    const cap: OutboundCapabilities = {
        // 反查：全局 common id → 渠道裸 id。刻意返回一个跟 common id 不同的字符串。
        async resolveOutboundTarget() {
            return {
                message: { channelId: 'qq_real_msg' },
                conversation: { channelId: 'qq_real_conv' },
                rootMessage: undefined,
            };
        },
        async resolveMessageRef(): Promise<MessageRef> {
            return { channelId: 'qq_real_msg' };
        },
        async resolveConversationRef(): Promise<ConversationRef> {
            return { channelId: 'qq_real_conv' };
        },
        async recordOutboundMessage() {
            return 'common_assistant_msg_id';
        },
        async reply(thread, content, ctx): Promise<MessageRef> {
            calls.reply.push({ thread, content, ctx });
            return { channelId: 'qq_new_msg' };
        },
        async sendText(conv, content, ctx): Promise<MessageRef> {
            calls.sendText.push({ conv, content, ctx });
            return { channelId: 'qq_new_msg' };
        },
    };
    return { cap, calls };
}

function makeDeps(cap: OutboundCapabilities): ChatResponseHandlerDeps {
    return {
        repo: {
            findOneBy: async () => null,
            update: async () => ({ affected: 0 }),
            createQueryBuilder: () => {
                throw new Error('not expected on this path');
            },
        } as unknown as ChatResponseHandlerDeps['repo'],
        ownsChannel: () => true,
        getCapabilities: () => cap,
        ack: () => {},
        nack: () => {},
        observeDuration: () => {},
        observeQueueDelay: () => {},
    };
}

function makeMsg(payload: Record<string, unknown>): ConsumeMessage {
    return {
        content: Buffer.from(JSON.stringify(payload)),
        fields: {} as ConsumeMessage['fields'],
        properties: { headers: {} } as ConsumeMessage['properties'],
    } as ConsumeMessage;
}

describe('出站上下文里的源消息 id：必须是 common message_id，不能是反查后的渠道裸 id', () => {
    beforeEach(() => {
        qqMessages.clear();
        qqChats.clear();
    });

    it('worker 放进 ctx.sourceCommonMessageId 的是 payload.message_id，不是反查出的渠道裸 id', async () => {
        const { cap, calls } = makeCap();

        await handleChatResponse(
            makeDeps(cap),
            makeMsg({
                channel: 'qq',
                session_id: null,
                message_id: '018f-common-msg',
                chat_id: '018f-common-chat',
                is_p2p: false,
                content: '回一句',
                status: 'success',
                part_index: 0,
                is_last: true,
                bot_name: 'chiwei-qq',
            }),
        );

        expect(calls.reply.length).toBe(1);
        expect(calls.reply[0].ctx.sourceCommonMessageId).toBe('018f-common-msg');
        // 这一条是本文件存在的理由：喂裸 id 进去，插件的私有映射反查必 miss。
        expect(calls.reply[0].ctx.sourceCommonMessageId).not.toBe('qq_real_msg');
        // 而回复锚点用的确实是反查出来的裸 id —— 两个 id 各归各位。
        expect(calls.reply[0].thread.selfChannelMessageId).toBe('qq_real_msg');
    });

    it('common message_id 反查出的渠道裸 id 与 common id 不同 —— 用裸 id 反查必 miss', async () => {
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
    });
});
