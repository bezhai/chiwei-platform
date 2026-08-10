import { describe, expect, it } from 'bun:test';

import {
    CLAIM_DONE,
    CLAIM_IN_FLIGHT,
    createLaneClaimStore,
    type InboundLaneStore,
    type LaneClaim,
    type LaneRedis,
} from './lane-claim';
import { UnprocessableLarkEvent, type LarkEvent } from './lark-event';
import {
    INBOUND_LANE_CHANNEL_CONSUME_FLAG,
    inboundLaneDedupeKey,
    inboundLaneQueueName,
    sharedInboundLaneQueueName,
    startInboundLaneConsumer,
    type InboundLaneEnvelope,
    type LaneChannel,
    type LaneConsumerScope,
} from './lane-queue';

const SCOPE: LaneConsumerScope = {
    channel: 'lark',
    lane: 'ppe-x',
    handles: (type) => type === 'im.message.receive_v1',
};

/** 分区前的队列：QQ 和飞书的信封混在一起。 */
const SHARED_QUEUE = 'inbound_lane.ppe-x';
/** 分区后的队列：只有飞书的信封。 */
const CHANNEL_QUEUE = 'inbound_lane.lark.ppe-x';

function envelope(overrides: Partial<InboundLaneEnvelope> = {}): InboundLaneEnvelope {
    return {
        channel: 'lark',
        event_type: 'im.message.receive_v1',
        global_message_id: 'cm_1',
        trace_id: 'trace-1',
        lane: 'ppe-x',
        bot_name: 'chiwei',
        params: { message: { message_id: 'om_1' } },
        ...overrides,
    };
}

type LaneMessageHandler = (
    msg: { content: Buffer; fields?: { redelivered?: boolean } } | null,
) => Promise<void>;

function fakeAmqp() {
    const asserted: Array<{ queue: string; options: unknown }> = [];
    const acked: string[] = [];
    const nacked: Array<{ id: string; requeue: boolean }> = [];
    let prefetched = 0;
    // 双订阅期间同一条 amqp channel 上挂着两个消费者，按队列名分开记，才能分别投递。
    const consumers = new Map<string, LaneMessageHandler>();

    const amqp: LaneChannel = {
        assertQueue: async (queue, options) => void asserted.push({ queue, options }),
        prefetch: async (count) => {
            prefetched = count;
        },
        consume: async (queue, handler) => {
            consumers.set(queue, handler);
            return {};
        },
        ack: (msg) => void acked.push((msg as { id: string }).id),
        nack: (msg, _allUpTo, requeue) =>
            void nacked.push({ id: (msg as { id: string }).id, requeue }),
    };

    const pushTo = async (queue: string, id: string, env: unknown, redelivered = false) => {
        const handler = consumers.get(queue);
        if (!handler) throw new Error(`nothing is consuming ${queue}`);
        await handler({
            content: Buffer.from(JSON.stringify(env)),
            fields: { redelivered },
            id,
        } as never);
    };

    return {
        amqp,
        asserted,
        acked,
        nacked,
        pushTo,
        push: (id: string, env: unknown, redelivered = false) =>
            pushTo(SHARED_QUEUE, id, env, redelivered),
        subscribed: () => [...consumers.keys()],
        get prefetched() {
            return prefetched;
        },
    };
}

function fakeStore(initial: Record<string, 'in-flight' | 'done'> = {}) {
    const keys = new Map<string, 'in-flight' | 'done'>(Object.entries(initial));
    const calls: string[] = [];
    return {
        keys,
        calls,
        claim: async (key: string): Promise<LaneClaim> => {
            calls.push(`claim:${key}`);
            const held = keys.get(key);
            if (held) return held;
            keys.set(key, 'in-flight');
            return 'claimed';
        },
        complete: async (key: string) => {
            calls.push(`complete:${key}`);
            keys.set(key, 'done');
        },
        release: async (key: string) => {
            calls.push(`release:${key}`);
            keys.delete(key);
        },
    };
}

/** 一条调用流水，用来断言"根本没去认领别人的消息"。 */
type SpyStore = InboundLaneStore & { calls: string[] };

/** 给真 store 套一层流水记录，行为不变。 */
function spyOn(store: InboundLaneStore): SpyStore {
    const calls: string[] = [];
    return {
        calls,
        claim: async (key) => {
            calls.push(`claim:${key}`);
            return store.claim(key);
        },
        complete: async (key) => {
            calls.push(`complete:${key}`);
            return store.complete(key);
        },
        release: async (key) => {
            calls.push(`release:${key}`);
            return store.release(key);
        },
    };
}

async function start(
    deliver: (event: LarkEvent) => Promise<void>,
    options: {
        store?: SpyStore;
        scope?: LaneConsumerScope;
        /** 是否订阅按 channel 分区的新队列。生产默认关。 */
        channelQueue?: boolean;
    } = {},
) {
    const amqp = fakeAmqp();
    const store = options.store ?? fakeStore();
    const waits: number[] = [];
    await startInboundLaneConsumer(options.scope ?? SCOPE, deliver, {
        amqp: amqp.amqp,
        store,
        wait: async (ms) => void waits.push(ms),
        ...(options.channelQueue === undefined
            ? {}
            : { channelQueueEnabled: async () => options.channelQueue! }),
    });
    return { amqp, store, waits };
}

/**
 * 两个 Pod 共享的一份 redis。channel-server 和本服务在双订阅窗口里读写的就是这么一批
 * key，所以这里的假 redis 要有值、NX 要原子、每条命令要让出一次微任务。
 */
function sharedRedis(initial: Record<string, string> = {}) {
    const keys = new Map<string, string>(Object.entries(initial));
    const redis: LaneRedis = {
        setNx: async (key, value) => {
            await Promise.resolve();
            if (keys.has(key)) return null;
            keys.set(key, value);
            return 'OK';
        },
        get: async (key) => {
            await Promise.resolve();
            return keys.get(key) ?? null;
        },
        setWithExpire: async (key, value) => {
            await Promise.resolve();
            keys.set(key, value);
            return 'OK';
        },
        del: async (...del) => {
            await Promise.resolve();
            return del.filter((key) => keys.delete(key)).length;
        },
    };
    return { keys, store: spyOn(createLaneClaimStore(() => redis)) };
}

// 双订阅窗口里同一条信封可能被 channel-server 拿到、也可能被本服务拿到，两边共享的不
// 只是队列，还有 redis 里那批幂等 key。所以统一 key 的格式不够，协议也得统一。
describe('跨服务换手', () => {
    const KEY = inboundLaneDedupeKey(envelope());

    // 对面处理完了。这条消息不该再处理，但要 ACK —— 它是重复，不是失败，留在队列里
    // 只会一直重投。
    it('acknowledges a message the other service already finished', async () => {
        const shared = sharedRedis({ [KEY]: CLAIM_DONE });
        const delivered: LarkEvent[] = [];
        const { amqp } = await start(async (e) => void delivered.push(e), { store: shared.store });

        await amqp.push('m1', envelope());

        expect(delivered).toEqual([]);
        expect(amqp.acked).toEqual(['m1']);
    });

    // 对面正拿着（还没写完成标记）。ACK 会在那之前把消息销毁 —— 那是真丢，不是重复。
    it('never acknowledges a message the other service is still holding', async () => {
        const shared = sharedRedis({ [KEY]: CLAIM_IN_FLIGHT });
        const delivered: LarkEvent[] = [];
        const { amqp } = await start(async (e) => void delivered.push(e), { store: shared.store });

        await amqp.push('m1', envelope());

        expect(delivered).toEqual([]);
        expect(amqp.acked).toEqual([]);
        expect(amqp.nacked).toEqual([{ id: 'm1', requeue: true }]);
    });

    // 两个消费者同时拿到同一条信封（一条从共享队列来、一条从分区队列来，或者干脆是两
    // 个 Pod）。"先查有没有处理过、再处理、再标记"是三步，两边能同时穿过第一步，各回
    // 一条消息。
    it('processes a message once even when two consumers get it at the same time', async () => {
        const shared = sharedRedis();
        const delivered: string[] = [];
        const a = await start(
            async () => {
                await Promise.resolve();
                delivered.push('a');
            },
            { store: shared.store },
        );
        const b = await start(
            async () => {
                await Promise.resolve();
                delivered.push('b');
            },
            { store: shared.store },
        );

        await Promise.all([a.amqp.push('m1', envelope()), b.amqp.push('m2', envelope())]);

        expect(delivered).toHaveLength(1);
        // 赢家 ACK，输家退回队列等租约——绝不能 ACK。
        expect([...a.amqp.acked, ...b.amqp.acked]).toHaveLength(1);
        expect([...a.amqp.nacked, ...b.amqp.nacked]).toEqual([
            { id: expect.any(String), requeue: true },
        ]);
    });
});

describe('inboundLaneQueueName', () => {
    // 队列的分区维度必须和消费者的所有权维度一致。owner 是 channel + lane，所以队列
    // 名也是 —— 不然 QQ 的信封和飞书的信封躺在同一个队列里被两个服务竞争消费。
    //
    // ⚠️ 这个字面量是**跨服务契约**：channel-server 的 inbound-lane.ts 必须拼出逐字
    // 相同的名字（那边有一条同样写死字面量的测试）。两个 app 是两个包，编译期对不上，
    // 只能靠两边各钉一条。
    it('names one queue per channel and lane', () => {
        expect(inboundLaneQueueName('lark', 'ppe-x')).toBe('inbound_lane.lark.ppe-x');
    });

    it('tells two channels on the same lane apart', () => {
        expect(inboundLaneQueueName('qq', 'ppe-x')).not.toBe(
            inboundLaneQueueName('lark', 'ppe-x'),
        );
    });

    // 分区前的名字还得留着：切换期间消费侧要同时订阅新旧两条队列（否则生产者切过去
    // 之前的消息没人收，或者切过去之后旧队列里的存量没人收）。
    it('keeps the pre-partition name for the cutover window', () => {
        expect(sharedInboundLaneQueueName('ppe-x')).toBe('inbound_lane.ppe-x');
    });
});

// ⚠️ 同样是跨服务契约：channel-server 的 inbound-lane-flag.ts 用同名 key（那边有一条
// 同样写死字面量的断言）。改名只改一边的症状是切换期间一个服务订了新队列、另一个没订。
describe('INBOUND_LANE_CHANNEL_CONSUME_FLAG', () => {
    it('is the same dynamic config key channel-server reads', () => {
        expect(INBOUND_LANE_CHANNEL_CONSUME_FLAG).toBe('enable_inbound_lane_channel_consume');
    });
});

describe('inboundLaneDedupeKey', () => {
    // 队列按 lane 分区，但 owner 其实按 channel + lane 分区，所以幂等 key 也必须带上
    // channel —— 否则飞书和 QQ 的同名事件会互相顶掉对方的完成标记。
    it('identifies one processing of one event, per channel and lane', () => {
        expect(inboundLaneDedupeKey(envelope())).toBe(
            'inbound_lane:lark:im.message.receive_v1:cm_1:ppe-x',
        );
    });

    it('tells the same message on two lanes apart', () => {
        expect(inboundLaneDedupeKey(envelope({ lane: 'ppe-a' }))).not.toBe(
            inboundLaneDedupeKey(envelope({ lane: 'ppe-b' })),
        );
    });

    it('tells the same message on two channels apart', () => {
        expect(inboundLaneDedupeKey(envelope({ channel: 'qq' }))).not.toBe(
            inboundLaneDedupeKey(envelope({ channel: 'lark' })),
        );
    });
});

describe('startInboundLaneConsumer', () => {
    // 这个队列**绝不能**配 TTL + 死信回 prod：装在里面的是"已经判定该在这条泳道处理"
    // 的消息，过期跑回 prod 就是拿泳道的改动去污染线上。
    it('declares a durable queue that never expires messages back to prod', async () => {
        const { amqp } = await start(async () => {});
        expect(amqp.asserted).toEqual([{ queue: 'inbound_lane.ppe-x', options: { durable: true } }]);
        expect(amqp.prefetched).toBe(1);
    });

    describe('subscribing across the partition cutover', () => {
        // 开关默认关：镜像可以先上线、先部署，什么时候真订阅新队列是切换动作的一部分。
        it('only subscribes to the shared queue by default', async () => {
            const { amqp } = await start(async () => {});
            expect(amqp.subscribed()).toEqual([SHARED_QUEUE]);
        });

        // 决策九：消费侧先双订阅 → 切生产者 → 旧队列排空 → drain 屏障移交。少了双订阅
        // 这一步，生产者的部署时刻就落在关键路径上：早切没人收，晚切旧队列里的没人收。
        it('subscribes to both queues once the channel queue is turned on', async () => {
            const { amqp } = await start(async () => {}, { channelQueue: true });
            expect(amqp.subscribed()).toEqual([SHARED_QUEUE, CHANNEL_QUEUE]);
        });

        // 新队列同样不配 TTL / DLX：装在里面的是"已经判定该在这条泳道处理"的消息，
        // 过期跑回 prod 就是拿泳道的改动污染线上。
        it('declares the channel queue fail-closed, same as the shared one', async () => {
            const { amqp } = await start(async () => {}, { channelQueue: true });
            expect(amqp.asserted).toEqual([
                { queue: SHARED_QUEUE, options: { durable: true } },
                { queue: CHANNEL_QUEUE, options: { durable: true } },
            ]);
        });

        it('handles a message that arrives on the channel queue', async () => {
            const delivered: LarkEvent[] = [];
            const { amqp } = await start(async (e) => void delivered.push(e), {
                channelQueue: true,
            });

            await amqp.pushTo(CHANNEL_QUEUE, 'm1', envelope());

            expect(delivered).toHaveLength(1);
            expect(amqp.acked).toEqual(['m1']);
        });

        // 双订阅期间最要命的一条：去重认的是信封，不是它从哪条队列来的。认队列的话，
        // 旧队列里的存量和新队列里的同一条消息会被处理两遍 —— 用户看到两条回复。
        it('processes the same message once no matter which queue carried it', async () => {
            const delivered: LarkEvent[] = [];
            const { amqp } = await start(async (e) => void delivered.push(e), {
                channelQueue: true,
            });

            await amqp.pushTo(SHARED_QUEUE, 'old', envelope());
            await amqp.pushTo(CHANNEL_QUEUE, 'new', envelope());

            expect(delivered).toHaveLength(1);
            // 第二条也要 ACK：它是重复，不是失败，留在队列里只会一直重投。
            expect(amqp.acked).toEqual(['old', 'new']);
        });

        // 决策八：分区做完之后"不是我的信封就退回"这条校验退化成断言，不是被删掉。
        // 分区队列上出现别人的信封 = 投递侧发错了队列，而这条队列没有第二个消费者，
        // 退回去只会永远弹；prefetch=1 会让它把整条泳道堵死。所以丢，但要吼出来。
        it('refuses to requeue a foreign envelope on the partitioned queue', async () => {
            const delivered: LarkEvent[] = [];
            const { amqp, store } = await start(async (e) => void delivered.push(e), {
                channelQueue: true,
            });

            await amqp.pushTo(CHANNEL_QUEUE, 'm1', envelope({ channel: 'qq' }));

            expect(delivered).toEqual([]);
            expect(amqp.acked).toEqual([]);
            expect(amqp.nacked).toEqual([{ id: 'm1', requeue: false }]);
            expect(store.calls).toEqual([]);
        });

        // 同一条信封，落在共享队列上就还是交接（对面还在订阅那条队列）。同一段校验，
        // 两条队列上两种结论——差别在于"还有没有别人可能来收"。
        it('still hands a foreign envelope back on the shared queue', async () => {
            const { amqp } = await start(async () => {}, { channelQueue: true });

            await amqp.pushTo(SHARED_QUEUE, 'm1', envelope({ channel: 'qq' }));

            expect(amqp.nacked).toEqual([{ id: 'm1', requeue: true }]);
        });
    });

    it('rebuilds the event from the envelope, lane and all', async () => {
        const delivered: LarkEvent[] = [];
        const { amqp } = await start(async (e) => void delivered.push(e));

        await amqp.push('m1', envelope());

        expect(delivered).toEqual([
            {
                type: 'im.message.receive_v1',
                payload: { message: { message_id: 'om_1' } },
                botName: 'chiwei',
                traceId: 'trace-1',
                lane: 'ppe-x',
            },
        ]);
        expect(amqp.acked).toEqual(['m1']);
    });

    describe('messages that belong to another channel', () => {
        // inbound_lane.{lane} 是**按 lane 分区的队列，而 owner 实际按 channel + lane
        // 分区** —— QQ 的信封和飞书的信封躺在同一个队列里，两个服务竞争消费。抢到别人
        // 的信封时 ACK 就等于把它吃掉，对面永远收不到。
        it('never acknowledges a message it does not own', async () => {
            const delivered: LarkEvent[] = [];
            const { amqp, store } = await start(async (e) => void delivered.push(e));

            await amqp.push('m1', envelope({ channel: 'qq' }));

            expect(delivered).toEqual([]);
            expect(amqp.acked).toEqual([]);
            expect(amqp.nacked).toEqual([{ id: 'm1', requeue: true }]);
            // 更要紧的是别去认领它：认领了就等于替对面写下"这条处理过了"。
            expect(store.calls).toEqual([]);
        });

        // 第一次拿到别人的信封就立刻退回去，让对面的消费者接手 —— 对面在线时这一下就
        // 交接完了，不该有任何延迟成本。
        it('hands a foreign message straight back on first sight', async () => {
            const { amqp, waits } = await start(async () => {});
            await amqp.push('m1', envelope({ channel: 'qq' }), false);
            expect(waits).toEqual([]);
            expect(amqp.nacked).toEqual([{ id: 'm1', requeue: true }]);
        });

        // 又回到自己手上，说明对面不在线（或者被随机分回来了）。这时必须放慢，否则就是
        // 一个纯烧 CPU 的活锁。放慢不放弃：消息一直留在队列里等它的主人上线。
        it('slows down when a foreign message keeps coming back', async () => {
            const { amqp, waits } = await start(async () => {});
            await amqp.push('m1', envelope({ channel: 'qq' }), true);
            expect(waits).toHaveLength(1);
            expect(waits[0]).toBeGreaterThan(0);
            expect(amqp.nacked).toEqual([{ id: 'm1', requeue: true }]);
        });

        // 老信封没有 channel 字段。那个年代只有飞书在用这个队列，按飞书算。
        it('treats an envelope with no channel as its own', async () => {
            const delivered: LarkEvent[] = [];
            const { amqp } = await start(async (e) => void delivered.push(e));
            const legacy = envelope();
            delete legacy.channel;

            await amqp.push('m1', legacy);

            expect(delivered).toHaveLength(1);
            expect(amqp.acked).toEqual(['m1']);
        });
    });

    describe('claiming', () => {
        // 先占位再处理。反过来（查一下没处理过 → 处理 → 标记）是三步，两个 Pod 能同时
        // 穿过第一步，各执行一遍副作用。
        it('claims the message before doing any work', async () => {
            const store = fakeStore();
            const order: string[] = [];
            const { amqp } = await start(async () => void order.push('deliver'), { store });

            await amqp.push('m1', envelope());

            expect(store.calls[0]).toMatch(/^claim:/);
            expect(order).toEqual(['deliver']);
            expect(store.calls[store.calls.length - 1]).toMatch(/^complete:/);
        });

        it('skips and acknowledges something already finished', async () => {
            const key = inboundLaneDedupeKey(envelope());
            const store = fakeStore({ [key]: 'done' });
            const delivered: LarkEvent[] = [];
            const { amqp } = await start(async (e) => void delivered.push(e), { store });

            await amqp.push('m1', envelope());

            expect(delivered).toEqual([]);
            expect(amqp.acked).toEqual(['m1']);
        });

        // 另一个消费者正拿着它（或者上一个持有者崩了、租约还没到期）。绝不能 ACK ——
        // 那会在对方还没写下完成标记时把消息销毁。退回队列，等租约到期后重来。
        it('requeues rather than acknowledges while someone else holds the claim', async () => {
            const key = inboundLaneDedupeKey(envelope());
            const store = fakeStore({ [key]: 'in-flight' });
            const delivered: LarkEvent[] = [];
            const { amqp, waits } = await start(async (e) => void delivered.push(e), { store });

            await amqp.push('m1', envelope(), true);

            expect(delivered).toEqual([]);
            expect(amqp.acked).toEqual([]);
            expect(amqp.nacked).toEqual([{ id: 'm1', requeue: true }]);
            expect(waits).toHaveLength(1); // 别原地空转
        });

        // 处理失败要立刻把占位还回去，否则重投的那一条会看到"有人在处理"，白等一个
        // 租约周期。
        it('releases the claim when processing fails', async () => {
            const store = fakeStore();
            const key = inboundLaneDedupeKey(envelope());
            const { amqp } = await start(
                async () => {
                    throw new Error('database is down');
                },
                { store },
            );

            await amqp.push('m1', envelope());

            expect(store.calls).toEqual([`claim:${key}`, `release:${key}`]);
            expect(amqp.acked).toEqual([]);
            expect(amqp.nacked).toEqual([{ id: 'm1', requeue: true }]);
        });

        it('lets the retry claim it again after a failure', async () => {
            const store = fakeStore();
            let attempts = 0;
            const { amqp } = await start(
                async () => {
                    attempts += 1;
                    if (attempts === 1) throw new Error('transient');
                },
                { store },
            );

            await amqp.push('m1', envelope());
            await amqp.push('m1', envelope(), true);

            expect(attempts).toBe(2);
            expect(amqp.acked).toEqual(['m1']);
        });
    });

    describe('messages that can never succeed', () => {
        // prefetch 是 1：把一条永远处理不了的消息塞回队头，整条泳道就永远堵在它上面。
        // 所以"这次不行"和"永远不行"必须分开：前者重投，后者丢掉但吼出来。
        const permanent = async (env: unknown) => {
            const delivered: LarkEvent[] = [];
            const { amqp, store } = await start(async (e) => void delivered.push(e));
            await amqp.push('m1', env);
            return { delivered, amqp, store };
        };

        it('rejects an envelope that is not even an object', async () => {
            const { amqp } = await permanent('nonsense');
            expect(amqp.nacked).toEqual([{ id: 'm1', requeue: false }]);
        });

        it.each(['event_type', 'global_message_id', 'lane', 'bot_name'] as const)(
            'rejects an envelope with no %s',
            async (field) => {
                const broken = envelope();
                delete (broken as unknown as Record<string, unknown>)[field];
                const { amqp } = await permanent(broken);
                expect(amqp.nacked).toEqual([{ id: 'm1', requeue: false }]);
            },
        );

        // 没有 params 的信封以前会一路走到解析层、解析出 null、然后被当成"处理成功"
        // ACK 掉 —— 又一条静默丢失。
        it('rejects an envelope carrying no event payload', async () => {
            const { amqp, delivered } = await permanent(envelope({ params: undefined }));
            expect(delivered).toEqual([]);
            expect(amqp.nacked).toEqual([{ id: 'm1', requeue: false }]);
        });

        // 信封说该去别的泳道，却躺在我们的队列里：投递方错了，重投一万次也还是错的。
        it('rejects an envelope addressed to another lane', async () => {
            const { amqp } = await permanent(envelope({ lane: 'ppe-other' }));
            expect(amqp.nacked).toEqual([{ id: 'm1', requeue: false }]);
        });

        // 本服务不认领这个事件类型。以前是"没人处理"打条日志然后 ACK —— 静默丢失。
        it('rejects an event type this service does not claim', async () => {
            const { amqp, delivered } = await permanent(
                envelope({ event_type: 'im.chat.updated_v1' }),
            );
            expect(delivered).toEqual([]);
            expect(amqp.nacked).toEqual([{ id: 'm1', requeue: false }]);
        });

        // 永久失败一律不认领：认领了会留下一个没人清理的 in-flight 键。
        it('does not claim anything it rejects up front', async () => {
            const { store } = await permanent(envelope({ lane: 'ppe-other' }));
            expect(store.calls).toEqual([]);
        });

        // 载荷本身没救（比如消息事件里没有 message_id）：处理方抛 UnprocessableLarkEvent，
        // 跟"库连不上"这种下次可能就好了的失败区分开。
        it('rejects a payload the handler declares unprocessable, and frees the claim', async () => {
            const store = fakeStore();
            const { amqp } = await start(
                async () => {
                    throw new UnprocessableLarkEvent('event carries no message id');
                },
                { store },
            );

            await amqp.push('m1', envelope());

            expect(amqp.acked).toEqual([]);
            expect(amqp.nacked).toEqual([{ id: 'm1', requeue: false }]);
            expect(store.calls[store.calls.length - 1]).toMatch(/^release:/);
        });
    });
});
