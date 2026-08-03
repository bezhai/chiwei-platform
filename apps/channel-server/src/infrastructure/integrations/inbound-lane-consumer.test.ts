// 入站 lane 消费者去重单测（§4.4 point 5）。按 event_type + globalMessageId + lane
// 三元组幂等：命中已处理直接跳过整条入站处理（MQ at-least-once 重投不重复）。

import { describe, it, expect, mock, afterAll } from 'bun:test';
import type { InboundLaneEnvelope } from './inbound-lane';

// consumeInboundLaneEnvelope 是纯函数（注入 acquire/process），但它与接线函数
// startInboundLaneConsumer 同模块，后者静态 import 了真实 setNx/getRabbitChannel/
// context。bun mock.module 是进程级全局：若本测试加载真实 @inner/shared/cache，会
// 污染同进程其他测试的 redis mock（让它们误连真 redis → ECONNREFUSED）。故这里把
// 这三个真实副作用依赖 mock 掉，再动态 import 纯函数。
// Redis 收敛成 @inner/shared/cache 的 RedisClient 单例后，这里桩的是
// getRedisClient()。bun 的 mock.module 是**整模块替换 + 进程级全局**，只写
// getRedisClient 会把同模块的 cache / RedisClient 等导出一并抹掉，
// 别的测试文件跟着遭殃；所以先抓真身、只覆盖这一个导出，afterAll 再注回去。
const realSharedCache = { ...(await import('@inner/shared/cache')) };
const redisStub = {
    incr: async () => 1,
    set: async () => 'OK',
    setWithExpire: async () => 'OK',
    get: async () => null,
    publish: async () => 0,
    subscribe: async () => {},
    unsubscribe: async () => {},
    psubscribe: async () => {},
    punsubscribe: async () => {},
    close: async () => {},
    xadd: async () => '1-0',
    xread: async () => null,
    xdel: async () => 0,
    xgroup: async () => 'OK',
    xreadgroup: async () => null,
    xack: async () => 0,
    del: async () => 0,
    setNx: async () => 'OK',
    evalScript: async () => null,
    exists: async () => 0,
    hgetall: async () => ({}),
};
mock.module('@inner/shared/cache', () => ({
    ...realSharedCache,
    getRedisClient: () => redisStub,
}));
let rabbitChannel:
    | {
          assertQueue: (queue: string, opts: unknown) => Promise<void>;
          prefetch: (count: number) => Promise<void>;
          consume: (
              queue: string,
              cb: (msg: { content: Buffer } | null) => Promise<void>,
          ) => Promise<void>;
          ack: (msg: unknown) => void;
          nack: (msg: unknown, allUpTo: boolean, requeue: boolean) => void;
      }
    | undefined;
const createdContexts: Array<{ botName?: string; traceId?: string; lane?: string }> = [];
// 同 @inner/shared/cache 的道理：整模块替换会把 mq 模块里没列出的导出（连接管理、
// 拓扑声明等）一并抹掉，而 @inner/shared/mq 是全服务共用的。抓真身、只覆盖本文件
// 要控的那几个，afterAll 注回去。
const realSharedMq = { ...(await import('@inner/shared/mq')) };
mock.module('@inner/shared/mq', () => ({
    ...realSharedMq,
    getLane: () => undefined,
    rabbitmqClient: {
        connect: async () => {},
        declareTopology: async () => {},
        publish: async () => {},
        consume: async () => {},
        ack: () => {},
        nack: () => {},
        getChannel: () => {
            throw new Error('not used in unit test');
        },
        close: async () => {},
    },
    getRabbitChannel: () => {
        if (!rabbitChannel) throw new Error('not used in unit test');
        return rabbitChannel;
    },
}));
// 本文件只想窥探 createContext 的入参，不想改它的语义。整模块替换会抹掉
// asyncLocalStorage 以及 context 上的 get/set/run/getBotName —— 后跑的测试文件
// （bot-var、bot-identity 等）会拿到这个残缺桩。所以真身 spread + 只包一层记录，
// afterAll 注回去。
const realContextModule = { ...(await import('@middleware/context')) };
mock.module('@middleware/context', () => ({
    ...realContextModule,
    context: {
        ...realContextModule.context,
        createContext: (botName?: string, traceId?: string, lane?: string) => {
            const ctx = realContextModule.context.createContext(botName, traceId, lane);
            createdContexts.push(ctx);
            return ctx;
        },
    },
}));

const { consumeInboundLaneEnvelope, startInboundLaneConsumer } =
    await import('./inbound-lane-consumer');

const env: InboundLaneEnvelope = {
    channel: 'lark',
    event_type: 'im.message.receive_v1',
    global_message_id: 'gmid-42',
    trace_id: 'trace-lane-1',
    lane: 'ppe-foo',
    bot_name: 'chiwei',
    params: { message: { message_id: 'm1' } },
};

describe('consumeInboundLaneEnvelope（三元组幂等）', () => {
    it('首次三元组：未完成 → 调用入站处理一次，成功后写完成标记', async () => {
        let processed = 0;
        let markedKey = '';
        await consumeInboundLaneEnvelope(env, {
            isProcessed: async () => false,
            markProcessed: async (key) => {
                markedKey = key;
            },
            process: async () => {
                processed += 1;
            },
        });
        expect(processed).toBe(1);
        expect(markedKey).toBe('inbound_lane:im.message.receive_v1:gmid-42:ppe-foo');
    });

    it('重复三元组：已完成 → 不再调用入站处理（不重复处理/回复/副作用）', async () => {
        let processed = 0;
        let marked = false;
        await consumeInboundLaneEnvelope(env, {
            isProcessed: async () => true,
            markProcessed: async () => {
                marked = true;
            },
            process: async () => {
                processed += 1;
            },
        });
        expect(processed).toBe(0);
        expect(marked).toBe(false);
    });

    it('process 抛错不写完成态，同一三元组重投会重新 process', async () => {
        const completed = new Set<string>();
        let processed = 0;

        await expect(
            consumeInboundLaneEnvelope(env, {
                isProcessed: async (key) => completed.has(key),
                markProcessed: async (key) => {
                    completed.add(key);
                },
                process: async () => {
                    processed += 1;
                    throw new Error('handler down');
                },
            }),
        ).rejects.toThrow('handler down');

        await expect(
            consumeInboundLaneEnvelope(env, {
                isProcessed: async (key) => completed.has(key),
                markProcessed: async (key) => {
                    completed.add(key);
                },
                process: async () => {
                    processed += 1;
                    throw new Error('handler down again');
                },
            }),
        ).rejects.toThrow('handler down again');

        expect(processed).toBe(2);
        expect(completed.size).toBe(0);
    });

    it('process 成功后写完成态，同一三元组重投会跳过', async () => {
        const completed = new Set<string>();
        let processed = 0;

        await consumeInboundLaneEnvelope(env, {
            isProcessed: async (key) => completed.has(key),
            markProcessed: async (key) => {
                completed.add(key);
            },
            process: async () => {
                processed += 1;
            },
        });
        await consumeInboundLaneEnvelope(env, {
            isProcessed: async (key) => completed.has(key),
            markProcessed: async (key) => {
                completed.add(key);
            },
            process: async () => {
                processed += 1;
            },
        });

        expect(processed).toBe(1);
        expect(completed.has('inbound_lane:im.message.receive_v1:gmid-42:ppe-foo')).toBe(true);
    });
});

describe('startInboundLaneConsumer 失败重投', () => {
    it('消费信封时用 trace_id 重建 context', async () => {
        let consumeCallback: ((msg: { content: Buffer } | null) => Promise<void>) | undefined;
        rabbitChannel = {
            assertQueue: async () => {},
            prefetch: async () => {},
            consume: async (_queue, cb) => {
                consumeCallback = cb;
            },
            ack: () => {},
            nack: () => {},
        };
        createdContexts.length = 0;
        let handled: InboundLaneEnvelope | undefined;

        await startInboundLaneConsumer('ppe-foo', async (e) => {
            handled = e;
        });
        await consumeCallback!({
            content: Buffer.from(JSON.stringify(env)),
        });

        expect(createdContexts[0]).toEqual({
            botName: 'chiwei',
            traceId: 'trace-lane-1',
            lane: 'ppe-foo',
        });
        expect(handled).toEqual(env);
        rabbitChannel = undefined;
    });

    it('处理抛错时 nack requeue=true，避免消息永久吞掉', async () => {
        let consumeCallback: ((msg: { content: Buffer } | null) => Promise<void>) | undefined;
        const nacks: Array<{ allUpTo: boolean; requeue: boolean }> = [];
        rabbitChannel = {
            assertQueue: async () => {},
            prefetch: async () => {},
            consume: async (_queue, cb) => {
                consumeCallback = cb;
            },
            ack: () => {},
            nack: (_msg, allUpTo, requeue) => {
                nacks.push({ allUpTo, requeue });
            },
        };

        await startInboundLaneConsumer('ppe-foo', async () => {
            throw new Error('handler down');
        });
        expect(consumeCallback).toBeDefined();

        await consumeCallback!({
            content: Buffer.from(JSON.stringify(env)),
        });

        expect(nacks).toEqual([{ allUpTo: false, requeue: true }]);
        rabbitChannel = undefined;
    });
});

// 本文件桩了三个多消费者模块，三个都要注回真身。漏掉任何一个，后加载的测试文件
// 拿到的"真身"其实还是本文件的桩——包括那些自以为在 spread 真身的文件，它们
// spread 到的会是这里留下的残缺版本，污染就这样一路传下去。
afterAll(() => {
    mock.module('@inner/shared/cache', () => realSharedCache);
    mock.module('@inner/shared/mq', () => realSharedMq);
    mock.module('@middleware/context', () => realContextModule);
});
