// publish 的 AMQP header 上下文注入单测。
//
// 背景：泳道信息此前只编码进队列名（chat_request_{lane}）和消息体，没有写进 AMQP
// header。下游 agent-service 的 runtime/propagation.py::extract_context 只读 header
// 的 "lane" / "trace_id" 两个 key，读不到就当 lane=None —— 处理泳道消息时所有出站
// HTTP 不带 x-ctx-lane，被 sidecar 打回 prod。泳道队列 TTL 到期 DLX 降级回 prod 后
// 同理：prod 服务不知道这条消息原本属于哪个泳道。
//
// 约定对齐 propagation.py::inject_context：key 固定 "lane" / "trace_id"，空值写空
// 字符串而不是省略 key。

import { describe, it, expect, beforeEach, afterEach } from 'bun:test';
// 走基座 context 而不是任何服务侧的扩展包装：服务侧的包装模块被别的测试文件
// mock.module 掉过（桩里没有 getTraceId），而 bun 的 mock.module 是进程级全局。
import { context } from '../middleware/context';
import type * as MqClientModule from './client';

// 消费方服务里有若干测试文件 mock.module 掉了本模块，而 bun 的 mock.module 是
// 进程级全局：整套 bun test 跑起来时，直接 `import './client'` 拿到的可能是别人的桩
//（publish 变成空实现，断言全落空）。带 query 的 specifier 是另一个模块 key，拿得到
// 未被替换的真实实现和一个干净的单例。
const { rabbitmqClient, CHAT_REQUEST } = (await import(
    // @ts-expect-error 带 query 的 specifier 只有 bun 运行时认，tsc 解析不到；类型由下面的断言给出
    '@inner/shared/mq?real'
)) as typeof MqClientModule;

interface PublishCall {
    exchange: string;
    rk: string;
    content: Buffer;
    headers: Record<string, unknown> | undefined;
}

const calls: PublishCall[] = [];

const fakeChannel = {
    publish: (
        exchange: string,
        rk: string,
        content: Buffer,
        options: { headers?: Record<string, unknown> },
    ): boolean => {
        calls.push({ exchange, rk, content, headers: options.headers });
        return true;
    },
    assertQueue: async () => ({}),
    bindQueue: async () => ({}),
};

// publish 依赖已连接的 channel。这里直接注入假 channel，避免 mock.module('amqplib')
// 污染同进程其他测试（bun 的 mock.module 是进程级全局）。
function injectFakeChannel(): void {
    (rabbitmqClient as unknown as { channel: unknown }).channel = fakeChannel;
}

function clearChannel(): void {
    (rabbitmqClient as unknown as { channel: unknown }).channel = null;
}

function lastHeaders(): Record<string, unknown> {
    expect(calls.length).toBe(1);
    const headers = calls[0]!.headers;
    if (!headers) throw new Error('expected publish options.headers to be present');
    return headers;
}

const originalLane = process.env.LANE;

beforeEach(() => {
    calls.length = 0;
    injectFakeChannel();
});

afterEach(() => {
    clearChannel();
    if (originalLane === undefined) {
        delete process.env.LANE;
    } else {
        process.env.LANE = originalLane;
    }
});

describe('publish 注入 lane / trace_id header', () => {
    it('泳道进程：header 带上当前泳道和 trace_id', async () => {
        process.env.LANE = 'ppe-foo';

        await context.run(context.createContext('trace-lane-1'), async () => {
            await rabbitmqClient.publish(CHAT_REQUEST, { hello: 'world' });
        });

        expect(lastHeaders()).toEqual({ lane: 'ppe-foo', trace_id: 'trace-lane-1' });
    });

    it('prod 进程：lane 写空串而不是省略 key（对齐 inject_context）', async () => {
        delete process.env.LANE;

        await context.run(context.createContext('trace-prod-1'), async () => {
            await rabbitmqClient.publish(CHAT_REQUEST, { hello: 'world' });
        });

        expect(lastHeaders()).toEqual({ lane: '', trace_id: 'trace-prod-1' });
    });

    it('无 context 时 trace_id 写空串', async () => {
        delete process.env.LANE;

        await rabbitmqClient.publish(CHAT_REQUEST, { hello: 'world' });

        expect(lastHeaders()).toEqual({ lane: '', trace_id: '' });
    });

    it('显式传 lane 参数：header 用传入的泳道，而不是进程泳道', async () => {
        process.env.LANE = 'ppe-foo';

        await context.run(context.createContext('trace-explicit'), async () => {
            await rabbitmqClient.publish(CHAT_REQUEST, {}, undefined, undefined, 'ppe-bar');
        });

        const headers = lastHeaders();
        expect(headers.lane).toBe('ppe-bar');
        expect(calls[0]!.rk).toBe('chat.request.ppe-bar');
    });

    it("显式传 lane='prod'：header lane 写空串，走 prod 队列", async () => {
        process.env.LANE = 'ppe-foo';

        await context.run(context.createContext('trace-explicit-prod'), async () => {
            await rabbitmqClient.publish(CHAT_REQUEST, {}, undefined, undefined, 'prod');
        });

        const headers = lastHeaders();
        expect(headers.lane).toBe('');
        expect(calls[0]!.rk).toBe('chat.request');
    });

    it('调用方自带 header（x-retry-count）不被覆盖，且同样带上 lane / trace_id', async () => {
        process.env.LANE = 'ppe-foo';

        await context.run(context.createContext('trace-retry'), async () => {
            await rabbitmqClient.publish(CHAT_REQUEST, {}, undefined, { 'x-retry-count': 3 });
        });

        expect(lastHeaders()).toEqual({
            'x-retry-count': 3,
            lane: 'ppe-foo',
            trace_id: 'trace-retry',
        });
    });

    it('调用方给的 lane / trace_id header 被 publish 的权威值盖掉，自定义 header 留着', async () => {
        process.env.LANE = 'ppe-foo';

        await context.run(context.createContext('trace-authoritative'), async () => {
            await rabbitmqClient.publish(
                CHAT_REQUEST,
                {},
                undefined,
                { lane: 'ppe-caller', trace_id: 'trace-caller', 'x-retry-count': 3 },
                'ppe-bar',
            );
        });

        // lane header 必须跟 routing key 同源：routing key 用的是 publish 内部算出的
        // effectiveLane，header 让调用方改写就会出现「消息进 chat_request_ppe-bar
        // 队列、header 却写着 ppe-caller」，下游按 header 判 lane 直接判错——这正是
        // header 注入要消灭的那类不一致。trace_id 同理，只认当前 context。
        expect(lastHeaders()).toEqual({
            lane: 'ppe-bar',
            trace_id: 'trace-authoritative',
            'x-retry-count': 3,
        });
        expect(calls[0]!.rk).toBe('chat.request.ppe-bar');
    });

    it('delayMs 与上下文 header 并存', async () => {
        process.env.LANE = 'ppe-foo';

        await context.run(context.createContext('trace-delay'), async () => {
            await rabbitmqClient.publish(CHAT_REQUEST, {}, 5000);
        });

        expect(lastHeaders()).toEqual({
            'x-delay': 5000,
            lane: 'ppe-foo',
            trace_id: 'trace-delay',
        });
    });
});
