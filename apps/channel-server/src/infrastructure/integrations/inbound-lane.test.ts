// 入站 lane 分发 MQ（fail-closed）单测。验证：
//  - 队列名 inbound_lane.{lane}
//  - 队列声明 fail-closed：durable:true、无 x-message-ttl、无 dead-letter 回 prod
//    （绝不复用现状 lane 队列的 10s TTL + DLX-to-prod，§4.6）
//  - publish 失败抛错、不静默吞（fail-closed 可观测）
//  - 三元组幂等 key 拼装 event_type + globalMessageId + lane

import { describe, it, expect } from 'bun:test';
import {
    inboundLaneQueueName,
    assertInboundLaneQueue,
    publishInboundLane,
    inboundDedupeKey,
    type InboundLaneEnvelope,
} from './inbound-lane';

// 极简假 amqp Channel：记录 assertQueue 参数 + sendToQueue 调用。
class FakeChannel {
    asserted: Array<{ queue: string; options: unknown }> = [];
    sent: Array<{ queue: string; content: string }> = [];
    failAssert = false;
    failSend = false;

    async assertQueue(queue: string, options: unknown): Promise<void> {
        if (this.failAssert) throw new Error('assertQueue boom');
        this.asserted.push({ queue, options });
    }
    sendToQueue(queue: string, content: Buffer): boolean {
        if (this.failSend) throw new Error('sendToQueue boom');
        this.sent.push({ queue, content: content.toString() });
        return true;
    }
}

const envelope: InboundLaneEnvelope = {
    channel: 'lark',
    event_type: 'im.message.receive_v1',
    global_message_id: 'gmid-1',
    trace_id: 'trace-1',
    lane: 'ppe-foo',
    bot_name: 'chiwei',
    params: { hello: 'world' },
};

describe('inbound_lane MQ（fail-closed 入站分发）', () => {
    it('队列名是 inbound_lane.{lane}', () => {
        expect(inboundLaneQueueName('ppe-foo')).toBe('inbound_lane.ppe-foo');
    });

    it('队列声明 fail-closed：durable，无 TTL、无 dead-letter 回 prod', async () => {
        const ch = new FakeChannel();
        await assertInboundLaneQueue(ch as never, 'ppe-foo');
        expect(ch.asserted.length).toBe(1);
        const opts = ch.asserted[0].options as {
            durable?: boolean;
            arguments?: Record<string, unknown>;
        };
        expect(opts.durable).toBe(true);
        // 关键 fail-closed 断言：没有 10s TTL、没有 dead-letter 回 prod
        const args = opts.arguments ?? {};
        expect(args['x-message-ttl']).toBeUndefined();
        expect(args['x-dead-letter-exchange']).toBeUndefined();
        expect(args['x-dead-letter-routing-key']).toBeUndefined();
    });

    it('publish 把 envelope 投到目标 lane 队列', async () => {
        const ch = new FakeChannel();
        await publishInboundLane(ch as never, envelope);
        expect(ch.sent.length).toBe(1);
        expect(ch.sent[0].queue).toBe('inbound_lane.ppe-foo');
        expect(JSON.parse(ch.sent[0].content)).toEqual(envelope as never);
    });

    it('assertQueue 失败 → 抛错（fail-closed，不静默吞）', async () => {
        const ch = new FakeChannel();
        ch.failAssert = true;
        await expect(publishInboundLane(ch as never, envelope)).rejects.toThrow();
        expect(ch.sent.length).toBe(0);
    });

    it('sendToQueue 失败 → 抛错（fail-closed）', async () => {
        const ch = new FakeChannel();
        ch.failSend = true;
        await expect(publishInboundLane(ch as never, envelope)).rejects.toThrow();
    });

    it('幂等 key = event_type + globalMessageId + lane', () => {
        expect(inboundDedupeKey(envelope)).toBe(
            'inbound_lane:im.message.receive_v1:gmid-1:ppe-foo',
        );
    });
});
