import { beforeEach, describe, expect, it } from 'bun:test';

import { recallReplies } from './recall-outbound';
import type { OutboundCapabilities, MessageRef } from '@core/ports/channel-plugin';

const channelMessages = new Map<string, string>();

// recall-worker 撤回走能力端口。逐条读取 common_agent_response.replies[]
// .common_message_id，先经当前 channel 插件反查渠道裸 message id，再调用
// capabilities.recall({ channelId: 裸id })。

function makeCap(failFor: Set<string> = new Set()): {
    cap: OutboundCapabilities;
    recalled: string[];
} {
    const recalled: string[] = [];
    const cap: OutboundCapabilities = {
        async resolveOutboundTarget() {
            throw new Error('not used');
        },
        async resolveMessageRef(input) {
            const channelId = channelMessages.get(input.commonMessageId);
            if (!channelId) {
                throw new Error(`cannot resolve common_message_id=${input.commonMessageId}`);
            }
            return { channelId };
        },
        async resolveConversationRef(): Promise<MessageRef> {
            throw new Error('not used');
        },
        async recordOutboundMessage() {
            throw new Error('not used');
        },
        async sendText(): Promise<MessageRef> {
            throw new Error('not used');
        },
        async reply(): Promise<MessageRef> {
            throw new Error('not used');
        },
        async recall(msg) {
            if (failFor.has(msg.channelId)) {
                throw new Error(`lark delete failed for ${msg.channelId}`);
            }
            recalled.push(msg.channelId);
        },
    };
    return { cap, recalled };
}

describe('recallReplies', () => {
    beforeEach(() => {
        channelMessages.clear();
    });

    it('逐条 common_message_id 反查后调 capabilities.recall(裸 message id)，全成功 → recalled 计数', async () => {
        channelMessages.set('018f-a', 'om_a');
        channelMessages.set('018f-b', 'om_b');

        const { cap, recalled } = makeCap();
        const result = await recallReplies(cap, [
            { common_message_id: '018f-a' },
            { common_message_id: '018f-b' },
        ]);

        expect(recalled).toEqual(['om_a', 'om_b']);
        expect(result.recalled).toBe(2);
        expect(result.failed).toBe(0);
    });

    it('部分失败 → 成功计 recalled、失败计 failed，不中断后续', async () => {
        channelMessages.set('018f-a', 'om_a');
        channelMessages.set('018f-b', 'om_b');
        channelMessages.set('018f-c', 'om_c');

        const { cap, recalled } = makeCap(new Set(['om_b']));
        const result = await recallReplies(cap, [
            { common_message_id: '018f-a' },
            { common_message_id: '018f-b' },
            { common_message_id: '018f-c' },
        ]);

        expect(recalled).toEqual(['om_a', 'om_c']);
        expect(result.recalled).toBe(2);
        expect(result.failed).toBe(1);
    });

    it('全失败 → recalled=0、failed=全部（worker 据此标 recall_failed）', async () => {
        channelMessages.set('018f-a', 'om_a');
        channelMessages.set('018f-b', 'om_b');

        const { cap, recalled } = makeCap(new Set(['om_a', 'om_b']));
        const result = await recallReplies(cap, [
            { common_message_id: '018f-a' },
            { common_message_id: '018f-b' },
        ]);

        expect(recalled).toEqual([]);
        expect(result.recalled).toBe(0);
        expect(result.failed).toBe(2);
    });

    it('能力不支持 recall（端口无 recall）→ fail-loud 抛错，绝不静默', async () => {
        const cap = {
            async resolveOutboundTarget() {
                throw new Error('not used');
            },
            async resolveMessageRef() {
                return { channelId: 'om_a' };
            },
            async resolveConversationRef(): Promise<MessageRef> {
                throw new Error('not used');
            },
            async recordOutboundMessage() {
                throw new Error('not used');
            },
            async sendText(): Promise<MessageRef> {
                throw new Error('not used');
            },
            async reply(): Promise<MessageRef> {
                throw new Error('not used');
            },
        } as OutboundCapabilities;
        await expect(
            recallReplies(cap, [{ common_message_id: '018f-a' }]),
        ).rejects.toThrow();
    });

    it('common_message_id 找不到 channel 映射 → 计 failed，不调用 recall', async () => {
        const { cap, recalled } = makeCap();
        const result = await recallReplies(cap, [{ common_message_id: '018f-missing' }]);

        expect(recalled).toEqual([]);
        expect(result.recalled).toBe(0);
        expect(result.failed).toBe(1);
    });
});
