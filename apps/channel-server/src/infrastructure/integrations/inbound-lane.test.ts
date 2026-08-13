// 入站 lane 分发 MQ（fail-closed）单测。验证：
//  - 队列按 channel + lane 分区：inbound_lane.{channel}.{lane}，分区前的
//    inbound_lane.{lane} 在切换窗口内仍然可用
//  - 队列声明 fail-closed：durable:true、无 x-message-ttl、无 dead-letter 回 prod
//    （绝不复用现状 lane 队列的 10s TTL + DLX-to-prod，§4.6）；新队列同样如此
//  - publish 失败抛错、不静默吞（fail-closed 可观测）
//  - 幂等 key 带 channel，且与 lark-service 逐字相同

import { describe, it, expect } from 'bun:test';
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

function withoutChannel(): InboundLaneEnvelope {
    const legacy = { ...envelope };
    delete legacy.channel;
    return legacy;
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

    // 决策九第二步：消费侧双订阅上线之后才切生产者。
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

    it('老信封没有 channel 字段时按飞书算（那个年代只有飞书在用这个队列）', async () => {
        const ch = new FakeChannel();
        await publishInboundLane(ch as never, withoutChannel(), true);
        expect(ch.sent[0].queue).toBe('inbound_lane.lark.ppe-foo');
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

    // ⚠️ 跨服务契约，同上：lark-service 的 inboundLaneDedupeKey 拼的是逐字相同的串。
    // 不统一的后果是同一条消息重投时换了消费者就认不出自己处理过 —— 处理两遍。
    it('幂等 key = channel + event_type + globalMessageId + lane', () => {
        expect(inboundDedupeKey(envelope)).toBe(
            'inbound_lane:lark:im.message.receive_v1:gmid-1:ppe-foo',
        );
    });

    // 分区之后两个服务各守一个 channel，key 里的 channel 段就是它们不重叠的依据。
    it('两个 channel 的同名事件不再互相顶掉完成标记', () => {
        expect(inboundDedupeKey({ ...envelope, channel: 'qq' })).not.toBe(
            inboundDedupeKey(envelope),
        );
    });

    it('老信封没有 channel 字段时算成飞书的 key', () => {
        expect(inboundDedupeKey(withoutChannel())).toBe(inboundDedupeKey(envelope));
    });

    // key 不含队列名：双订阅期间同一条消息可能从旧队列来、也可能从新队列来，认队列的
    // 话两边各算一次，用户看到两条回复。
    it('幂等 key 不掺队列名', () => {
        expect(inboundDedupeKey(envelope)).not.toContain(sharedInboundLaneQueueName('ppe-foo'));
        expect(inboundDedupeKey(envelope)).not.toContain(
            inboundLaneQueueName('lark', 'ppe-foo'),
        );
    });
});
