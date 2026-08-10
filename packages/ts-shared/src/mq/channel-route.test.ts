// 队列拓扑的 channel 维度 + 消费者的 cancel / drain 能力。
//
// 背景：chat_response / recall 此前只按 lane 分区，channel 只是 payload 里的一个
// 字段。出站 owner 按 channel 拆开之后，两个服务会竞争消费同一条队列，流量被
// RabbitMQ 轮询劈成两半。分区维度必须跟所有权维度一致。
//
// 命名口径：channel 揉进 base 名，泳道后缀继续加在最后 ——
//   chat_response      → chat_response_lark      → chat_response_lark_{lane}
//   chat.response      → chat.response.lark      → chat.response.lark.{lane}
// 这样现有的 laneQueue / laneRK 原样套用，泳道队列的 x-dead-letter-routing-key
// （它拿的就是 route.rk）自动指向**同 channel** 的 prod rk。
//
// 本包保持渠道无关：channelRoute 只接收 channel 参数，生产代码里不出现任何具体渠道名。
// 具体渠道名只出现在**测试资产**里 —— 就是下面这份跨语言契约向量。
//
// 为什么要向量而不是两边各写各的字面量：TS 和 Python 各自断言
// `chat_response_lark` / `chat.response.lark.{lane}` 的话，「实现和本地 expected 一起
// 被改」或者「CI 只跑了一侧」都会让两边同时变绿，而失效的表现是两个服务静默守着
// 对方不知道的队列 —— 出站消息没人消费，或者两个人都消费。两侧读同一份文件之后，
// 要骗过测试就得改共享的那一份。

import { describe, it, expect, afterEach } from 'bun:test';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import type * as MqClientModule from './client';

// 与 client.test.ts 同样的理由：消费方服务里有测试 mock.module 掉了本模块，而 bun 的
// mock.module 是进程级全局。带 query 的 specifier 是另一个模块 key。
const {
    rabbitmqClient,
    channelRoute,
    laneQueue,
    laneRK,
    CHAT_RESPONSE,
    RECALL,
} = (await import(
    // @ts-expect-error 带 query 的 specifier 只有 bun 运行时认，tsc 解析不到
    '@inner/shared/mq?real'
)) as typeof MqClientModule;

interface AssertedQueue {
    name: string;
    args: Record<string, unknown>;
}
interface Binding {
    queue: string;
    exchange: string;
    rk: string;
}

interface Delivery {
    content: Buffer;
    fields: Record<string, unknown>;
    properties: Record<string, unknown>;
}

interface FakeConsumer {
    tag: string;
    queue: string;
    cb: (msg: Delivery | null) => void;
}

/** deliver 的结果：broker 到底投没投出去，以及没投的原因。 */
type DeliveryOutcome = 'delivered' | 'no-consumer' | 'prefetch-full';

/**
 * 够用的 broker 假体：带 consumerTag、prefetch 窗口和 ack 记账。
 *
 * 只数 Promise 不 ack 的假体证明不了交接是安全的 —— 交接屏障要保证的是「cancel
 * 之后不再有新消息进 handler，且在途的都已经 ack 完」，这两件事都必须有 unacked
 * 计数才能观察。所以这里照 AMQP 的语义做三件事：
 *   1. basic.cancel 之后这个 consumer 就不存在了，broker 不会再投（不是「投了没人接」）；
 *   2. 未 ack 的条数顶到 prefetch 上限时 broker 停投；
 *   3. ack 归还一个 prefetch 名额。
 */
class FakeChannel {
    readonly asserted: AssertedQueue[] = [];
    readonly bindings: Binding[] = [];
    readonly cancelled: string[] = [];
    readonly acked: string[] = [];
    private tagSeq = 0;
    private deliverySeq = 0;
    private prefetchLimit = 10;
    private consumers = new Map<string, FakeConsumer>();
    private unackedByQueue = new Map<string, number>();
    private queueOfDelivery = new Map<string, string>();

    async assertExchange(): Promise<unknown> {
        return {};
    }
    async assertQueue(name: string, options: { arguments?: Record<string, unknown> }) {
        this.asserted.push({ name, args: options?.arguments ?? {} });
        return {};
    }
    async bindQueue(queue: string, exchange: string, rk: string) {
        this.bindings.push({ queue, exchange, rk });
        return {};
    }
    async prefetch(count: number): Promise<unknown> {
        this.prefetchLimit = count;
        return {};
    }
    async consume(queue: string, cb: (msg: Delivery | null) => void) {
        const consumerTag = `tag-${(this.tagSeq += 1)}`;
        this.consumers.set(queue, { tag: consumerTag, queue, cb });
        return { consumerTag };
    }
    async cancel(tag: string) {
        this.cancelled.push(tag);
        for (const [queue, consumer] of this.consumers) {
            if (consumer.tag === tag) this.consumers.delete(queue);
        }
        return {};
    }
    ack(msg: Delivery): void {
        const deliveryTag = String(msg.fields.deliveryTag);
        const queue = this.queueOfDelivery.get(deliveryTag);
        if (!queue) throw new Error(`ack for unknown delivery ${deliveryTag}`);
        this.queueOfDelivery.delete(deliveryTag);
        this.unackedByQueue.set(queue, this.unacked(queue) - 1);
        this.acked.push(deliveryTag);
    }
    nack(): void {}

    /** 把一条消息推给某个队列的消费者回调（不等它跑完）。 */
    deliver(queue: string, body: unknown): DeliveryOutcome {
        const consumer = this.consumers.get(queue);
        // cancel 之后 broker 侧压根没有这个 consumer 了，不存在「投了但没人接」。
        if (!consumer) return 'no-consumer';
        if (this.unacked(queue) >= this.prefetchLimit) return 'prefetch-full';

        const deliveryTag = `d-${(this.deliverySeq += 1)}`;
        this.queueOfDelivery.set(deliveryTag, queue);
        this.unackedByQueue.set(queue, this.unacked(queue) + 1);
        consumer.cb({
            content: Buffer.from(JSON.stringify(body)),
            fields: { deliveryTag, consumerTag: consumer.tag, routingKey: queue },
            properties: {},
        });
        return 'delivered';
    }

    unacked(queue: string): number {
        return this.unackedByQueue.get(queue) ?? 0;
    }

    argsOf(queue: string): Record<string, unknown> {
        const found = this.asserted.find((q) => q.name === queue);
        if (!found) throw new Error(`queue ${queue} was never asserted`);
        return found.args;
    }
}

function injectChannel(channel: FakeChannel): void {
    (rabbitmqClient as unknown as { channel: unknown }).channel = channel;
}

const originalLane = process.env.LANE;

afterEach(() => {
    const singleton = rabbitmqClient as unknown as { channel: unknown; consumers: unknown[] };
    singleton.channel = null;
    singleton.consumers = [];
    if (originalLane === undefined) delete process.env.LANE;
    else process.env.LANE = originalLane;
});

interface ContractCase {
    name: string;
    base: string;
    channel: string;
    lane: string | null;
    expect: {
        queue: string;
        rk: string;
        queue_args: Record<string, unknown>;
    };
}

interface ChannelRouteContract {
    exchanges: { main: string; dead_letter: string };
    base_routes: Record<string, { queue: string; rk: string }>;
    channels: string[];
    lanes: Array<string | null>;
    cases: ContractCase[];
}

// 两侧读的是同一份文件。Python 侧：
// apps/agent-service/tests/unit/infra/test_channel_routes.py
const CONTRACT_PATH = resolve(import.meta.dir, '../../../../contracts/mq-channel-routes.json');
const contract = JSON.parse(readFileSync(CONTRACT_PATH, 'utf8')) as ChannelRouteContract;

/** 契约里的 base key → 本包的 Route 常量。找不到即说明常量被改名了。 */
const BASE_ROUTES: Record<string, MqClientModule.Route> = {
    [CHAT_RESPONSE.queue]: CHAT_RESPONSE,
    [RECALL.queue]: RECALL,
};

function baseRoute(key: string): MqClientModule.Route {
    const route = BASE_ROUTES[key];
    if (!route) {
        throw new Error(
            `contract base_routes has "${key}" but this package exposes ` +
                `[${Object.keys(BASE_ROUTES).join(', ')}]`,
        );
    }
    return route;
}

describe('跨语言契约向量 — 向量自身的完整性', () => {
    it('base_routes 与本包的 Route 常量逐值一致', () => {
        for (const [key, expected] of Object.entries(contract.base_routes)) {
            const route = baseRoute(key);
            expect({ queue: route.queue, rk: route.rk }).toEqual(expected);
        }
    });

    it('cases 覆盖 base × channel × lane 的全组合（不许悄悄缩水）', () => {
        const expectedNames = new Set<string>();
        for (const base of Object.keys(contract.base_routes)) {
            for (const channel of contract.channels) {
                for (const lane of contract.lanes) {
                    expectedNames.add(`${base}|${channel}|${lane ?? 'prod'}`);
                }
            }
        }
        const actual = new Set(
            contract.cases.map((c) => `${c.base}|${c.channel}|${c.lane ?? 'prod'}`),
        );
        expect([...actual].sort()).toEqual([...expectedNames].sort());
    });

    // 向量自身就该守住这条：泳道队列 TTL 到期后按 x-dead-letter-routing-key 弹回
    // prod，必须弹到**同 channel** 的 prod rk。弹到别的 channel 上，回复会由别的
    // 渠道发出去，比不弹严重得多。
    it('每个泳道 case 的 DLX 回落目标 = 同 base 同 channel 的 prod case 的 rk', () => {
        for (const c of contract.cases) {
            if (c.lane === null) continue;
            const prod = contract.cases.find(
                (o) => o.lane === null && o.base === c.base && o.channel === c.channel,
            );
            expect(prod, `no prod case for ${c.base}/${c.channel}`).toBeDefined();
            expect(c.expect.queue_args['x-dead-letter-routing-key']).toBe(prod!.expect.rk);
            expect(c.expect.queue_args['x-dead-letter-exchange']).toBe(contract.exchanges.main);
        }
    });
});

describe('跨语言契约向量 — TS 侧实现对齐', () => {
    for (const c of contract.cases) {
        it(`${c.name}：channelRoute + laneQueue / laneRK 逐值命中`, () => {
            const route = channelRoute(baseRoute(c.base), c.channel);
            expect(laneQueue(route.queue, c.lane ?? undefined)).toBe(c.expect.queue);
            expect(laneRK(route.rk, c.lane ?? undefined)).toBe(c.expect.rk);
        });

        it(`${c.name}：declareRoute 的队列参数与绑定逐值命中`, async () => {
            if (c.lane === null) delete process.env.LANE;
            else process.env.LANE = c.lane;
            const ch = new FakeChannel();
            injectChannel(ch);

            await rabbitmqClient.declareRoute(channelRoute(baseRoute(c.base), c.channel));

            expect(ch.asserted.map((q) => q.name)).toEqual([c.expect.queue]);
            expect(ch.argsOf(c.expect.queue)).toEqual(c.expect.queue_args);
            expect(ch.bindings).toEqual([
                {
                    queue: c.expect.queue,
                    exchange: contract.exchanges.main,
                    rk: c.expect.rk,
                },
            ]);
        });
    }

    it('同一条消息的两个 channel 落到不同队列 / 不同 rk', () => {
        const [first, second] = contract.channels;
        const a = channelRoute(CHAT_RESPONSE, first!);
        const b = channelRoute(CHAT_RESPONSE, second!);
        expect(a.queue).not.toBe(b.queue);
        expect(a.rk).not.toBe(b.rk);
    });
});

describe('drainConsumer — 交接屏障', () => {
    // 交接屏障要保证的是两件事，缺一不可：cancel 之后不再有新消息进 handler；
    // drain 返回时在途（已投递未 ack）确实归零。所以这里把 prefetch 窗口填满、
    // 让 handler 真的 ack，再看 drain 的时序 —— 只数 Promise 的话，「在途归零」
    // 是没被观察过的断言。
    it('prefetch 满窗交接：cancel 后不再进 handler，drain 返回时在途归零', async () => {
        delete process.env.LANE;
        const ch = new FakeChannel();
        injectChannel(ch);
        await ch.prefetch(10);

        let release!: () => void;
        const inHandler = new Promise<void>((resolve) => {
            release = resolve;
        });
        const entered: number[] = [];

        await rabbitmqClient.consume('chat_response_lark', async (msg) => {
            entered.push(entered.length);
            await inHandler;
            // 真的 ack：交接安全性关心的正是「handler 跑完 = ack 已发」。
            rabbitmqClient.ack(msg);
        });

        // 把 prefetch 窗口填满，10 条全部进到 handler 里挂住
        for (let i = 0; i < 10; i += 1) {
            expect(ch.deliver('chat_response_lark', { i })).toBe('delivered');
        }
        expect(entered).toHaveLength(10);
        expect(ch.unacked('chat_response_lark')).toBe(10);
        // 第 11 条这时进不来是因为 prefetch 满了 —— 与下面「因为 cancel 了进不来」
        // 是两件事，先钉住这一件，免得后面那条断言蒙对。
        expect(ch.deliver('chat_response_lark', { i: 10 })).toBe('prefetch-full');

        let drained = false;
        const draining = rabbitmqClient
            .drainConsumer('chat_response_lark', { pollMs: 1 })
            .then(() => {
                drained = true;
            });

        // basic.cancel 立刻发出（停止新投递），但 10 条在途的还没跑完
        await Bun.sleep(10);
        expect(ch.cancelled).toEqual(['tag-1']);
        expect(drained).toBe(false);
        expect(ch.unacked('chat_response_lark')).toBe(10);

        // cancel 之后 broker 侧已经没有这个 consumer 了：第 11 条不会进 handler。
        expect(ch.deliver('chat_response_lark', { i: 11 })).toBe('no-consumer');
        expect(entered).toHaveLength(10);

        release();
        await draining;

        expect(drained).toBe(true);
        expect(entered).toHaveLength(10);
        expect(ch.acked).toHaveLength(10);
        // drain 返回的那一刻在途为零 —— 中间没有消息在飞，交接才是安全的。
        expect(ch.unacked('chat_response_lark')).toBe(0);
    });

    it('drain 过的队列不会在重连时被复活', async () => {
        delete process.env.LANE;
        const ch = new FakeChannel();
        injectChannel(ch);

        await rabbitmqClient.consume('recall_lark', async () => {});
        await rabbitmqClient.drainConsumer('recall_lark', { pollMs: 1 });

        const consumers = (rabbitmqClient as unknown as { consumers: Array<{ queue: string }> })
            .consumers;
        expect(consumers.map((c) => c.queue)).not.toContain('recall_lark');
    });

    it('在途一直不结束时超时报错，不静默卡住', async () => {
        delete process.env.LANE;
        const ch = new FakeChannel();
        injectChannel(ch);

        await rabbitmqClient.consume('chat_response_qq', async () => {
            await new Promise(() => {});
        });
        ch.deliver('chat_response_qq', {});
        await Promise.resolve();

        await expect(
            rabbitmqClient.drainConsumer('chat_response_qq', { pollMs: 1, timeoutMs: 20 }),
        ).rejects.toThrow(/still in flight/);
    });
});
