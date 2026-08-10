// 入站 lane 消费者去重单测（§4.4 point 5）。按 channel + event_type + globalMessageId
// + lane 幂等：命中已处理直接跳过整条入站处理（MQ at-least-once 重投不重复）。
//
// 另一半是切换期的双订阅：队列正在从「只按 lane 分」迁到「按 channel + lane 分」，
// 消费侧要先同时订阅新旧两条队列，生产者才能切（决策九）。

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
// 幂等标记要真的记住**值**，不只是"有没有"：跨服务换手的两个坑全在值上——对面留下的
// "正在处理"被当成"已处理"就是真丢消息。所以这里是一份带值的假 redis，NX 语义照真的
// 来（已存在就返回 null），每个命令前让出一次微任务，好让两个消费者真的交错。
//
// 每条接线测试开头由 laneBroker() 清空。
const redisKeys = new Map<string, string>();
const fakeRedis = {
    incr: async () => 1,
    set: async (key: string, value: string) => {
        await Promise.resolve();
        redisKeys.set(key, value);
        return 'OK' as const;
    },
    setWithExpire: async (key: string, value: string) => {
        await Promise.resolve();
        redisKeys.set(key, value);
        return 'OK' as const;
    },
    get: async (key: string) => {
        await Promise.resolve();
        return redisKeys.get(key) ?? null;
    },
    expire: async () => 1,
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
    del: async (key: string) => {
        await Promise.resolve();
        return redisKeys.delete(key) ? 1 : 0;
    },
    // SET key value EX ttl NX：已存在就不写、返回 null。判断和写入之间不许有 await，
    // 否则这个假的就比真的松，测不出并发。
    setNx: async (key: string, value: string) => {
        await Promise.resolve();
        if (redisKeys.has(key)) return null;
        redisKeys.set(key, value);
        return 'OK' as const;
    },
    evalScript: async () => null,
    exists: async (key: string) => {
        await Promise.resolve();
        return redisKeys.has(key) ? 1 : 0;
    },
    hgetall: async () => ({}),
};
mock.module('@inner/shared/cache', () => ({
    ...realSharedCache,
    getRedisClient: () => fakeRedis,
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
const { createInboundLaneStore } = await import('./inbound-lane-claim');

const env: InboundLaneEnvelope = {
    channel: 'lark',
    event_type: 'im.message.receive_v1',
    global_message_id: 'gmid-42',
    trace_id: 'trace-lane-1',
    lane: 'ppe-foo',
    bot_name: 'chiwei',
    params: { message: { message_id: 'm1' } },
};

/** 这条信封的幂等 key。lark-service 会算出逐字相同的一个（跨服务契约）。 */
const KEY = 'inbound_lane:lark:im.message.receive_v1:gmid-42:ppe-foo';

// 幂等分叉一律跑在**真的 store**（上面那份带值的假 redis）上，不用串行的假 store：
// 换手和并发这两个坑全在协议的值和原子性上，串行的假 store 一个都碰不到。
const store = createInboundLaneStore(() => fakeRedis);

describe('consumeInboundLaneEnvelope（原子占位幂等）', () => {
    it('首次占到位：处理一次，成功后落完成标记', async () => {
        redisKeys.clear();
        let processed = 0;
        const outcome = await consumeInboundLaneEnvelope(env, {
            store,
            process: async () => {
                processed += 1;
            },
        });
        expect(processed).toBe(1);
        expect(outcome).toBe('handled');
        expect(redisKeys.get(KEY)).toBe('done');
    });

    it('已完成的不再处理（不重复处理/回复/副作用）', async () => {
        redisKeys.clear();
        redisKeys.set(KEY, 'done');
        let processed = 0;
        const outcome = await consumeInboundLaneEnvelope(env, {
            store,
            process: async () => {
                processed += 1;
            },
        });
        expect(processed).toBe(0);
        expect(outcome).toBe('already-done');
    });

    // 有人正拿着（或者上一个持有者崩了、租约还没到期）。既不能处理，更不能当成"已处理"
    // ——那会在对方写下完成标记之前把消息 ACK 掉，真丢。
    it('别人正拿着时既不处理也不算已完成', async () => {
        redisKeys.clear();
        redisKeys.set(KEY, 'in-flight');
        let processed = 0;
        const outcome = await consumeInboundLaneEnvelope(env, {
            store,
            process: async () => {
                processed += 1;
            },
        });
        expect(processed).toBe(0);
        expect(outcome).toBe('in-flight');
    });

    it('process 抛错立刻释放占位，重投能重新占到', async () => {
        redisKeys.clear();
        let processed = 0;

        await expect(
            consumeInboundLaneEnvelope(env, {
                store,
                process: async () => {
                    processed += 1;
                    throw new Error('handler down');
                },
            }),
        ).rejects.toThrow('handler down');
        // 占位没还回去的话，重投的那条会看到"有人在处理"，白等一个租约周期。
        expect(redisKeys.has(KEY)).toBe(false);

        await consumeInboundLaneEnvelope(env, {
            store,
            process: async () => {
                processed += 1;
            },
        });
        expect(processed).toBe(2);
        expect(redisKeys.get(KEY)).toBe('done');
    });

    it('process 成功后重投会跳过', async () => {
        redisKeys.clear();
        let processed = 0;
        const count = async () => {
            processed += 1;
        };

        await consumeInboundLaneEnvelope(env, { store, process: count });
        await consumeInboundLaneEnvelope(env, { store, process: count });

        expect(processed).toBe(1);
    });
});

/** 分区前的共享队列。 */
const SHARED_QUEUE = 'inbound_lane.ppe-foo';
/** 本服务在切换窗口内认领的两条分区队列。 */
const LARK_QUEUE = 'inbound_lane.lark.ppe-foo';
const QQ_QUEUE = 'inbound_lane.qq.ppe-foo';

/** 按队列分开记消费者：双订阅时同一条 amqp channel 上挂着好几个。 */
function laneBroker() {
    redisKeys.clear();
    const consumers = new Map<string, (msg: { content: Buffer } | null) => Promise<void>>();
    const asserted: string[] = [];
    const acks: number[] = [];
    const nacks: Array<{ allUpTo: boolean; requeue: boolean }> = [];
    rabbitChannel = {
        assertQueue: async (queue) => void asserted.push(queue),
        prefetch: async () => {},
        consume: async (queue, cb) => {
            consumers.set(queue, cb);
        },
        ack: () => void acks.push(1),
        nack: (_msg, allUpTo, requeue) => void nacks.push({ allUpTo, requeue }),
    };
    return {
        asserted,
        acks,
        nacks,
        subscribed: () => [...consumers.keys()],
        push: async (queue: string, payload: unknown, redelivered = false) => {
            const consumer = consumers.get(queue);
            if (!consumer) throw new Error(`nothing is consuming ${queue}`);
            await consumer({
                content: Buffer.from(JSON.stringify(payload)),
                fields: { redelivered },
            } as never);
        },
    };
}

// lark-service 固定订阅这两条队列（apps/lark-service/src/lark/ingress/lane-queue.test.ts
// 钉了逐字相同的字面量）。两个 app 是两个包，编译期对不上，只能两边各钉一条。
const LARK_SERVICE_SUBSCRIBES = ['inbound_lane.ppe-foo', 'inbound_lane.lark.ppe-foo'];

/** 双订阅窗口里两边都要用的启动参数：拥有全部，不睡觉。 */
const OWNS_EVERYTHING = {
    handles: ['lark', 'qq'],
    loadOwnedChannels: async () => ['lark', 'qq'],
    wait: async () => {},
};

// 双订阅窗口里同一条信封可能被任一方拿到，所以两个服务共享的不只是队列，还有 redis
// 里那批幂等 key。key 的**格式**统一了不够，**协议**也得统一——不然两种换手各错一
// 边，其中一种是真丢消息。
//
// ⚠️ 这里的 'in-flight' / 'done' 是跨服务契约的字面量，lark-service 的
// ingress/lane-queue.test.ts 钉了同样两个。
describe('跨服务换手的幂等协议', () => {
    it('对面正拿着这条消息时绝不 ACK（ACK 掉就是真丢）', async () => {
        const broker = laneBroker();
        const handled: InboundLaneEnvelope[] = [];
        // lark-service 已经占到位、还没写完成标记（正在处理，或者刚崩、租约没到期）。
        redisKeys.set(KEY, 'in-flight');

        await startInboundLaneConsumer('ppe-foo', async (e) => void handled.push(e), {
            ...OWNS_EVERYTHING,
        });
        await broker.push(SHARED_QUEUE, env);

        expect(handled).toEqual([]);
        expect(broker.acks).toHaveLength(0);
        expect(broker.nacks).toEqual([{ allUpTo: false, requeue: true }]);
        rabbitChannel = undefined;
    });

    it('自己处理完之后留下的是对面认得的完成标记', async () => {
        const broker = laneBroker();

        await startInboundLaneConsumer('ppe-foo', async () => {}, { ...OWNS_EVERYTHING });
        await broker.push(SHARED_QUEUE, env);

        // 写别的值（比如 '1'）的话，对面读到会判成"有人正在处理"，于是一直退回队列，
        // 直到 24h 后 TTL 过期——然后整条消息被重新处理一遍。
        expect(redisKeys.get(KEY)).toBe('done');
        expect(broker.acks).toHaveLength(1);
        rabbitChannel = undefined;
    });

    // 两个 Pod（这里是两条 amqp channel 上的两个消费者）共享同一个 redis。"先查有没有
    // 处理过、再处理、再标记"是三步，两边能同时穿过第一步，各执行一遍副作用——用户看
    // 到两条回复。
    it('两个消费者同时拿到同一条信封时只有一个能处理', async () => {
        const handled: string[] = [];
        const a = laneBroker();
        await startInboundLaneConsumer(
            'ppe-foo',
            async () => {
                await Promise.resolve();
                handled.push('a');
            },
            { ...OWNS_EVERYTHING },
        );
        const b = laneBroker();
        await startInboundLaneConsumer(
            'ppe-foo',
            async () => {
                await Promise.resolve();
                handled.push('b');
            },
            { ...OWNS_EVERYTHING },
        );

        await Promise.all([a.push(SHARED_QUEUE, env), b.push(SHARED_QUEUE, env)]);

        expect(handled).toHaveLength(1);
        // 赢家 ACK，输家退回队列等租约——绝不能 ACK。
        expect([...a.acks, ...b.acks]).toHaveLength(1);
        expect([...a.nacks, ...b.nacks]).toEqual([{ allUpTo: false, requeue: true }]);
        rabbitChannel = undefined;
    });
});

describe('cutover 期间「我拥有哪些 channel」的运行期收窄', () => {
    // 决策八要的是"不同 owner 不共享队列"。只按 runtime 注册面推导订阅，收窄就只能靠
    // Task F 删代码——而 cutover 的整个窗口里两边都在抢，分区等于没做。
    it('收窄之后不再跟 lark-service 抢同一条分区队列', async () => {
        const broker = laneBroker();

        await startInboundLaneConsumer('ppe-foo', async () => {}, {
            // 本进程仍然注册着 lark runtime（Task F 之前删不掉）。
            handles: ['lark', 'qq'],
            // 但入站的飞书流量已经移交出去了。
            loadOwnedChannels: async () => ['qq'],
            channelQueueEnabled: async () => true,
        });

        const partitioned = broker.subscribed().filter((q) => q !== SHARED_QUEUE);
        expect(partitioned).toEqual([QQ_QUEUE]);
        expect(partitioned.filter((q) => LARK_SERVICE_SUBSCRIBES.includes(q))).toEqual([]);
        rabbitChannel = undefined;
    });

    // 共享队列是唯一一条两个服务必然同时订阅的队列（旧队列里的存量得有人收干净）。
    // 所以那上面的"不共享"要靠所有权判断兑现：收窄之后飞书的信封一律退回去，等对面
    // 拿走。自己处理掉 = 流量被随机劈成两半，不报错、不留痕。
    it('收窄之后共享队列上的飞书信封退回去，不自己处理', async () => {
        const broker = laneBroker();
        const handled: InboundLaneEnvelope[] = [];

        await startInboundLaneConsumer('ppe-foo', async (e) => void handled.push(e), {
            handles: ['lark', 'qq'],
            loadOwnedChannels: async () => ['qq'],
            channelQueueEnabled: async () => true,
        });
        await broker.push(SHARED_QUEUE, env);

        expect(handled).toEqual([]);
        expect(broker.acks).toHaveLength(0);
        expect(broker.nacks).toEqual([{ allUpTo: false, requeue: true }]);
        rabbitChannel = undefined;
    });

    // 收窄是操作者的显式动作。读不到配置就保持现状，别自作主张把自己变砖。
    it('没配收窄时两个 channel 都认领', async () => {
        const broker = laneBroker();
        const handled: InboundLaneEnvelope[] = [];

        await startInboundLaneConsumer('ppe-foo', async (e) => void handled.push(e), {
            handles: ['lark', 'qq'],
            loadOwnedChannels: async () => ['lark', 'qq'],
            channelQueueEnabled: async () => true,
        });
        await broker.push(SHARED_QUEUE, env);

        expect(broker.subscribed()).toEqual([SHARED_QUEUE, LARK_QUEUE, QQ_QUEUE]);
        expect(handled).toEqual([env]);
        rabbitChannel = undefined;
    });

    // 收窄在分区队列上同样立刻生效：订阅退不掉（要 basic.cancel + 重启），但认领可以
    // 停。停不下来的话，移交那一刻两个服务还在同一条分区队列上分摊流量。
    it('收窄之后连自己已订阅的分区队列上的信封也退回去', async () => {
        const broker = laneBroker();
        const handled: InboundLaneEnvelope[] = [];

        // 起的时候还拥有 lark（所以订了 inbound_lane.lark.ppe-foo），随后被摘掉。
        let owned = ['lark', 'qq'];
        await startInboundLaneConsumer('ppe-foo', async (e) => void handled.push(e), {
            handles: ['lark', 'qq'],
            loadOwnedChannels: async () => owned,
            channelQueueEnabled: async () => true,
        });
        owned = ['qq'];
        await broker.push(LARK_QUEUE, env);

        expect(handled).toEqual([]);
        expect(broker.acks).toHaveLength(0);
        expect(broker.nacks).toEqual([{ allUpTo: false, requeue: true }]);
        rabbitChannel = undefined;
    });
});

describe('startInboundLaneConsumer 失败重投', () => {
    it('消费信封时用 trace_id 重建 context', async () => {
        const broker = laneBroker();
        createdContexts.length = 0;
        let handled: InboundLaneEnvelope | undefined;

        await startInboundLaneConsumer(
            'ppe-foo',
            async (e) => {
                handled = e;
            },
            { handles: ['lark', 'qq'], loadOwnedChannels: async () => ['lark', 'qq'] },
        );
        await broker.push(SHARED_QUEUE, env);

        expect(createdContexts[0]).toEqual({
            botName: 'chiwei',
            traceId: 'trace-lane-1',
            lane: 'ppe-foo',
        });
        expect(handled).toEqual(env);
        rabbitChannel = undefined;
    });

    it('处理抛错时 nack requeue=true，避免消息永久吞掉', async () => {
        const broker = laneBroker();

        await startInboundLaneConsumer(
            'ppe-foo',
            async () => {
                throw new Error('handler down');
            },
            { handles: ['lark', 'qq'], loadOwnedChannels: async () => ['lark', 'qq'] },
        );
        await broker.push(SHARED_QUEUE, env);

        expect(broker.nacks).toEqual([{ allUpTo: false, requeue: true }]);
        rabbitChannel = undefined;
    });
});

describe('startInboundLaneConsumer 分区切换期的双订阅', () => {
    // 开关默认关：镜像可以先上线、先部署，什么时候真订阅新队列是切换动作的一部分。
    it('默认只订阅分区前的共享队列', async () => {
        const broker = laneBroker();
        await startInboundLaneConsumer('ppe-foo', async () => {}, {
            handles: ['lark', 'qq'],
            loadOwnedChannels: async () => ['lark', 'qq'],
        });
        expect(broker.subscribed()).toEqual([SHARED_QUEUE]);
        rabbitChannel = undefined;
    });

    // 决策九：消费侧先双订阅 → 切生产者 → 旧队列排空 → 移交。少了双订阅这一步，
    // 生产者的部署时刻就落在关键路径上：早切没人收，晚切旧队列里的存量没人收。
    it('开关打开后共享队列 + 本服务每个 channel 各订一条', async () => {
        const broker = laneBroker();
        await startInboundLaneConsumer('ppe-foo', async () => {}, {
            handles: ['lark', 'qq'],
            loadOwnedChannels: async () => ['lark', 'qq'],
            channelQueueEnabled: async () => true,
        });
        expect(broker.subscribed()).toEqual([SHARED_QUEUE, LARK_QUEUE, QQ_QUEUE]);
        // 新队列同样 fail-closed 声明（无 TTL、无 DLX，由 assertInboundLaneQueue 保证）。
        expect(broker.asserted).toEqual([SHARED_QUEUE, LARK_QUEUE, QQ_QUEUE]);
        rabbitChannel = undefined;
    });

    // 拆完之后本服务只剩 QQ，订阅面跟着 runtime 收窄，不该还守着飞书那条队列。
    it('只订自己认领的 channel', async () => {
        const broker = laneBroker();
        await startInboundLaneConsumer('ppe-foo', async () => {}, {
            handles: ['qq'],
            loadOwnedChannels: async () => ['qq'],
            channelQueueEnabled: async () => true,
        });
        expect(broker.subscribed()).toEqual([SHARED_QUEUE, QQ_QUEUE]);
        rabbitChannel = undefined;
    });

    it('分区队列上的消息照常处理', async () => {
        const broker = laneBroker();
        const handled: InboundLaneEnvelope[] = [];
        await startInboundLaneConsumer('ppe-foo', async (e) => void handled.push(e), {
            handles: ['lark', 'qq'],
            loadOwnedChannels: async () => ['lark', 'qq'],
            channelQueueEnabled: async () => true,
        });

        await broker.push(LARK_QUEUE, env);

        expect(handled).toEqual([env]);
        expect(broker.acks).toHaveLength(1);
        rabbitChannel = undefined;
    });

    // 双订阅期间最要命的一条：去重认的是信封，不是它从哪条队列来的。认队列的话，旧
    // 队列里的存量和新队列里的同一条消息会被处理两遍 —— 用户看到两条回复。
    it('同一条信封走哪条队列都只处理一次', async () => {
        const broker = laneBroker();
        const handled: InboundLaneEnvelope[] = [];
        await startInboundLaneConsumer('ppe-foo', async (e) => void handled.push(e), {
            handles: ['lark', 'qq'],
            loadOwnedChannels: async () => ['lark', 'qq'],
            channelQueueEnabled: async () => true,
        });

        await broker.push(SHARED_QUEUE, env);
        await broker.push(LARK_QUEUE, env);

        expect(handled).toEqual([env]);
        // 第二条也要 ACK：它是重复，不是失败，留在队列里只会一直重投。
        expect(broker.acks).toHaveLength(2);
        rabbitChannel = undefined;
    });

    // 决策八：分区做完之后"不是我的信封"这条校验退化成断言，不是被删掉。分区队列上
    // 出现别人的信封 = 投递侧发错了队列，而这条队列没有第二个消费者，退回去只会永远
    // 弹；prefetch=1 会让它把整条泳道堵死。所以丢，但要吼出来。
    it('分区队列上收到别人的信封时丢掉而不是重投', async () => {
        const broker = laneBroker();
        const handled: InboundLaneEnvelope[] = [];
        await startInboundLaneConsumer('ppe-foo', async (e) => void handled.push(e), {
            handles: ['lark', 'qq'],
            loadOwnedChannels: async () => ['lark', 'qq'],
            channelQueueEnabled: async () => true,
        });

        await broker.push(QQ_QUEUE, env); // env 是飞书的信封

        expect(handled).toEqual([]);
        expect(broker.acks).toHaveLength(0);
        expect(broker.nacks).toEqual([{ allUpTo: false, requeue: false }]);
        rabbitChannel = undefined;
    });

    // 共享队列没有这个不变量：它本来就装着两个渠道的信封，本服务在切换窗口内两个都
    // 认领，按信封自己的 channel 分发。
    it('共享队列上不做归属断言，按信封的 channel 分发', async () => {
        const broker = laneBroker();
        const handled: InboundLaneEnvelope[] = [];
        await startInboundLaneConsumer('ppe-foo', async (e) => void handled.push(e), {
            handles: ['lark', 'qq'],
            loadOwnedChannels: async () => ['lark', 'qq'],
            channelQueueEnabled: async () => true,
        });

        await broker.push(SHARED_QUEUE, { ...env, channel: 'qq' });

        expect(handled).toHaveLength(1);
        expect(broker.acks).toHaveLength(1);
        rabbitChannel = undefined;
    });

    // 老信封没有 channel 字段，那个年代只有飞书在用这个队列 —— 落在飞书的分区队列上
    // 不算越界。
    it('没有 channel 字段的老信封算飞书的', async () => {
        const broker = laneBroker();
        const handled: InboundLaneEnvelope[] = [];
        await startInboundLaneConsumer('ppe-foo', async (e) => void handled.push(e), {
            handles: ['lark'],
            loadOwnedChannels: async () => ['lark'],
            channelQueueEnabled: async () => true,
        });

        const legacy = { ...env };
        delete legacy.channel;
        await broker.push(LARK_QUEUE, legacy);

        expect(handled).toHaveLength(1);
        expect(broker.acks).toHaveLength(1);
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
