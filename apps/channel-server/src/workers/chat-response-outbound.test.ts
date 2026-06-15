import { describe, it, expect } from 'bun:test';

import { dispatchChatResponseOutbound } from './chat-response-outbound';
import type { OutboundCapabilities, RenderContext } from '@core/ports/channel-plugin';
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
        async resolveConversationRef() {
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
