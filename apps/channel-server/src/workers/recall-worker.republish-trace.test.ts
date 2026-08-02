// 重投（republish）必须把入站消息的 trace_id 带下去。
//
// replies 还没落库时 handleRecall 会延时重投一条 recall —— 这是 recall 链路上唯一
// 由 worker 自己发起的出站消息。重投走 rabbitmq.ts::publish，而 publish 的 trace_id
// 取自 AsyncLocalStorage：重投分支若发生在 context 之外，写进 header 的就是空串，
// 真实重试路径上 trace 链断掉（lane 不受影响——它是显式算出来当参数传的）。
//
// 这里刻意**不 stub publish**：把真实 publish 接进 deps.republish（和 recall-worker
// main() 里的接法一致），断言最终落到 AMQP header 上的值。stub 掉只能验「handler
// 传了什么参数」，验不到「header 上最终是什么」，而后者才是下游
// propagation.py::extract_context 真正读的东西。同理它也顺带钉住 publish 的权威
// 字段不被调用方 header 覆盖（x-retry-count 这类自定义 header 则必须留着）。
//
// handler 侧「重投用哪个 lane 参数」的口径由 recall-worker.lane-header.test.ts 覆盖。

import { describe, it, expect, beforeEach, afterEach } from 'bun:test';
import type { ConsumeMessage } from 'amqplib';

import { handleRecall } from './recall-worker';
import type { RecallHandlerDeps } from './recall-worker';
import type * as RabbitMQModule from '@integrations/rabbitmq';

// 同 rabbitmq.test.ts：多个测试文件用 mock.module 把 @integrations/rabbitmq 整体换成
// 桩，而 bun 的 mock.module 是进程级全局。带 query 的 specifier 是另一个模块 key，
// 拿得到未被替换的真实 publish 和一个干净的单例。
const { rabbitmqClient, RECALL } = (await import(
    // @ts-expect-error 带 query 的 specifier 只有 bun 运行时认，tsc 解析不到；类型由下面的断言给出
    '../infrastructure/integrations/rabbitmq.ts?real'
)) as typeof RabbitMQModule;

interface PublishCall {
    rk: string;
    headers: Record<string, unknown> | undefined;
}

const published: PublishCall[] = [];

const fakeChannel = {
    publish: (
        _exchange: string,
        rk: string,
        _content: Buffer,
        options: { headers?: Record<string, unknown> },
    ): boolean => {
        published.push({ rk, headers: options.headers });
        return true;
    },
    assertQueue: async () => ({}),
    bindQueue: async () => ({}),
};

const originalLane = process.env.LANE;

beforeEach(() => {
    published.length = 0;
    (rabbitmqClient as unknown as { channel: unknown }).channel = fakeChannel;
    // 进程泳道故意设成第三个值：重投的 lane 必须来自入站 header，任何一步回落到
    // 进程 env 都会在断言里露出来。
    process.env.LANE = 'ppe-worker-env';
});

afterEach(() => {
    (rabbitmqClient as unknown as { channel: unknown }).channel = null;
    if (originalLane === undefined) {
        delete process.env.LANE;
    } else {
        process.env.LANE = originalLane;
    }
});

function makeMsg(headers?: Record<string, unknown>): ConsumeMessage {
    return {
        content: Buffer.from(
            JSON.stringify({
                channel: 'lark',
                session_id: 'sess-1',
                reason: 'unsafe',
                detail: 'test',
            }),
        ),
        fields: {} as ConsumeMessage['fields'],
        properties: { headers } as unknown as ConsumeMessage['properties'],
    } as ConsumeMessage;
}

// replies 还没落库（findOneBy → null）→ 走延时重投分支。republish 接真实 publish。
function makeDeps(): RecallHandlerDeps {
    return {
        repo: {
            findOneBy: async () => null,
            update: async () => ({ affected: 1 }),
        } as unknown as RecallHandlerDeps['repo'],
        getCapabilities: () => {
            throw new Error('重投分支不应该取 channel capabilities');
        },
        republish: (payload, delayMs, headers, lane) =>
            rabbitmqClient.publish(RECALL, payload, delayMs, headers, lane),
        ack: () => {},
        nack: () => {},
    };
}

describe('handleRecall 重投：AMQP header 上的 trace_id / lane', () => {
    it('泳道消息重投：header 的 trace_id 就是入站的 trace_id，lane 与 routing key 同源', async () => {
        await handleRecall(makeDeps(), makeMsg({ lane: 'ppe-taskb', trace_id: 'trace-inbound-1' }));

        expect(published.length).toBe(1);
        expect(published[0]!.rk).toBe('action.recall.ppe-taskb');
        expect(published[0]!.headers).toEqual({
            trace_id: 'trace-inbound-1',
            lane: 'ppe-taskb',
            'x-retry-count': 1,
            'x-delay': 5000,
        });
    });

    it('降级回 prod 的消息重投：trace 照样接上，lane 显式写空串（不回落进程 LANE）', async () => {
        await handleRecall(makeDeps(), makeMsg({ trace_id: 'trace-inbound-2' }));

        expect(published.length).toBe(1);
        expect(published[0]!.rk).toBe('action.recall');
        expect(published[0]!.headers).toEqual({
            trace_id: 'trace-inbound-2',
            lane: '',
            'x-retry-count': 1,
            'x-delay': 5000,
        });
    });

    it('入站没有 trace_id：不写空串，起一条新 trace（后续重试仍能串起来）', async () => {
        await handleRecall(makeDeps(), makeMsg({ lane: 'ppe-taskb' }));

        expect(published.length).toBe(1);
        expect(published[0]!.headers?.trace_id).toBeString();
        expect(published[0]!.headers?.trace_id).not.toBe('');
    });
});
