// 入站 lane 分发 MQ（fail-closed）单测。验证：
//  - 队列按 channel + lane 分区：inbound_lane.{channel}.{lane}，分区前的
//    inbound_lane.{lane} 仍然可用（分区那场迁移的两个开关还没打开，泳道信封目前
//    全部走它）
//  - 队列声明 fail-closed：durable:true、无 x-message-ttl、无 dead-letter 回 prod
//    （绝不复用现状 lane 队列的 10s TTL + DLX-to-prod，§4.6）；新队列同样如此
//  - publish 失败抛错、不静默吞（fail-closed 可观测）
//  - 幂等 key 由信封的哪几个字段拼成（格式本身钉在共享包里）

import { describe, it, expect } from 'bun:test';
import { inboundLaneClaimKey } from '@inner/shared/inbound-lane-claim';
import {
    inboundLaneQueueName,
    sharedInboundLaneQueueName,
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

/** 队列里可能还躺着不带 channel 的旧信封 —— 线格式不受类型系统保护。 */
function withoutChannel(): InboundLaneEnvelope {
    const { channel: _channel, ...rest } = envelope;
    return rest as InboundLaneEnvelope;
}

describe('inbound_lane MQ（fail-closed 入站分发）', () => {
    // 队列的分区维度必须和消费者的所有权维度一致。拆分后 owner 是 channel + lane，
    // 只按 lane 分的话 QQ 和飞书的信封躺在同一条队列里被两个服务竞争消费。
    //
    // ⚠️ 这两个字面量是**跨服务契约**：lark-service 的 lane-queue.ts 必须拼出逐字相同
    // 的名字（那边有一条同样写死字面量的测试）。两个 app 是两个包，编译期对不上。
    it('队列名是 inbound_lane.{channel}.{lane}', () => {
        expect(inboundLaneQueueName('lark', 'ppe-foo')).toBe('inbound_lane.lark.ppe-foo');
    });

    it('两个 channel 在同一条 lane 上不共享队列', () => {
        expect(inboundLaneQueueName('qq', 'ppe-foo')).not.toBe(
            inboundLaneQueueName('lark', 'ppe-foo'),
        );
    });

    // 切换窗口内消费侧要同时订阅新旧两条队列，旧名字不能删。
    it('分区前的队列名仍然是 inbound_lane.{lane}', () => {
        expect(sharedInboundLaneQueueName('ppe-foo')).toBe('inbound_lane.ppe-foo');
    });

    it('队列声明 fail-closed：durable，无 TTL、无 dead-letter 回 prod', async () => {
        const ch = new FakeChannel();
        await assertInboundLaneQueue(ch as never, 'inbound_lane.lark.ppe-foo');
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

    it('publish 默认投分区前的共享队列', async () => {
        const ch = new FakeChannel();
        await publishInboundLane(ch as never, envelope, false);
        expect(ch.sent.length).toBe(1);
        expect(ch.sent[0].queue).toBe('inbound_lane.ppe-foo');
        expect(JSON.parse(ch.sent[0].content)).toEqual(envelope as never);
    });

    // 分区迁移第二步：消费侧先订上分区队列，生产者才能切过去。
    it('开关打开后 publish 投按 channel 分区的队列', async () => {
        const ch = new FakeChannel();
        await publishInboundLane(ch as never, envelope, true);
        expect(ch.sent[0].queue).toBe('inbound_lane.lark.ppe-foo');
        expect(ch.asserted[0].queue).toBe('inbound_lane.lark.ppe-foo');
    });

    it('按信封自己的 channel 分区，不是按写死的飞书', async () => {
        const ch = new FakeChannel();
        await publishInboundLane(ch as never, { ...envelope, channel: 'qq' }, true);
        expect(ch.sent[0].queue).toBe('inbound_lane.qq.ppe-foo');
    });

    // 本服务已经不处理飞书了。没有 channel 的信封曾经被当成飞书，那在飞书还归本服务
    // 的时候是对的；现在它只会把信封投到一条本服务永远不消费的队列上，而且是静默的。
    it('信封没有 channel 时抛错，不猜一个渠道出来', async () => {
        const ch = new FakeChannel();
        await expect(publishInboundLane(ch as never, withoutChannel(), true)).rejects.toThrow(
            /carries no channel/,
        );
        expect(ch.sent.length).toBe(0);
    });

    it('assertQueue 失败 → 抛错（fail-closed，不静默吞）', async () => {
        const ch = new FakeChannel();
        ch.failAssert = true;
        await expect(publishInboundLane(ch as never, envelope, false)).rejects.toThrow();
        expect(ch.sent.length).toBe(0);
    });

    it('sendToQueue 失败 → 抛错（fail-closed）', async () => {
        const ch = new FakeChannel();
        ch.failSend = true;
        await expect(publishInboundLane(ch as never, envelope, false)).rejects.toThrow();
    });

    // key 的格式钉在共享包里（packages/ts-shared/src/inbound-lane-claim.test.ts），两个
    // 服务用的是同一份实现。这里只钉本服务自己的那一半：线格式的字段落进 key 的哪一段。
    // 字段错位不会报错，只会算出一把没人认识的锁。
    it('幂等 key 由信封的 channel + event_type + globalMessageId + lane 拼成', () => {
        expect(inboundDedupeKey(envelope)).toBe(
            inboundLaneClaimKey({
                channel: 'lark',
                eventType: 'im.message.receive_v1',
                globalMessageId: 'gmid-1',
                lane: 'ppe-foo',
            }),
        );
    });

    // 猜出来的 key 比算不出 key 危险得多：猜错了就是替另一个服务写下「这条处理过了」。
    it('信封没有 channel 时算不出 key，直接抛', () => {
        expect(() => inboundDedupeKey(withoutChannel())).toThrow(/carries no channel/);
    });
});
