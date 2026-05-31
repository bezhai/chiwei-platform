import { describe, it, expect } from 'bun:test';

import { recallReplies } from './recall-outbound';
import type { OutboundCapabilities, MessageRef } from '@core/ports/channel-plugin';

// B3：recall-worker 撤回走能力端口。逐条撤回 agent_responses.replies[].message_id
// （现状是飞书裸 message id），改 capabilities.recall({channelId: 裸id})；撤回循环
// /计数（recalled/failed）是 worker 编排，留在 worker，端口只做「撤回这一条」。

function makeCap(failFor: Set<string> = new Set()): {
    cap: OutboundCapabilities;
    recalled: string[];
} {
    const recalled: string[] = [];
    const cap: OutboundCapabilities = {
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
    it('逐条调 capabilities.recall(裸 message id)，全成功 → recalled 计数', async () => {
        const { cap, recalled } = makeCap();
        const result = await recallReplies(cap, [
            { message_id: 'om_a' },
            { message_id: 'om_b' },
        ]);

        expect(recalled).toEqual(['om_a', 'om_b']);
        expect(result.recalled).toBe(2);
        expect(result.failed).toBe(0);
    });

    it('部分失败 → 成功计 recalled、失败计 failed，不中断后续', async () => {
        const { cap, recalled } = makeCap(new Set(['om_b']));
        const result = await recallReplies(cap, [
            { message_id: 'om_a' },
            { message_id: 'om_b' },
            { message_id: 'om_c' },
        ]);

        expect(recalled).toEqual(['om_a', 'om_c']);
        expect(result.recalled).toBe(2);
        expect(result.failed).toBe(1);
    });

    it('全失败 → recalled=0、failed=全部（worker 据此标 recall_failed）', async () => {
        const { cap, recalled } = makeCap(new Set(['om_a', 'om_b']));
        const result = await recallReplies(cap, [
            { message_id: 'om_a' },
            { message_id: 'om_b' },
        ]);

        expect(recalled).toEqual([]);
        expect(result.recalled).toBe(0);
        expect(result.failed).toBe(2);
    });

    it('能力不支持 recall（端口无 recall）→ fail-loud 抛错，绝不静默', async () => {
        const cap = {
            async sendText(): Promise<MessageRef> {
                throw new Error('not used');
            },
            async reply(): Promise<MessageRef> {
                throw new Error('not used');
            },
        } as OutboundCapabilities;
        await expect(recallReplies(cap, [{ message_id: 'om_a' }])).rejects.toThrow();
    });
});
