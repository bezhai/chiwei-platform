import { describe, it, expect } from 'bun:test';

import { dispatchChatResponseOutbound, resolveChatResponseTarget } from './chat-response-outbound';
import type {
    OutboundCapabilities,
    OutboundTargetResolveInput,
    ConversationResolveInput,
    RenderContext,
} from '@core/ports/channel-plugin';
import type { ContentItem, ThreadRef } from '@core/channels/contracts';
import type { ConversationRef, MessageRef } from '@core/ports/channel-plugin';

// B3：chat-response-worker 出站走能力端口。worker 把「part_index / proactive →
// 回复 vs 新发」的出站策略（平台无关，任何 channel 都有「回复某条 vs 新发」的
// 选择）通过选择调 capabilities.reply / sendText 表达；端口只做原子能力。
//
// 本测试钉死 dispatch 决策与现状 worker 逐字一致：
//   part 0 + 非 proactive            → reply(触发消息)
//   part 0 + proactive + 有 root     → reply(root)
//   part 0 + proactive + 无 root     → sendText(chat)
//   part >0                          → sendText(chat)
// 且 content 是 AI 原始 markdown 文本、ctx 带 imageRegistryId（全局 id）+
// groupConversationId（渠道裸群会话 id）+ resolveMentions（群=true / p2p=false）。

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
        async resolveOutboundTarget() {
            throw new Error('not used');
        },
        async resolveMessageRef() {
            throw new Error('not used');
        },
        async recordOutboundMessage() {
            throw new Error('not used');
        },
        async reply(thread, content, ctx): Promise<MessageRef> {
            calls.reply.push({ thread, content, ctx });
            return { channelId: 'new_reply_id' };
        },
        async sendText(conv, content, ctx): Promise<MessageRef> {
            calls.sendText.push({ conv, content, ctx });
            return { channelId: 'new_send_id' };
        },
    };
    return { cap, calls };
}

const baseInput = {
    content: '赤尾的回复 ![p](1.png)',
    channelMessageId: 'om_trigger',
    channelConversationId: 'oc_chat',
    channelRootMessageId: 'om_root' as string | undefined,
    imageRegistryId: 'global_msg_ulid',
    isP2p: false,
};

describe('dispatchChatResponseOutbound', () => {
    it('part 0 非 proactive → reply(触发消息) + content 原始 markdown + ctx 全字段', async () => {
        const { cap, calls } = makeCap();
        const ref = await dispatchChatResponseOutbound(cap, {
            ...baseInput,
            partIndex: 0,
            isProactive: false,
        });

        expect(calls.sendText.length).toBe(0);
        expect(calls.reply.length).toBe(1);
        expect(calls.reply[0].thread.selfChannelMessageId).toBe('om_trigger');
        expect(calls.reply[0].thread.inThread).toBeUndefined();
        // content = AI 原始 markdown（飞书化由能力端口内部做）
        expect(calls.reply[0].content).toEqual([{ kind: 'text', text: '赤尾的回复 ![p](1.png)' }]);
        // ctx：registry 用全局 id；groupConversationId 渠道裸；群聊 resolveMentions=true
        expect(calls.reply[0].ctx).toEqual({
            imageRegistryId: 'global_msg_ulid',
            groupConversationId: 'oc_chat',
            resolveMentions: true,
        });
        expect(ref.channelId).toBe('new_reply_id');
    });

    it('part 0 proactive 有 root → reply(root)', async () => {
        const { cap, calls } = makeCap();
        await dispatchChatResponseOutbound(cap, {
            ...baseInput,
            partIndex: 0,
            isProactive: true,
        });

        expect(calls.reply.length).toBe(1);
        expect(calls.reply[0].thread.selfChannelMessageId).toBe('om_root');
        expect(calls.reply[0].thread.inThread).toBeUndefined();
        expect(calls.sendText.length).toBe(0);
    });

    it('part 0 proactive 无 root → sendText(chat)', async () => {
        const { cap, calls } = makeCap();
        await dispatchChatResponseOutbound(cap, {
            ...baseInput,
            channelRootMessageId: undefined,
            partIndex: 0,
            isProactive: true,
        });

        expect(calls.sendText.length).toBe(1);
        expect(calls.sendText[0].conv.channelId).toBe('oc_chat');
        expect(calls.reply.length).toBe(0);
    });

    it('part >0 → sendText(chat)', async () => {
        const { cap, calls } = makeCap();
        await dispatchChatResponseOutbound(cap, {
            ...baseInput,
            partIndex: 2,
            isProactive: false,
        });

        expect(calls.sendText.length).toBe(1);
        expect(calls.sendText[0].conv.channelId).toBe('oc_chat');
        expect(calls.reply.length).toBe(0);
    });

    it('p2p → ctx.resolveMentions=false（与现状 is_p2p 跳过 mention 一致）', async () => {
        const { cap, calls } = makeCap();
        await dispatchChatResponseOutbound(cap, {
            ...baseInput,
            isP2p: true,
            partIndex: 0,
            isProactive: false,
        });

        expect(calls.reply[0].ctx?.resolveMentions).toBe(false);
    });
});

// ---------------------------------------------------------------------------
// resolveChatResponseTarget：proactive 无 root 的合成消息（无 inbound 锚点，如
// agent-service persona review diff 推送）跳过 message 维度反查——合成 message_id
// 在渠道映射表没有行、全量反查必炸；conversation 维度必须照旧反查（fail-loud）。
// 其余路径（非 proactive / proactive 有 root）维持全量 resolveOutboundTarget 不变。
// ---------------------------------------------------------------------------

function makeResolveCap(opts: { conversationMiss?: boolean } = {}): {
    cap: OutboundCapabilities;
    calls: {
        resolveTarget: OutboundTargetResolveInput[];
        resolveConv: ConversationResolveInput[];
        sendText: Array<{ conv: ConversationRef }>;
        reply: Array<{ thread: ThreadRef }>;
    };
} {
    const calls = {
        resolveTarget: [] as OutboundTargetResolveInput[],
        resolveConv: [] as ConversationResolveInput[],
        sendText: [] as Array<{ conv: ConversationRef }>,
        reply: [] as Array<{ thread: ThreadRef }>,
    };
    const cap: OutboundCapabilities = {
        async resolveOutboundTarget(input) {
            calls.resolveTarget.push(input);
            return {
                message: { channelId: 'om_msg' },
                conversation: { channelId: 'oc_chat' },
                rootMessage: input.commonRootMessageId ? { channelId: 'om_root' } : undefined,
            };
        },
        async resolveConversationRef(input: ConversationResolveInput) {
            calls.resolveConv.push(input);
            if (opts.conversationMiss) {
                throw new Error(
                    `lark outbound cannot resolve common_conversation_id=${input.commonConversationId}`,
                );
            }
            return { channelId: 'oc_chat' };
        },
        async resolveMessageRef() {
            throw new Error('not used');
        },
        async recordOutboundMessage() {
            throw new Error('not used');
        },
        async reply(thread): Promise<MessageRef> {
            calls.reply.push({ thread });
            return { channelId: 'new_reply_id' };
        },
        async sendText(conv): Promise<MessageRef> {
            calls.sendText.push({ conv });
            return { channelId: 'new_send_id' };
        },
    };
    return { cap, calls };
}

const targetInput = {
    messageId: 'persona-review:prod:akao:v2', // 合成 id，渠道映射表没有这行
    conversationId: '018f-common-chat',
    rootMessageId: undefined as string | undefined,
    isProactive: true,
};

describe('resolveChatResponseTarget', () => {
    it('proactive 无 root → 跳过 message 维度反查，只反查 conversation', async () => {
        const { cap, calls } = makeResolveCap();
        const refs = await resolveChatResponseTarget(cap, targetInput);

        expect(calls.resolveTarget.length).toBe(0); // 合成 message_id 绝不进全量反查
        expect(calls.resolveConv).toEqual([{ commonConversationId: '018f-common-chat' }]);
        expect(refs.channelConversationId).toBe('oc_chat');
        expect(refs.channelMessageId).toBe('');
        expect(refs.channelRootMessageId).toBeUndefined();
    });

    it('proactive 无 root + conversation 反查失败 → 照样 fail-loud（绝不发进未知会话）', async () => {
        const { cap } = makeResolveCap({ conversationMiss: true });
        await expect(resolveChatResponseTarget(cap, targetInput)).rejects.toThrow(
            /common_conversation_id=018f-common-chat/,
        );
    });

    it('proactive 无 root + channel 没实现 resolveConversationRef → fail-loud', async () => {
        const { cap } = makeResolveCap();
        delete cap.resolveConversationRef;
        await expect(resolveChatResponseTarget(cap, targetInput)).rejects.toThrow(
            /resolveConversationRef/,
        );
    });

    it('非 proactive → 全量 resolveOutboundTarget，行为与现状逐字一致', async () => {
        const { cap, calls } = makeResolveCap();
        const refs = await resolveChatResponseTarget(cap, {
            messageId: '018f-common-msg',
            conversationId: '018f-common-chat',
            rootMessageId: '018f-common-root',
            isProactive: false,
        });

        expect(calls.resolveConv.length).toBe(0);
        expect(calls.resolveTarget).toEqual([
            {
                commonMessageId: '018f-common-msg',
                commonConversationId: '018f-common-chat',
                commonRootMessageId: '018f-common-root',
            },
        ]);
        expect(refs).toEqual({
            channelMessageId: 'om_msg',
            channelConversationId: 'oc_chat',
            channelRootMessageId: 'om_root',
        });
    });

    it('proactive 有 root → 仍走全量反查（现状路径零变化）', async () => {
        const { cap, calls } = makeResolveCap();
        const refs = await resolveChatResponseTarget(cap, {
            messageId: '018f-common-msg',
            conversationId: '018f-common-chat',
            rootMessageId: '018f-common-root',
            isProactive: true,
        });

        expect(calls.resolveConv.length).toBe(0);
        expect(calls.resolveTarget.length).toBe(1);
        expect(refs.channelRootMessageId).toBe('om_root');
    });

    it('proactive 无 root：resolve → dispatch 直达 sendText（合成消息端到端不碰 reply）', async () => {
        const { cap, calls } = makeResolveCap();
        const refs = await resolveChatResponseTarget(cap, targetInput);
        await dispatchChatResponseOutbound(cap, {
            content: '【persona 慢漂】akao 写下了新一版身份正文',
            channelMessageId: refs.channelMessageId,
            channelConversationId: refs.channelConversationId,
            channelRootMessageId: refs.channelRootMessageId,
            imageRegistryId: 'persona-review:prod:akao:v2',
            isP2p: false,
            partIndex: 0,
            isProactive: true,
        });

        expect(calls.reply.length).toBe(0);
        expect(calls.sendText).toEqual([{ conv: { channelId: 'oc_chat' } }]);
    });
});
